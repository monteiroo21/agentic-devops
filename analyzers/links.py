import re
from collections import Counter
from typing import ClassVar, Literal
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from analyzers.base import Analyzer
from common import (CONFIG, VERDICT_SEVERITY, AgentReport, Verdict, WorkflowReport, answers_schema, list_skills,
                    read_prompt, skill_body, split_backed, with_ids)
from tools.links import (check_page_links, check_url, check_urls, compact, fetch_page_text, link_for_model,
                         page_links_for_model, page_text_for_model)

LINKS = CONFIG.links
SIGN_IN = re.compile(r"\b(log|sign)[ -]?in\b", re.IGNORECASE)

class Assessment(BaseModel):
    url: str = Field(description="The link this comment is about.")
    likely_intentional: bool = Field(
        description="True if the problem looks deliberate, e.g. a test page that demonstrates a 404.")
    note: str = Field(description="One or two sentences explaining the problem.")
    suggested_fix: str | None = Field(
        None, description="How to fix it, if you can tell. Leave this out if no fix is needed.")

SKILLS = list_skills(LINKS.skills)
FINDING_RULES = [rule for skill in SKILLS.values() for rule in skill.rules()] + ["other"]
AgentRule = Literal[tuple(FINDING_RULES)]
NEEDS_PAGE_TEXT = {rule for skill in SKILLS.values() if skill.flag("needs-page-text")
                   for rule in skill.rules()} | {"other"}

class AgentFinding(BaseModel):
    url: str = Field(description="The link with the problem, exactly as the tools returned it.")
    rule: AgentRule = Field(description="The kind of problem.")  # pyright: ignore[reportInvalidTypeForm]
    evidence: str = Field(
        description="What in the tool results shows the problem, e.g. the page title or the final URL.")
    note: str = Field(description="One or two sentences explaining the problem.")

class SubmitReport(BaseModel):
    """Hand in the final report. Call this once, after you have finished investigating."""
    narrative: str = Field(
        description="A 2-4 sentence summary of the page's link health. Only mention things the tools checked.")
    assessments: list[Assessment] = Field(
        description="One entry per broken, blocked or unreachable link. Empty if there are none.")
    agent_findings: list[AgentFinding] = Field(
        [], description="Problems you found that the status code does not show, e.g. a page that returns 200 but is "
                        "an error page, a redirect to the homepage or a login page, or link text that does not match "
                        "the page it leads to. Empty if there are none.")

class RejectedClaim(AgentFinding):
    reason: Literal["not-read", "not-homepage-redirect", "no-tool-result"]
    times_rejected: int
    read_afterwards: bool | None  # None when reading the page wasn't what was missing
    outcome: Literal["kept", "relabelled", "dropped"]


class Finding(BaseModel):
    rule: str
    severity: str  # error: broken or unreachable, warning: a problem the model found, info: blocked, redirected or not requested, needs-review: undecided
    page: str | None
    url: str
    tag: str | None
    text: str | None
    status: int | None
    error: str | None
    source: str  # checker, agent or model
    verdict: str | None = None  # the workflow model's decision on a link
    evidence: str | None = None
    note: str | None = None
    likely_intentional: bool | None = None
    suggested_fix: str | None = None

class Report(AgentReport):
    link_checks: dict[str, int]  # links per check verdict: ok, redirect, broken, blocked, unreachable
    links_found: int  # unique links found on the page
    links_checked: int  # how many of those were checked; should equal links_found
    findings: list[Finding]
    unverified: list[dict] = []  # submitted items no tool result backs, kept after the last rejection
    rejected_claims: list[RejectedClaim] = []  # each distinct claim that was sent back, and its outcome

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

# Agent

