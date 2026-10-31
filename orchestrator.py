import argparse
import time
from argparse import Namespace
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, Field
from pydantic_ai import BinaryContent, ModelRequest, ModelResponse, Tool, ToolCallPart, ToolReturnPart
from agent import SkillTools, run_agent
from analyzers.accessibility import WCAG_FOR_RULE, AccessibilityAnalyzer
from analyzers.links import LinkAnalyzer
from common import (CONFIG, VERDICT_SEVERITY, AgentReport, Verdict, docstring_with, read_prompt, report_fields,
                    report_path, skills_prompt, trace_to_logfire)
from crawl import crawl_settings, crawl_site, load_or_crawl, save_crawl
from tools.browser import screenshot
from tools.links import check_url, fetch_page_text, page_text_for_model, remember_results
from workflow import Judge, Memory, run_workflows

ORCHESTRATOR = CONFIG.orchestrator
CHECKS = {"links": LinkAnalyzer, "accessibility": AccessibilityAnalyzer}  # what the workers can check on a page
SKILLS = {**LinkAnalyzer.skills, **AccessibilityAnalyzer.skills}
TO_REVIEW = {"fail", "unsure", "unanswered"}  # the worker verdicts that always need the orchestrator's decision
NOTES = {"broken-links", "axe-violations"}  # criteria the workers answer with a note on a problem, not a verdict
READS_DESTINATION = {"soft-404", "redirect", "text-mismatch"}  # items whose evidence includes the page linked to
WORKER_USAGE = ("llm_requests", "input_tokens", "output_tokens", "truncated_replies", "invalid_replies")
CHECK_TOOLS = {"check_links", "check_accessibility"}  # each call to one starts a round
KEPT_RESULTS = {"crawl_site", "decide"}  # results never cleared: the pages' ids are needed until the end

class Decision(BaseModel):
    item: str = Field(description="The item's id, like i12.")
    verdict: Literal["pass", "fail", "unsure"]
    reason: str = Field(description="What in the item's evidence shows this verdict.")

class LeftOut(BaseModel):
    page: str = Field(description="The page's id, like p3.")
    check: Literal["links", "accessibility"] = Field(description="The check you didn't run on it.")
    reason: str = Field(description="Why you didn't run it.")

class SubmitReport(BaseModel):
    narrative: str = Field(
        description="A 3-6 sentence summary of the site's links and accessibility, and of which workers' verdicts you "
                    "changed and why. Only mention what the tools showed you.")
    left_out: list[LeftOut] = Field(
        [], description="The checks you chose not to run on pages with status 200 and HTML, one entry per page and "
                        "check. Empty if there are none.")

class PageResult(BaseModel):
    id: str
    url: str
    status: int | None
    content_type: str | None
    checks: dict[str, str]  # per check: analyzed, failed, left out, lands on another page, or not analyzed
    why: dict[str, str] = {}  # per check: the error, why the orchestrator left it out, or the page it lands on

class ItemResult(BaseModel):
    id: str
    check: str
    criterion: str
    url: str | None  # the link, or for soft-404 the page the links lead to; None for accessibility
    selector: str | None  # the element, for accessibility
    pages: list[str]  # every page it came up on
    worker_verdict: str  # pass, fail, unsure or unanswered; note for a note on a broken link or a violated rule
    worker_reason: str | None
    decision: Literal["confirmed", "changed"] | None = None  # None when the orchestrator didn't decide on it
    verdict: str  # after the orchestrator's decision
    reason: str | None
    evidence_read: bool
    looked_at: bool  # whether the orchestrator saw it with look_at
    facts: dict  # what the worker was shown

class SiteFinding(BaseModel):
    check: str
    rule: str
    severity: str  # error: broken, unreachable or an axe violation, warning: a problem found by a model, info: blocked, redirected or not requested, needs-review: undecided
    url: str  # the link, or for accessibility the first page it is on
    pages: list[str]  # every page it is on
    selector: str | None = None
    html: str | None = None
    wcag: list[str] = []
    text: str | None = None
    status: int | None = None
    error: str | None = None
    source: str  # checker, axe, worker, or orchestrator when it changed the worker's verdict
    item: str | None = None
    verdict: str | None = None
    evidence: str | None = None
    note: str | None = None
    likely_intentional: bool | None = None
    suggested_fix: str | None = None

