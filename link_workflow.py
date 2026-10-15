import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import ClassVar
from urllib.parse import urlparse
from pydantic import BaseModel
from common import (VERDICT_SEVERITY, Answers, Verdict, WorkflowReport, add_task, answers_schema, ask_tasks,
                    criteria_counts, make_chat, make_parser, parse_args, report_fields, report_path, with_ids)
from link_analyzer import (Finding, build_findings, check_page_links, compact, ends_on_homepage, fetch_page_text,
                           link_for_model, record_result)

PAGE_TEXT_CHARS = 600  # the start of a page is enough to tell an error page from a real one
SIGN_IN = re.compile(r"\b(log|sign)[ -]?in\b", re.IGNORECASE)

class LinkNote(BaseModel):
    FORMAT: ClassVar = ("notes", answers_schema("notes", {
        "id": {"type": "string"},
        "note": {"type": "string"},
        "likely_intentional": {"type": "boolean"},
        "suggested_fix": {"type": "string"},
    }, ["id", "note", "likely_intentional"]))
    id: str
    note: str
    likely_intentional: bool
    suggested_fix: str | None = None

SYSTEM_PROMPT = """
    You are a web link reviewer. You answer one question at a time about a few links of a web page, or the pages
    they lead to, using only the information you are given about them.

    For every item, give a short reason and then a verdict:
    - pass: there is no problem. A few words of reason are enough.
    - fail: the information shows the problem the question asks about. The reason must say what in the item
      shows it.
    - unsure: the information you were given is not enough to decide. If you can't check something, the answer
      is unsure, never fail.
    Answer for every id you are given, and only for those.
"""

BROKEN_LINKS = """The link checker has confirmed that these links are broken, unreachable or blocked, so don't
judge them again. For each id, write a note explaining the problem in one or two sentences, say whether it looks
deliberate (likely_intentional), for example a test page that demonstrates a 404, and suggest how to fix it if you
can tell."""

SOFT_404 = """Question: soft-404. Each id is a page that a link leads to, and the server answered it with a working
status. That only means the server answered: an error page or an empty page can return a working status too. Fail
a page that is an error page or says that what it was about is no longer there, for example "page not found",
"no longer available" or an error message. Pass a page that shows real content."""

REDIRECT = """Question: redirect. Each id is a link that was redirected and ended on a working page. Compare the
link's text and URL with its final_url and the title and heading of the page it ended on. Pass a link that ends on
the page it promises, even at a new address. Fail a link that ends somewhere the link did not promise, such as the
homepage, a login page or a list page instead of the item it names."""

TEXT_MISMATCH = """Question: text-mismatch. Does each link lead to what its text says? Compare the link's text with
its URL and the title and heading of the page it leads to. Fail a link whose text and destination clearly don't
agree, for example a link that names one product but leads to another. Pass a link whose destination fits its text.
Text that names no destination, like "read more", can't disagree with it, so it passes."""

class LinkVerdict(BaseModel):
    criterion: str
    id: str
    url: str  # the link, or for soft-404 the page the links lead to
    verdict: str  # pass, fail, unsure, or unanswered when no valid answer came even after asking again
    reason: str | None = None
    omitted_first: bool = False  # left out of the model's first answer for its batch

class LinkWorkflowReport(WorkflowReport):
    link_checks: dict[str, int]  # links per check verdict: ok, redirect, broken, blocked, unreachable
    links_found: int  # unique links found on the page
    links_checked: int  # how many of those were checked; should equal links_found
    pages_read: int  # pages the links lead to, read with fetch_page_text for the soft-404 question
    findings: list[Finding]
    verdicts: list[LinkVerdict]  # every link or page judged, passes included

def leads_to_page(link):
    # a working link to an HTML page, whose destination can be judged
    check = link["check"]
    return link["tag"] == "a" and check["verdict"] in ("ok", "redirect") and check["content_type"] == "text/html"

def pages_to_read(links):
    # the pages the links lead to, each with the links that end on it
    pages = {}
    for link in links:
        if leads_to_page(link):
            pages.setdefault(link["check"]["final_url"], []).append(link["url"])
    return pages

def redirect_rule(link, checks):
    # where a redirect that fails ended
    check = link["check"]
    if ends_on_homepage(link["url"], checks, {}):
        return "redirect-to-homepage"
    if SIGN_IN.search(urlparse(check["final_url"]).path) or SIGN_IN.search(check["title"] or ""):
        return "redirect-to-login"
    return "other"

def broken_entries(links):
    broken = [link for link in links if link["check"]["verdict"] in ("broken", "unreachable", "blocked")]
    return [(element_id, link_for_model(link), {"url": link["url"]})
            for element_id, link in with_ids(broken, "broken")]