def build_findings(checks, link_info, notes):
    # notes are the model's comments on the broken links, by URL: the agent's Assessments or the workflow's LinkNotes
    findings = []
    for url, check in checks.items():
        verdict = check["verdict"]
        if verdict == "ok":
            continue
        info = link_info.get(url, {})
        tag = info.get("tag")
        if verdict == "broken":
            rule = "broken-link" if tag in ("a", None) else "broken-resource"
            severity = "error"
        elif verdict == "unreachable":
            rule, severity = "unreachable", "error"
        else:  # "blocked", "redirect", or "skipped": not requested, since it looks like an action
            rule, severity = verdict, "info"
        note = notes.get(url)
        findings.append(Finding(
            rule=rule,
            severity=severity,
            page=info.get("page"),
            url=url,
            tag=tag,
            text=info.get("text"),
            status=check["status"],
            error=check["error"],
            source="checker",
            note=note.note if note else None,
            likely_intentional=note.likely_intentional if note else None,
            suggested_fix=note.suggested_fix if note else None,
        ))
    return findings

def unbacked_reason(agent_finding, checks, fetched):
    if agent_finding.rule in NEEDS_PAGE_TEXT:
        return "not-read"
    if agent_finding.rule == "redirect-to-homepage" and (agent_finding.url in checks or agent_finding.url in fetched):
        return "not-homepage-redirect"
    return "no-tool-result"

def final_url_of(url, checks, fetched):
    return (checks.get(url) or fetched.get(url) or {}).get("final_url")

def ends_on_homepage(url, checks, fetched):
    # the link was redirected and ended on the root of a site
    final_url = final_url_of(url, checks, fetched)
    return final_url is not None and final_url != url and urlparse(final_url).path in ("", "/")

def is_backed(agent_finding, checks, fetched):
    url = agent_finding.url
    if agent_finding.rule in NEEDS_PAGE_TEXT:
        final_url = checks.get(url, {}).get("final_url")
        return url in fetched or final_url in fetched
    if agent_finding.rule == "redirect-to-homepage" and not ends_on_homepage(url, checks, fetched):
        return False
    return url in fetched or url in checks

def assessment_problem(assessment, checks):
    check = checks.get(assessment.url)
    if check is None:
        return "no tool result shows this URL"
    if check["verdict"] == "ok":
        return (f"this link answered with status {check['status']} and no redirect, so the checker reported nothing "
                "for it to assess; if its page has a problem the status code does not show, put it in agent_findings")
    return None

def finding_problem(agent_finding, checks, fetched):
    if is_backed(agent_finding, checks, fetched):
        return None
    reason = unbacked_reason(agent_finding, checks, fetched)
    if reason == "not-read":
        return "you never read that page or the page it redirects to"
    if reason == "not-homepage-redirect":
        final_url = final_url_of(agent_finding.url, checks, fetched)
        return f"the tools show it ends on {final_url}, which is not a redirect to a homepage"
    return "no tool result shows this URL"

def check_report(report, checks, fetched):
    # splits the report into what the tool results back and what they don't
    assessments, unverified_assessments = split_backed(
        report.assessments, "assessment", lambda a: assessment_problem(a, checks))
    agent_findings, unverified_findings = split_backed(
        report.agent_findings, "agent_finding", lambda f: finding_problem(f, checks, fetched))
    return assessments, agent_findings, unverified_assessments + unverified_findings

def unbacked_message(unverified):
    lines = [f"- {item['kind']}" + (f" {item['rule']}" if "rule" in item else "")
             + f" for {item['url']}: {item['reason']}" for item in unverified]
    return ("Report not accepted. These items are not backed by what the tools showed you:\n"
            + "\n".join(lines)
            + "\nCheck them (use fetch_page_text to read a page), give them the rule that fits, or remove them, "
              "then call submit_report again.")

def build_agent_findings(agent_findings, checks, fetched, link_info, findings):
    # Turn the agent's backed findings into report findings
    already_errors = {f.url for f in findings if f.severity == "error"}
    report_findings = []
    for agent_finding in agent_findings:
        url = agent_finding.url
        if url in already_errors:
            continue  # already reported as broken by the checker
        info = link_info.get(url, {})
        result = checks.get(url) or fetched.get(url, {})
        report_findings.append(Finding(
            rule=agent_finding.rule,
            severity="warning",
            page=info.get("page"),
            url=url,
            tag=info.get("tag"),
            text=info.get("text"),
            status=result.get("status"),
            error=result.get("error"),
            source="agent",
            evidence=agent_finding.evidence,
            note=agent_finding.note,
        ))
    return report_findings