class SiteReport(AgentReport):
    mode: str = "orchestrator"
    narrative: str | None  # None when the orchestrator stopped before submitting a report
    worker_model: str
    worker_thinking: bool
    worker_llm_requests: int
    worker_input_tokens: int
    worker_output_tokens: int
    worker_truncated_replies: int
    worker_invalid_replies: int
    worker_model_s: float  # the time the workers waited on their model
    worker_collect_s: float  # the time the workers' tools took, like checking links or running axe-core
    worker_by_check: dict[str, dict[str, float]]  # the same, per check
    tools_s: float  # the time the orchestrator's tools took, the workers' included
    crawl_s: float | None  # the time crawl_site took to crawl or load the site
    crawl_started_at: str | None
    crawl_reused: bool  # whether a crawl saved before the run was used
    coverage: dict[str, dict[str, int]]
    decisions: dict[str, int]  # confirmed, and each kind of change, like pass->fail
    rounds: list[dict]  # each check call, in order: its check, its pages, and the items it returned
    refused_decisions: list[dict]  # decisions made without the evidence or the skill, and why they were refused
    pages: list[PageResult]
    findings: list[SiteFinding]
    items: list[ItemResult]

def filled(entry):
    return {key: value for key, value in entry.items() if value not in (None, [], {})}

def unique(values):
    return list(dict.fromkeys(values))

def shortened(value, limit=ORCHESTRATOR.max_fact_chars):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {key: shortened(inner, limit) for key, inner in value.items()}
    if isinstance(value, list):
        return [shortened(inner, limit) for inner in value]
    return value

def cleared_note(part):
    # what replaces a result of an earlier round in the conversation
    if part.tool_name in CHECK_TOOLS and isinstance(part.content, dict):
        pages = ", ".join(page["id"] for page in part.content.get("pages", []))
        return (f"Cleared to save space: this round's {len(part.content.get('items', []))} items, from {pages}, are "
                "done. Read any of them again with get_evidence.")
    if part.tool_name == "load_skill":
        return "Cleared to save space: load the skill again to read it."
    return f"Cleared to save space: call {part.tool_name} again to see this result."

def check_of(item):
    return "links" if item["skill"] in LinkAnalyzer.skills else "accessibility"

def worker_verdict(item):
    if item["criterion"] in NOTES:
        return "note"
    return item["answer"].verdict if item["answer"] is not None else "unanswered"

def worker_reason(item):
    answer = item["answer"]
    if answer is None:
        return None
    return answer.reason if isinstance(answer, Verdict) else answer.note