def soft_404_entries(pages, fetched):
    read = [(url, fetched[url]) for url in pages if fetched[url]["text"] is not None]
    return [(element_id,
             compact({**page, "text": page["text"][:PAGE_TEXT_CHARS]}, leave_out=("content_type", "truncated")),
             {"url": url, "links": pages[url], "rule": "soft-404"})
            for element_id, (url, page) in with_ids(read, "page")]

def redirect_entries(links, checks, failed):
    redirected = [link for link in links if link["tag"] == "a" and link["check"]["verdict"] == "redirect"
                  and link["url"] not in failed]
    return [(element_id, link_for_model(link),
             {"url": link["url"], "links": [link["url"]], "rule": redirect_rule(link, checks)})
            for element_id, link in with_ids(redirected, "redirect")]

def text_mismatch_entries(links, failed):
    named = [link for link in links if leads_to_page(link) and link["text"] and link["url"] not in failed
             and (link["check"]["title"] or link["check"]["heading"])]
    return [(element_id, link_for_model(link), {"url": link["url"], "links": [link["url"]], "rule": "text-mismatch"})
            for element_id, link in with_ids(named, "link")]

def failed_links(elements, answers):
    # the links a question has already failed, which the later questions leave out
    return {url for element_id, answer in answers.by_id.items()
            if isinstance(answer, Verdict) and answer.verdict == "fail"
            for url in elements[element_id]["links"]}

def build_report_findings(checks, link_info, elements, answers):
    notes = {elements[element_id]["url"]: answer for element_id, answer in answers.by_id.items()
             if isinstance(answer, LinkNote)}
    findings = build_findings(checks, link_info, notes)
    verdicts = []
    for element_id, element in elements.items():
        if element["criterion"] == "broken-links":
            continue  # the notes are already on the checker's findings
        answer = answers.by_id.get(element_id)
        verdict = answer.verdict if answer else "unanswered"
        verdicts.append(LinkVerdict(
            criterion=element["criterion"],
            id=element_id,
            url=element["url"],
            verdict=verdict,
            reason=answer.reason if answer else None,
            omitted_first=element_id in answers.omitted,
        ))
        if verdict == "pass":
            continue  # passes stay in verdicts
        for url in element["links"]:
            info = link_info.get(url, {})
            findings.append(Finding(
                rule=element["rule"],
                severity=VERDICT_SEVERITY[verdict],
                page=info.get("page"),
                url=url,
                tag=info.get("tag"),
                text=info.get("text"),
                status=checks[url]["status"],
                error=checks[url]["error"],
                source="model",
                verdict=verdict,
                evidence=answer.reason if answer else None,
            ))
    return findings, verdicts

def main():
    args = parse_args(make_parser("Find link problems on a web page, asking the model about one question and a few "
                                  "links at a time.", workflow=True))
    chat = make_chat(args, SYSTEM_PROMPT, tools=[])

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()

    page = check_page_links(args.url)
    if page["error"]:
        print(f"=====check_page_links could not check {args.url}: {page['error']}=====")
        return
    link_info, checks = {}, {}
    record_result("check_page_links", page, link_info, checks)
    links = page["links"]
    print(f"=====Checked {len(links)} link(s): {dict(Counter(check['verdict'] for check in checks.values()))}=====")

    pages = pages_to_read(links)
    print(f"=====Reading the {len(pages)} page(s) the links lead to=====")
    fetched = {url: fetch_page_text(url) for url in pages}

    usage = Counter()
    answers = Answers()
    elements = {}

    def ask_about(criterion, instructions, entries, **options):
        tasks = []
        add_task(tasks, elements, criterion, instructions, entries, **options)
        ask_tasks(chat, tasks, args.batch_size, usage, answers)

    # In this order: a link that fails one question is left out of the later ones
    ask_about("broken-links", BROKEN_LINKS, broken_entries(links), answer=LinkNote)
    ask_about("soft-404", SOFT_404, soft_404_entries(pages, fetched))
    ask_about("redirect", REDIRECT, redirect_entries(links, checks, failed_links(elements, answers)))
    ask_about("text-mismatch", TEXT_MISMATCH, text_mismatch_entries(links, failed_links(elements, answers)))

    findings, verdicts = build_report_findings(checks, link_info, elements, answers)
    report = LinkWorkflowReport(
        **report_fields(args, chat, started_at, start, usage),
        counts=dict(Counter(finding.severity for finding in findings)),
        invalid_replies=usage["invalid_replies"],
        batch_size=args.batch_size,
        criteria=criteria_counts(verdicts, answers.invented),
        link_checks=dict(Counter(check["verdict"] for check in checks.values())),
        links_found=len(link_info),
        links_checked=sum(1 for url in link_info if url in checks),
        pages_read=len(fetched),
        findings=findings,
        verdicts=verdicts,
    )
    path = report_path("links-workflow", chat.model, args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