def rejection_outcomes(rejections, final_findings, checks, fetched):
    final_rules = {}
    for f in final_findings:
        final_rules.setdefault(f.url, set()).add(f.rule)
    rejected = []
    for (url, rule), entry in rejections.items():
        if rule in final_rules.get(url, set()):
            outcome = "kept"
        elif url in final_rules:
            outcome = "relabelled"
        else:
            outcome = "dropped"
        read_afterwards = None
        if entry["reason"] != "not-homepage-redirect":
            read_afterwards = url in fetched or final_url_of(url, checks, fetched) in fetched
        rejected.append(RejectedClaim(**entry["claim"].model_dump(), reason=entry["reason"],
                                      times_rejected=entry["times_rejected"],
                                      read_afterwards=read_afterwards, outcome=outcome))
    return rejected

# Workflow

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
             compact({**page, "text": page["text"][:LINKS.workflow_page_text_chars]},
                     leave_out=("content_type", "truncated")),
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

class LinkAnalyzer(Analyzer):
    name = "links"
    description = "Find link problems on a web page."
    skills = SKILLS
    submission = SubmitReport
    agent_report_type = Report
    workflow_report_type = LinkWorkflowReport
    judge_prompt = read_prompt(LINKS.prompts / "judge.md")

    def __init__(self, args):
        super().__init__(args)
        # What the tools found, filled in during the run
        self.link_info = {}
        self.checks = {}
        self.fetched = {}
        self.rejections = {}  # each distinct claim that was sent back, with why and how often
        self.links = []  # the links on the page, for the workflow
        self.pages = {}  # the pages they lead to, for the workflow

    def record_checks(self, results):
        for result in results:
            self.checks[result["url"]] = result

    def record_page_links(self, page):
        for link in page["links"]:
            self.link_info.setdefault(link["url"],
                                      {"page": page["final_url"], "tag": link["tag"], "text": link["text"]})
        self.record_checks([link["check"] for link in page["links"]])

    def link_counts(self):
        return dict(
            link_checks=dict(Counter(check["verdict"] for check in self.checks.values())),
            links_found=len(self.link_info),
            links_checked=sum(1 for url in self.link_info if url in self.checks),
        )

    # Agent

    def system_prompt(self):
        rules = ", ".join(FINDING_RULES[:-1])
        read_first = [rule for rule in FINDING_RULES if rule in NEEDS_PAGE_TEXT]
        read_first = ", ".join(read_first[:-1]) + " or " + read_first[-1]
        return read_prompt(LINKS.prompts / "agent.md", rules=rules, read_first=read_first)

    def user_prompt(self):
        return f"Check the links on {self.args.url}"

    def check_url_tool(self, url: str):
        """Checks whether a single URL works and returns its status code, final URL, redirect chain, content type and
        any connection error, and for an HTML page its title and first heading. Empty fields are left out, and the
        final URL is only given when it differs from the URL.

        Args:
            url: The URL to check.
        """
        result = check_url(url)
        self.record_checks([result])
        return compact(result)

    def check_page_links_tool(self, url: str):
        """Finds every link on a web page and checks all of them. Returns each link's text, tag and whether it is
        internal or external, with its status code, final URL, redirect chain, content type and any connection
        error, and for a link to an HTML page that page's title and first heading. Empty fields are left out, and the
        final URL is only given when it differs from the link's URL.

        Args:
            url: The URL of the web page whose links to check.
        """
        page = check_page_links(url)
        self.record_page_links(page)
        return page_links_for_model(page)

    def check_urls_tool(self, urls: list[str]):
        """Checks whether multiple URLs work and returns their status code, final URL, redirect chain, content type
        and any connection error, and for HTML pages their title and first heading. Empty fields are left out, and
        the final URL is only given when it differs from the URL.

        Args:
            urls: The URLs to check.
        """
        results = check_urls(urls)
        self.record_checks(results)
        return [compact(result) for result in results]

    def fetch_page_text_tool(self, url: str):
        """Fetches the title, first heading and a short snippet of visible text from a web page. Empty fields are
        left out, and the final URL is only given when the page redirected.

        Args:
            url: The URL of the web page to fetch text from.
        """
        page = fetch_page_text(url)
        self.fetched[page["url"]] = page
        return page_text_for_model(page)

    def tools(self):
        return [Tool(self.check_url_tool, name="check_url"), Tool(self.check_page_links_tool, name="check_page_links"),
                Tool(self.check_urls_tool, name="check_urls"), Tool(self.fetch_page_text_tool, name="fetch_page_text")]

    def review(self, report):
        *_, unverified = check_report(report, self.checks, self.fetched)
        return unverified

    def reject(self, unverified):
        for item in unverified:
            if item["kind"] == "agent_finding":
                claim = AgentFinding.model_validate(item)
                entry = self.rejections.setdefault((claim.url, claim.rule), {
                    "claim": claim, "reason": unbacked_reason(claim, self.checks, self.fetched), "times_rejected": 0})
                entry["times_rejected"] += 1
        return unbacked_message(unverified)

    def agent_report(self, submitted):
        assessments, agent_findings, unverified = check_report(submitted, self.checks, self.fetched)
        findings = build_findings(self.checks, self.link_info, {a.url: a for a in assessments})
        findings += build_agent_findings(agent_findings, self.checks, self.fetched, self.link_info, findings)
        rejected_claims = rejection_outcomes(self.rejections, submitted.agent_findings, self.checks, self.fetched)
        return findings, dict(**self.link_counts(), unverified=unverified, rejected_claims=rejected_claims)

    # Workflow

    def collect(self):
        page = check_page_links(self.args.url)
        if page["error"]:
            print(f"=====check_page_links could not check {self.args.url}: {page['error']}=====")
            return False
        self.record_page_links(page)
        self.links = page["links"]
        print(f"=====Checked {len(self.links)} link(s): "
              f"{dict(Counter(check['verdict'] for check in self.checks.values()))}=====")

        self.pages = pages_to_read(self.links)
        print(f"=====Reading the {len(self.pages)} page(s) the links lead to=====")
        self.fetched = {url: fetch_page_text(url) for url in self.pages}
        return True

    def instructions(self, skill_name, ids):
        return f"{ids}\n{skill_body(SKILLS[skill_name])}"

    def ask_questions(self, questions):
        # In this order: a link that fails one question is left out of the later ones
        questions.ask("broken-links", "Each id is a link that the link checker found broken, unreachable or blocked. "
                                      "Write a note for each id, with likely_intentional saying whether the problem "
                                      "looks deliberate.",
                      broken_entries(self.links), answer=LinkNote)
        questions.ask("soft-404", "Question: soft-404. Each id is a page that a link leads to, and the server answered "
                                  "it with a working status. Fail a page that is a soft 404.",
                      soft_404_entries(self.pages, self.fetched))
        questions.ask("redirect", "Question: redirect. Each id is a link that was redirected and ended on a working "
                                  "page. Fail a link whose redirect is a problem.",
                      redirect_entries(self.links, self.checks, failed_links(questions.elements, questions.answers)))
        questions.ask("text-mismatch", "Question: text-mismatch. Each id is a link to a working page. Fail a link "
                                       "whose text and destination don't agree.",
                      text_mismatch_entries(self.links, failed_links(questions.elements, questions.answers)))

    def workflow_report(self, questions):
        findings, verdicts = build_report_findings(self.checks, self.link_info, questions.elements, questions.answers)
        return findings, verdicts, dict(**self.link_counts(), pages_read=len(self.fetched))