class Orchestrator:
    def __init__(self, args):
        self.args = args
        self.started = datetime.now().astimezone()
        self.crawl = None
        self.crawl_reused = False
        self.crawl_s = None
        self.pages = {}  # by id, like p3: the crawl's entry for the page
        self.page_ids = {}  # by URL
        self.reports = {check: {} for check in CHECKS}  # per check, by page id: the worker's report
        self.errors = {check: {} for check in CHECKS}  # per check, by page id: why the worker couldn't check it
        self.lands_on = {}  # by page id: the page of the site the accessibility check's browser ended on instead
        self.memory = Memory()
        self.shown = set()  # the facts already returned, like a broken link's (url, rule)
        self.evidence_read = set()
        self.looked_at = set()
        self.decisions = {}  # by item id
        self.refused = []
        self.rounds = []  # each check call: its check, pages and items
        self.cleared = set()  # the tool calls whose results were cleared from the conversation
        self.skills = SkillTools(SKILLS)
        # the workers' model, for every page; each check has its own system prompt
        self.judges = {check: Judge(self.worker_args(check, args.url), analyzer.judge_prompt)
                       for check, analyzer in CHECKS.items()}

    def items(self):
        return {item["id"]: item for item in self.memory.items.values()}

    def to_analyze(self):
        return [page_id for page_id, page in self.pages.items()
                if page["status"] == 200 and page["content_type"] == "text/html"]

    def done(self, check, page_id):
        return (page_id in self.reports[check] or page_id in self.errors[check]
                or (check == "accessibility" and page_id in self.lands_on))

    def item_pages(self, item):
        return unique(element["page"] for element in item["elements"])

    def crawl_site_tool(self):
        """Crawls the site from its start page, following its links like a visitor would, and lists every page it
        found: its id, URL, status code, content type, title, how many clicks it is from the start page, where it
        redirects to, and how many forms it has. Also lists the links the crawl didn't follow, with why, and says how
        many links lead to other sites. A crawl of the site saved in the last hours is reused."""
        if self.crawl is None:
            start = time.perf_counter()
            url = self.args.url
            scope = {"mode": "host", "hosts": [urlsplit(url).hostname]}
            if self.args.recrawl:
                self.crawl = crawl_site(url, scope, crawl_settings())
                save_crawl(self.crawl)
            else:
                self.crawl = load_or_crawl(url, scope, crawl_settings())
            self.crawl_reused = datetime.fromisoformat(self.crawl["started_at"]) < self.started
            self.pages = {f"p{number}": page for number, page in enumerate(self.crawl["pages"], 1)}
            self.page_ids = {page["url"]: page_id for page_id, page in self.pages.items()}
            self.crawl_s = round(time.perf_counter() - start, 1)
        forms = Counter(form["found_on"] for form in self.crawl["forms"])
        return {
            "start_url": self.crawl["start_url"],
            "pages": [filled({"id": page_id, "url": page["url"], "status": page["status"],
                              "content_type": page["content_type"], "title": page["title"], "depth": page["depth"],
                              "redirect_to": page["redirect_to"], "error": page["error"],
                              "forms": forms[page["url"]] or None})
                      for page_id, page in self.pages.items()],
            "not_followed": [{"url": link["url"], "reason": link["reason"]} for link in self.crawl["skipped"]],
            "links_to_other_sites": len(self.crawl["external"]),
            "stopped_early": self.crawl["truncated"],
        }

    def worker_args(self, check, url, site_pages=()):
        # site_pages: the site's pages that are checked on their own, so one the browser lands on isn't judged twice
        return Namespace(url=url, analyzer=CHECKS[check].name, backend="ollama", model=self.args.worker_model,
                         no_think=self.args.no_think, max_output_tokens=self.args.max_output_tokens,
                         batch_size=self.args.batch_size, output=None, checked_elsewhere=set(site_pages))

    def page_facts(self, check, report):
        # what a page's tools found that the orchestrator doesn't review; one already returned isn't repeated
        if check == "accessibility":
            violated = unique(finding.rule for finding in report.findings
                              if finding.source == "axe" and finding.verdict is None)
            return {"axe_violations": violated}
        checker = []
        for finding in report.findings:
            # redirects come up again as items
            if (finding.source != "checker" or finding.rule == "redirect"
                    or ("links", finding.url, finding.rule) in self.shown):
                continue
            self.shown.add(("links", finding.url, finding.rule))
            checker.append(filled({"url": finding.url, "rule": finding.rule, "text": finding.text,
                                   "status": finding.status, "error": finding.error, "note": finding.note}))
        return {"links_checked": report.links_checked, "checker": checker}

    async def analyze(self, check, page_ids):
        # the workers judge the pages together: each question is asked once for all of them
        print(f"\n=====Workers: {check} of {', '.join(page_ids)}=====")
        site_pages = [self.pages[page_id]["url"] for page_id in self.to_analyze()]
        analyzers = [CHECKS[check](self.worker_args(check, self.pages[page_id]["url"], site_pages))
                     for page_id in page_ids]
        try:
            reports = await run_workflows(analyzers, self.judges[check], self.memory)
        except Exception as e:
            for page_id in page_ids:
                self.errors[check][page_id] = str(e) or type(e).__name__
            return [{"id": page_id, "error": self.errors[check][page_id]} for page_id in page_ids]

        results = []
        for page_id, analyzer, report in zip(page_ids, analyzers, reports):
            lands_on = getattr(analyzer, "lands_on", None)
            if lands_on:
                self.lands_on[page_id] = lands_on
                results.append({"id": page_id, "lands_on": self.page_ids.get(lands_on, lands_on)})
            elif report is None:
                self.errors[check][page_id] = "the worker's tools could not read the page"
                results.append({"id": page_id, "error": self.errors[check][page_id]})
            else:
                self.reports[check][page_id] = report
                results.append(filled({"id": page_id, **self.page_facts(check, report)}))
        return results

    def item_for_model(self, item, pages, full=False):
        entry = {"id": item["id"], "criterion": item["criterion"]}
        verdict = worker_verdict(item)
        if verdict == "note":
            entry["note"] = worker_reason(item)
        else:
            entry["verdict"] = verdict
            # in accessibility most of the workers' mistakes are passes, so their passes come with the reason
            if verdict != "pass" or full or check_of(item) == "accessibility":
                entry["reason"] = worker_reason(item)
        if full:
            entry["selector"] = item["kept"].get("selector")
            entry["facts"] = item["shown"]
        elif not (verdict == "pass" and check_of(item) == "accessibility"):
            entry["facts"] = shortened(item["shown"])  # an accessibility pass has its reason instead, to keep it short
        entry["pages"] = pages
        return filled(entry)

    async def check_pages(self, check, pages):
        if self.crawl is None:
            return {"error": "no pages yet: call crawl_site first"}
        if len(pages) > ORCHESTRATOR.pages_per_call:
            return {"error": f"send at most {ORCHESTRATOR.pages_per_call} pages per call"}
        analyzable = set(self.to_analyze())
        judged_before = set(self.items())
        to_check, problems = [], {}
        for page_id in unique(pages):
            if page_id not in self.pages:
                problems[page_id] = "there is no page with this id"
            elif page_id not in analyzable:
                problems[page_id] = "only pages with status 200 and HTML can be checked"
            elif self.done(check, page_id):
                problems[page_id] = "already checked"
            else:
                to_check.append(page_id)
        results = await self.analyze(check, to_check) if to_check else []

        batch = {self.pages[result["id"]]["url"] for result in results
                 if "error" not in result and "lands_on" not in result}
        new_items, also_on = [], {}
        for item in self.memory.items.values():
            if check_of(item) != check:
                continue
            on_batch = [self.page_ids[url] for url in self.item_pages(item) if url in batch]
            if not on_batch:
                continue
            if item["id"] in judged_before:
                also_on[item["id"]] = on_batch
            else:
                new_items.append(self.item_for_model(item, on_batch))
        self.rounds.append({"check": check, "pages": to_check, "items": len(new_items),
                            "to_review": sum(entry.get("verdict") in TO_REVIEW for entry in new_items)})
        return filled({"pages": results, "items": new_items, "also_on": also_on, "problems": problems})

    def clear_finished_rounds(self, messages):
        # once a new round starts, the results of the earlier ones are replaced by a short note, if every item they
        # returned that needs a decision has one: the code keeps every item, and get_evidence reads it again
        starts = [index for index, message in enumerate(messages) if isinstance(message, ModelResponse)
                  and any(isinstance(part, ToolCallPart) and part.tool_name in CHECK_TOOLS for part in message.parts)]
        if len(starts) < 2:
            return messages
        latest = starts[-1]
        earlier = [part for message in messages[:latest] if isinstance(message, ModelRequest)
                   for part in message.parts if isinstance(part, ToolReturnPart)]
        by_id = self.items()
        returned = [entry["id"] for part in earlier if part.tool_name in CHECK_TOOLS and isinstance(part.content, dict)
                    for entry in part.content.get("items", [])]
        if any(worker_verdict(by_id[item_id]) in TO_REVIEW and item_id not in self.decisions
               for item_id in returned if item_id in by_id):
            return messages  # an earlier round isn't finished
        cleared = 0
        for index, message in enumerate(messages[:latest]):
            if not isinstance(message, ModelRequest):
                continue
            parts = []
            for part in message.parts:
                if (isinstance(part, ToolReturnPart) and part.tool_name not in KEPT_RESULTS
                        and part.tool_call_id not in self.cleared):
                    self.cleared.add(part.tool_call_id)
                    part = replace(part, content=cleared_note(part))
                    cleared += 1
                parts.append(part)
            messages[index] = replace(message, parts=parts)
        if cleared:
            print(f"=====Cleared {cleared} result(s) of earlier rounds from the conversation=====")
        return messages

    @docstring_with(max_pages=ORCHESTRATOR.pages_per_call)
    async def check_links_tool(self, pages: list[str]):
        """Sends pages to a worker, a smaller model, which checks every link on each page and judges them: whether
        each page the links lead to is a soft 404, whether each redirect is a problem, and whether each link's text
        matches the page it leads to. Up to {max_pages} pages per call, and only pages with status 200 and HTML. The
        pages of one call are judged together, which is faster than sending them in separate calls.

        For each page, returns the links the link checker found broken, unreachable, blocked, or didn't request
        because they look like actions; these are facts, and one already returned for another page isn't repeated.
        Then returns the items the worker judged for the first time on these pages: each with its id, criterion,
        verdict, the pages it came up on, and the facts the worker was shown, with long texts shortened; a verdict
        other than pass also comes with the worker's reason, and a note on a broken link has a note instead of a
        verdict. An item that comes up again isn't judged again: also_on lists the items judged on earlier pages that
        came up on these, with the pages they came up on.

        Args:
            pages: The ids of the pages to check, like p3, as crawl_site listed them.
        """
        return await self.check_pages("links", pages)

    @docstring_with(max_pages=ORCHESTRATOR.pages_per_call)
    async def check_accessibility_tool(self, pages: list[str]):
        """Sends pages to a worker, a smaller model, which checks their accessibility against WCAG 2.2. axe-core first
        tests each page; then the worker writes a note on each rule axe-core found violated, and judges the elements
        axe-core couldn't decide, each image's alt text, each link's purpose, the page's title and language, and its
        headings. The headings in the part every page shares, like the footer, are judged once for the site. Up to
        {max_pages} pages per call, and only pages with status 200 and HTML. The pages of one call are judged
        together, which is faster than sending them in separate calls. A page whose browser ends on another page of
        the site, like one that refreshes to the home page, isn't judged again: lands_on says which page it ended on.

        For each page, returns the rules axe-core found violated; these are facts. Then returns the items the worker
        judged for the first time on these pages: each with its id, criterion, verdict, the worker's reason and the
        pages it came up on. Items other than passes also come with the facts the worker was shown, with long texts
        shortened; get_evidence returns a pass's facts. A note on a violated rule has a note instead of a verdict. An
        item that comes up again isn't judged again: also_on lists the items judged on earlier pages that came up on
        these, with the pages they came up on.

        Args:
            pages: The ids of the pages to check, like p3, as crawl_site listed them.
        """
        return await self.check_pages("accessibility", pages)

    def destination(self, item):
        # the page an item's link leads to, or for a soft-404 item that page itself
        if item["criterion"] == "soft-404":
            return item["kept"]["url"]
        if item["criterion"] in READS_DESTINATION:
            return check_url(item["kept"]["url"])["final_url"]
        return None

    @docstring_with(max_items=ORCHESTRATOR.evidence_per_call)
    def get_evidence_tool(self, items: list[str]):
        """Returns everything about items: the facts the worker was shown, in full, its verdict and reason, and every
        page the item came up on, and for an accessibility item its element's selector. For an item about a page that
        links lead to, like a soft 404, a redirect or a link's text, it also returns that page's title, first heading
        and text, more of it than the worker saw. Read an item's evidence before you decide on it. Up to {max_items}
        items per call.

        Args:
            items: The ids of the items, like i12.
        """
        if len(items) > ORCHESTRATOR.evidence_per_call:
            return {"error": f"ask for at most {ORCHESTRATOR.evidence_per_call} items per call"}
        by_id = self.items()
        found, unknown = [], []
        for item_id in unique(items):
            item = by_id.get(item_id)
            if item is None:
                unknown.append(item_id)
                continue
            self.evidence_read.add(item_id)
            entry = self.item_for_model(item, [{"id": self.page_ids[url], "url": url} for url in self.item_pages(item)],
                                        full=True)
            destination = self.destination(item)
            if destination:
                entry["destination"] = page_text_for_model(fetch_page_text(destination))
            found.append(entry)
        return filled({"items": found, "unknown_ids": unknown})

    @docstring_with(max_items=ORCHESTRATOR.look_per_call)
    async def look_at_tool(self, items: list[str]):
        """Shows you how accessibility items look on their page: a screenshot of each item's element with a little of
        the page around it, or of the top of the page for an item about the whole page, like its title or language.
        The workers and axe-core only read a page's code and text, and none of them sees the page. Up to {max_items}
        items per call.

        Args:
            items: The ids of the items, like i12.
        """
        if len(items) > ORCHESTRATOR.look_per_call:
            return {"error": f"ask for at most {ORCHESTRATOR.look_per_call} items per call"}
        by_id = self.items()
        returned = []
        for item_id in unique(items):
            item = by_id.get(item_id)
            if item is None:
                returned.append({"item": item_id, "error": "there is no item with this id"})
                continue
            if check_of(item) != "accessibility":
                returned.append({"item": item_id, "error": "only accessibility items have an element to show"})
                continue
            url, selector = self.item_pages(item)[0], item["kept"].get("selector")
            shot = await screenshot(url, selector)
            if shot["error"]:
                returned.append({"item": item_id, "error": shot["error"]})
                continue
            self.looked_at.add(item_id)
            returned += [filled({"item": item_id, "page": self.page_ids.get(url, url), "selector": selector}),
                         BinaryContent(shot["png"], media_type="image/png")]
        return returned

    def decision_problem(self, decision, item):
        if item is None:
            return "there is no item with this id"
        if worker_verdict(item) == "note":
            return "this is a note, which has no verdict to decide on"
        if decision.item not in self.evidence_read:
            return "read its evidence with get_evidence first"
        if item["skill"] not in self.skills.loaded:
            return f"load the {item['skill']} skill with load_skill first"
        return None

    def decide_tool(self, decisions: list[Decision]):
        """Records your verdicts on items, each confirming the worker's verdict or changing it. Decide on every item
        the worker failed, was unsure about or left unanswered, and on every pass you find wrong. A decision is only
        accepted for an item whose evidence you read with get_evidence, once you loaded the skill for its criterion
        with load_skill. Deciding on an item again replaces your earlier decision. Notes, on broken links or on rules
        axe-core found violated, have no verdict to decide on.

        Args:
            decisions: Your verdict on each item, with what in its evidence shows it.
        """
        by_id = self.items()
        accepted, refused = [], []
        for decision in decisions:
            item = by_id.get(decision.item)
            problem = self.decision_problem(decision, item)
            if problem:
                refused.append({"item": decision.item, "reason": problem})
                self.refused.append({**decision.model_dump(), "refused_because": problem})
            else:
                self.decisions[decision.item] = decision
                accepted.append({"item": decision.item, "changed": decision.verdict != worker_verdict(item)})
        return filled({"accepted": accepted, "refused": refused})

    def tools(self):
        return [Tool(self.crawl_site_tool, name="crawl_site"), Tool(self.check_links_tool, name="check_links"),
                Tool(self.check_accessibility_tool, name="check_accessibility"),
                Tool(self.get_evidence_tool, name="get_evidence"), Tool(self.look_at_tool, name="look_at"),
                Tool(self.decide_tool, name="decide"), *self.skills.tools()]

    def missing(self, report):
        left_out = {(entry.check, entry.page) for entry in report.left_out}
        missing = [{"kind": "page", "id": page_id,
                    "reason": f"the {check} check didn't run on it, and it isn't in left_out"}
                   for check in CHECKS for page_id in self.to_analyze()
                   if not self.done(check, page_id) and (check, page_id) not in left_out]
        missing += [{"kind": "item", "id": item["id"],
                     "reason": f"the worker's verdict is {worker_verdict(item)}, and you haven't decided on it"}
                    for item in self.memory.items.values()
                    if worker_verdict(item) in TO_REVIEW and item["id"] not in self.decisions]
        return missing

    def reject(self, missing):
        listed = missing[:ORCHESTRATOR.max_missing_listed]
        lines = [f"- {entry['kind']} {entry['id']}: {entry['reason']}" for entry in listed]
        if len(missing) > len(listed):
            lines.append(f"- and {len(missing) - len(listed)} more")
        return ("Report not accepted. These aren't done yet:\n" + "\n".join(lines)
                + "\nCheck those pages with check_links or check_accessibility, or put them in left_out with why. Read "
                  "those items' evidence with get_evidence and decide on them with decide. Then call submit_report "
                  "again.")

    # Report

    def final(self, item):
        decision = self.decisions.get(item["id"])
        if decision is None:
            return worker_verdict(item), worker_reason(item), "worker"
        changed = decision.verdict != worker_verdict(item)
        return decision.verdict, decision.reason, "orchestrator" if changed else "worker"

    def fact_findings(self):
        # what the tools found without a model: the link checker's problems and axe-core's violations
        findings = {}
        for page_id, report in self.reports["links"].items():
            for finding in report.findings:
                if finding.source != "checker":
                    continue
                site_finding = findings.setdefault(("links", finding.url, finding.rule), SiteFinding(
                    check="links", rule=finding.rule, severity=finding.severity, url=finding.url, pages=[],
                    text=finding.text, status=finding.status, error=finding.error, source="checker",
                    note=finding.note, likely_intentional=finding.likely_intentional,
                    suggested_fix=finding.suggested_fix))
                site_finding.pages.append(self.pages[page_id]["url"])
        for page_id, report in self.reports["accessibility"].items():
            page_url = self.pages[page_id]["url"]
            for finding in report.findings:
                if finding.source != "axe" or finding.verdict is not None:
                    continue  # the elements axe-core couldn't decide are items
                key = ("accessibility", finding.rule, finding.selector, finding.html)
                site_finding = findings.setdefault(key, SiteFinding(
                    check="accessibility", rule=finding.rule, severity=finding.severity, url=page_url, pages=[],
                    selector=finding.selector, html=finding.html, wcag=finding.wcag, source="axe", note=finding.note,
                    suggested_fix=finding.suggested_fix))
                site_finding.pages.append(page_url)
        return list(findings.values())

    def item_findings(self):
        findings = []
        for item in self.memory.items.values():
            verdict, reason, source = self.final(item)
            if verdict in ("pass", "note"):
                continue
            pages, kept = self.item_pages(item), item["kept"]
            if check_of(item) == "accessibility":
                findings.append(SiteFinding(
                    check="accessibility", rule=kept.get("rule") or item["criterion"],
                    severity=VERDICT_SEVERITY[verdict], url=pages[0], pages=pages, selector=kept.get("selector"),
                    html=kept.get("html"), wcag=kept.get("wcag") or WCAG_FOR_RULE.get(item["criterion"], []),
                    source=source, item=item["id"], verdict=verdict, evidence=reason))
                continue
            link_item = item["criterion"] != "soft-404"  # a soft-404 item is about a page, not a link
            link_check = check_url(kept["url"]) if link_item else {}
            findings.append(SiteFinding(
                check="links", rule=kept["rule"], severity=VERDICT_SEVERITY[verdict], url=kept["url"], pages=pages,
                text=item["shown"].get("text") if link_item else None, status=link_check.get("status"),
                error=link_check.get("error"), source=source, item=item["id"], verdict=verdict, evidence=reason))
        return findings

    def item_results(self):
        results = []
        for item in self.memory.items.values():
            decision = self.decisions.get(item["id"])
            verdict, reason, _ = self.final(item)
            results.append(ItemResult(
                id=item["id"], check=check_of(item), criterion=item["criterion"], url=item["kept"].get("url"),
                selector=item["kept"].get("selector"), pages=self.item_pages(item),
                worker_verdict=worker_verdict(item), worker_reason=worker_reason(item),
                decision=None if decision is None else "changed" if decision.verdict != worker_verdict(item)
                else "confirmed",
                verdict=verdict, reason=reason, evidence_read=item["id"] in self.evidence_read,
                looked_at=item["id"] in self.looked_at, facts=item["shown"]))
        return results

    def page_result(self, page_id, page, left_out):
        checks, why = {}, {}
        for check in CHECKS:
            if page_id in self.reports[check]:
                checks[check] = "analyzed"
            elif page_id in self.errors[check]:
                checks[check], why[check] = "failed", self.errors[check][page_id]
            elif check == "accessibility" and page_id in self.lands_on:
                checks[check], why[check] = "lands on another page", self.lands_on[page_id]
            elif (check, page_id) in left_out:
                checks[check], why[check] = "left out", left_out[(check, page_id)]
            else:
                checks[check] = "not analyzed"
        return PageResult(id=page_id, url=page["url"], status=page["status"], content_type=page["content_type"],
                          checks=checks, why=why)

    def site_report(self, submitted, lead, started_at, start, usage):
        left_out = {}
        if submitted:
            left_out = {(entry.check, entry.page): entry.reason for entry in submitted.left_out
                        if entry.page in self.pages}
        items = list(self.memory.items.values())
        to_review = [item for item in items if worker_verdict(item) in TO_REVIEW]
        decisions = Counter()
        for item in items:
            decision = self.decisions.get(item["id"])
            if decision is not None:
                was = worker_verdict(item)
                decisions["confirmed" if decision.verdict == was else f"{was}->{decision.verdict}"] += 1
        findings = self.fact_findings() + self.item_findings()
        workers = Counter()
        for judge in self.judges.values():
            workers.update(judge.usage)
        coverage = {"pages": {"found": len(self.pages), "to_analyze": len(self.to_analyze())}}
        for check in CHECKS:
            coverage[check] = {
                "pages_analyzed": len(self.reports[check]),
                "pages_failed": len(self.errors[check]),
                "pages_left_out": sum(entry_check == check for entry_check, _ in left_out),
                **({"pages_landing_elsewhere": len(self.lands_on)} if check == "accessibility" else {}),
                "items": sum(check_of(item) == check for item in items),
                "items_to_review": sum(check_of(item) == check for item in to_review),
                "items_reviewed": sum(check_of(item) == check and item["id"] in self.decisions for item in to_review),
                "passes_reviewed": sum(check_of(item) == check and item["id"] in self.decisions
                                       for item in items if worker_verdict(item) == "pass"),
            }
        coverage["items"] = {
            "elements_judged": sum(len(item["elements"]) for item in items),  # across pages, so repeats count
            "evidence_read": len(self.evidence_read),
            "looked_at": len(self.looked_at),
            "results_cleared": len(self.cleared),
        }
        return SiteReport(
            **report_fields(lead, started_at, start, usage),
            counts=dict(Counter(finding.severity for finding in findings)),
            narrative=submitted.narrative if submitted else None,
            rejected_submissions=usage["rejected_submissions"],
            unbacked_claims=usage["unbacked_claims"],
            text_replies=usage["text_replies"],
            skills_loaded=self.skills.loaded,
            skill_files_read=self.skills.files_read,
            worker_model=self.args.worker_model,
            worker_thinking=not self.args.no_think,
            **{f"worker_{key}": workers[key] for key in WORKER_USAGE},
            worker_model_s=round(workers["model_s"], 1),
            worker_collect_s=round(workers["collect_s"], 1),
            worker_by_check={check: {key: round(judge.usage[key], 1)
                                     for key in (*WORKER_USAGE, "model_s", "collect_s")}
                             for check, judge in self.judges.items()},
            tools_s=round(usage["tools_s"], 1),
            crawl_s=self.crawl_s,
            crawl_started_at=self.crawl["started_at"] if self.crawl else None,
            crawl_reused=self.crawl_reused,
            coverage=coverage,
            decisions=dict(decisions),
            rounds=self.rounds,
            refused_decisions=self.refused,
            pages=[self.page_result(page_id, page, left_out) for page_id, page in self.pages.items()],
            findings=findings,
            items=self.item_results(),
        )

def parse_args():
    parser = argparse.ArgumentParser(description="Check a website's links and accessibility: a Gemini model crawls "
                                                 "it, sends its pages to smaller Ollama models that judge them, then "
                                                 "reviews and corrects what they decided.")
    parser.add_argument("url", help="the site's start page")
    parser.add_argument("--model", help=f"the orchestrator's Gemini model (default {CONFIG.models.gemini})")
    parser.add_argument("--worker-model", default=CONFIG.models.ollama,
                        help=f"the workers' Ollama model (default {CONFIG.models.ollama})")
    parser.add_argument("--no-think", action="store_true",
                        help="the workers answer without thinking first, which is much faster on a CPU")
    parser.add_argument("--max-output-tokens", type=int, default=CONFIG.ollama.max_output_tokens,
                        help="the most tokens a worker may write per turn, thinking included "
                             f"(default {CONFIG.ollama.max_output_tokens})")
    parser.add_argument("--batch-size", type=int, default=CONFIG.workflow.batch_size,
                        help=f"how many elements a worker judges in one call (default {CONFIG.workflow.batch_size})")
    parser.add_argument("--recrawl", action="store_true",
                        help="crawl the site again, even if a recent crawl of it was saved")
    args = parser.parse_args()
    if urlsplit(args.url).scheme not in ("http", "https") or not urlsplit(args.url).hostname:
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    return args

def main():
    trace_to_logfire()
    args = parse_args()
    remember_results()  # each URL is checked once, however many pages link to it
    orchestrator = Orchestrator(args)
    lead = Namespace(url=args.url, backend="gemini", model=args.model, no_think=False,
                     max_output_tokens=CONFIG.ollama.max_output_tokens)
    system_prompt = f"{read_prompt(ORCHESTRATOR.prompts / 'agent.md')}\n\n# Skills\n\n{skills_prompt(SKILLS)}"

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    submitted, usage = run_agent(lead, system_prompt,
                                 f"Check the links and the accessibility of the site at {args.url}",
                                 orchestrator.tools(), SubmitReport, orchestrator.missing, orchestrator.reject,
                                 request_limit=ORCHESTRATOR.max_requests,
                                 process_history=orchestrator.clear_finished_rounds)

    report = orchestrator.site_report(submitted, lead, started_at, start, usage)
    path = report_path("orchestrator", lead)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
