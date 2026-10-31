import asyncio
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from pydantic_ai_harness.playwright import EgressPolicy, PlaywrightBrowserToolset
from analyzers.base import Analyzer
from common import (CONFIG, VERDICT_SEVERITY, AgentReport, Note, WorkflowReport, docstring_with, list_skills,
                    read_prompt, skill_body, split_backed, with_ids, without)
from tools.axe import axe_for_model, axe_on, axe_selector, check_messages, run_axe, wcag_number, wcag_tags
from tools.browser import ActionLog, GuardedSession, settle
from tools.page_content import content_for_model, content_on, get_page_content, item_for_model

ACCESSIBILITY = CONFIG.accessibility
BROWSER = CONFIG.browser
BROWSER_TOOLS = {"snapshot", "screenshot", "wait_for", "click", "hover", "press_key", "type_text", "select_option",
                 "go_back"}

class Assessment(BaseModel):
    rule: str = Field(description="The axe rule, exactly as run_axe returned it.")
    note: str = Field(description="One or two sentences explaining the problem and who it affects.")
    suggested_fix: str | None = Field(None, description="How to fix it, if you can tell.")

class Review(BaseModel):
    rule: str = Field(description="The axe rule, exactly as run_axe returned it.")
    selector: str = Field(description="The element's selector, exactly as run_axe returned it.")
    verdict: Literal["pass", "fail", "manual-review"] = Field(
        description="pass or fail if the tool results show which, otherwise manual-review.")
    evidence: str = Field(
        description="What in the tool results supports the verdict, or what is missing to decide.")
    note: str = Field(description="One or two sentences explaining the verdict.")
    state: str = Field("s0", description="The state of the page the element was in, exactly as run_axe returned it.")

SKILLS = list_skills(ACCESSIBILITY.skills)
CRITERIA = [skill for skill in SKILLS.values() if skill.rules()]
AgentRule = Literal[tuple(skill.name for skill in CRITERIA)]
WCAG_FOR_RULE = {skill.name: skill.values("wcag") for skill in CRITERIA}
ELEMENT_LISTS = {skill.name: skill.metadata["elements"] for skill in CRITERIA if "elements" in skill.metadata}
NEEDS_SELECTOR = {skill.name for skill in CRITERIA if skill.flag("needs-selector")}

class AgentFinding(BaseModel):
    rule: AgentRule = Field(description="The kind of problem.")  # pyright: ignore[reportInvalidTypeForm]
    selector: str | None = Field(  # None for a problem with the page as a whole
        None, description="The element with the problem, exactly as get_page_content returned it. Leave it out for "
                          "the page title, or for a problem with the page as a whole.")
    evidence: str = Field(description="What in the tool results shows the problem, e.g. the alt text, or a link's "
                                      "text and its context.")
    note: str = Field(description="One or two sentences explaining the problem and who it affects.")
    suggested_fix: str | None = Field(None, description="How to fix it, if you can tell.")
    state: str = Field("s0", description="The state of the page the element was in, exactly as get_page_content "
                                         "returned it.")

class SubmitReport(BaseModel):
    """Hand in the final report. Call this once, after you have finished investigating."""
    narrative: str = Field(
        description="A 2-4 sentence summary of the page's accessibility. Only mention things the tools showed.")
    assessments: list[Assessment] = Field(
        [], description="One entry per rule in run_axe's violations. Empty if there are none.")
    reviews: list[Review] = Field(
        [], description="One entry per element in run_axe's needs_review. Empty if there are none.")
    agent_findings: list[AgentFinding] = Field(
        [], description="Problems you found with get_page_content that axe does not report. Empty if there are none.")

REVIEW_SEVERITY = {"fail": "warning", "pass": "info", "manual-review": "needs-review"}

class Finding(BaseModel):
    rule: str
    severity: str  # error: axe violation, warning: problem the agent found, info: no problem, needs-review: undecided
    wcag: list[str]
    selector: str | None
    source: str  # axe or agent
    html: str | None = None
    summary: str | None = None  # axe's explanation
    verdict: str | None = None  # the agent's decision on an element axe could not decide
    evidence: str | None = None
    note: str | None = None
    suggested_fix: str | None = None

class StateFinding(Finding):
    state: str  # the state of the agent's page it was found in, like s0

class PageState(BaseModel):
    id: str  # s0 for the page as first loaded
    url: str
    actions: list[str]  # what the agent did after loading the page, oldest first
    axe: dict | None = Field(None, exclude=True)
    content: dict | None = Field(None, exclude=True)

class Report(AgentReport):
    axe_version: str | None
    browser: bool  # whether the agent had the browser tools
    states: list[PageState]  # every state of the page the agent checked
    findings: list[StateFinding]
    unverified: list[dict] = []  # submitted items no tool result backs, kept after the last rejection

class ElementVerdict(BaseModel):
    criterion: str
    id: str
    selector: str | None
    verdict: str  # pass, fail, unsure, or unanswered when no valid answer came even after asking again
    reason: str | None = None
    omitted_first: bool = False  # left out of the model's first answer for its batch

class AccessibilityWorkflowReport(WorkflowReport):
    axe_version: str | None
    findings: list[Finding]
    verdicts: list[ElementVerdict]  # every element judged, passes included

# Agent

def checked(states, state_id, key):
    return next((state for state in states if state.id == state_id and getattr(state, key) is not None), None)

def assessment_problem(assessment, states):
    results = [state.axe for state in states if state.axe is not None]
    if not results:
        return "you never ran run_axe"
    if assessment.rule not in {rule["id"] for axe in results for rule in axe["violations"]}:
        return "run_axe reported no violation of that rule"
    return None

def review_problem(review, states):
    state = checked(states, review.state, "axe")
    if state is None:
        return f"run_axe never checked a state {review.state}"
    to_review = {(rule["id"], axe_selector(node)) for rule in state.axe["incomplete"] for node in rule["nodes"]}
    if (review.rule, review.selector) not in to_review:
        return f"run_axe did not list that element under needs_review for that rule in {review.state}"
    return None

def finding_problem(finding, states):
    if not any(state.content is not None for state in states):
        return "you never read the page with get_page_content"
    if finding.rule not in ELEMENT_LISTS:
        return None  # page-title is about the whole page
    kind = ELEMENT_LISTS[finding.rule]
    noun = kind.removesuffix("s")
    if finding.selector is None:
        return f"give the selector of the {noun}" if finding.rule in NEEDS_SELECTOR else None
    state = checked(states, finding.state, "content")
    if state is None:
        return f"get_page_content never read a state {finding.state}"
    if finding.selector not in {item["selector"] for item in state.content[kind]}:
        return f"get_page_content returned no {noun} with that selector in {finding.state}"
    return None

def check_report(report, states):
    # splits the report into what the tool results back and what they don't
    assessments, unverified_assessments = split_backed(
        report.assessments, "assessment", lambda a: assessment_problem(a, states))
    reviews, unverified_reviews = split_backed(report.reviews, "review", lambda r: review_problem(r, states))
    agent_findings, unverified_findings = split_backed(
        report.agent_findings, "agent_finding", lambda f: finding_problem(f, states))
    return assessments, reviews, agent_findings, unverified_assessments + unverified_reviews + unverified_findings

def unbacked_message(unverified):
    lines = [f"- {item['kind']} {item['rule']}" + (f" on {item['selector']}" if item.get("selector") else "")
             + (f" in {item['state']}" if item.get("state") else "") + f": {item['reason']}" for item in unverified]
    return ("Report not accepted. These items are not backed by what the tools showed you:\n"
            + "\n".join(lines)
            + "\nCheck them, give them the rule, selector and state exactly as the tools returned them, or remove "
              "them, then call submit_report again.")

def first_seen(states, kind):
    seen = set()
    for state in states:
        for rule in state.axe[kind] if state.axe is not None else []:
            for node in rule["nodes"]:
                if (rule["id"], axe_selector(node)) not in seen:
                    seen.add((rule["id"], axe_selector(node)))
                    yield state, rule, node

def axe_report_findings(states, assessments, reviews):
    assessment_by_rule = {a.rule: a for a in assessments}
    review_by_element = {(r.rule, r.selector): r for r in reviews}
    findings = []
    for state, rule, node in first_seen(states, "violations"):
        assessment = assessment_by_rule.get(rule["id"])
        findings.append(StateFinding(
            rule=rule["id"],
            severity="error",
            wcag=wcag_tags(rule),
            selector=axe_selector(node),
            source="axe",
            state=state.id,
            html=node["html"][:ACCESSIBILITY.max_html_chars],
            summary=node.get("failureSummary"),
            note=assessment.note if assessment else None,
            suggested_fix=assessment.suggested_fix if assessment else None,
        ))
    for state, rule, node in first_seen(states, "incomplete"):
        review = review_by_element.get((rule["id"], axe_selector(node)))
        findings.append(StateFinding(
            rule=rule["id"],
            severity=REVIEW_SEVERITY[review.verdict] if review else "needs-review",
            wcag=wcag_tags(rule),
            selector=axe_selector(node),
            source="axe",
            state=state.id,
            html=node["html"][:ACCESSIBILITY.max_html_chars],
            summary=node.get("failureSummary"),
            verdict=review.verdict if review else None,
            evidence=review.evidence if review else None,
            note=review.note if review else None,
        ))
    return findings

def agent_report_finding(finding):
    return StateFinding(
        rule=finding.rule,
        severity="warning",
        wcag=WCAG_FOR_RULE[finding.rule],
        selector=finding.selector,
        source="agent",
        state=finding.state,
        evidence=finding.evidence,
        note=finding.note,
        suggested_fix=finding.suggested_fix,
    )

def save_axe(axe, args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(axe, indent=2))

# Workflow

def repeated_link_texts(links):
    groups = {}
    for link in links:
        name = link["aria_label"] or link["text"]
        if name:
            groups.setdefault(name.casefold(), []).append(link)
    return [group for group in groups.values() if len({link["href"].rstrip("/") for link in group}) > 1]

def group_for_model(group):
    text = group[0]["aria_label"] or group[0]["text"]
    return {"text": text, "links": [
        {key: value for key, value in {"destination": link["href"], "context": link["context"],
                                       "heading": link["heading"]}.items() if value and value != text}
        for link in group]}

def build_findings(axe, elements, answers, omitted):
    findings, verdicts = [], []
    if axe is not None:
        notes = {elements[element_id]["rule"]: answer for element_id, answer in answers.items()
                 if isinstance(answer, Note)}
        for rule in axe["violations"]:
            note = notes.get(rule["id"])
            for node in rule["nodes"]:
                findings.append(Finding(
                    rule=rule["id"],
                    severity="error",
                    wcag=wcag_tags(rule),
                    selector=axe_selector(node),
                    source="axe",
                    html=node["html"][:ACCESSIBILITY.max_html_chars],
                    summary=node.get("failureSummary"),
                    note=note.note if note else None,
                    suggested_fix=note.suggested_fix if note else None,
                ))

    for element_id, element in elements.items():
        if element["criterion"] == "axe-violations":
            continue
        answer = answers.get(element_id)
        verdict = answer.verdict if answer else "unanswered"
        verdicts.append(ElementVerdict(
            criterion=element["criterion"],
            id=element_id,
            selector=element["selector"],
            verdict=verdict,
            reason=answer.reason if answer else None,
            omitted_first=element_id in omitted,
        ))
        from_axe = element["criterion"] == "axe-needs-review"
        if verdict == "pass" and not from_axe:
            continue  # passes stay in verdicts; every element axe left undecided is a finding, as in the agent
        findings.append(Finding(
            rule=element["rule"] if from_axe else element["criterion"],
            severity=VERDICT_SEVERITY[verdict],
            wcag=element["wcag"] if from_axe else WCAG_FOR_RULE[element["criterion"]],
            selector=element["selector"],
            source="axe" if from_axe else "model",
            html=element.get("html"),
            summary=element.get("summary"),
            verdict=verdict,
            evidence=answer.reason if answer else None,
        ))
    return findings, verdicts

class AccessibilityAnalyzer(Analyzer):
    name = "accessibility"
    description = "Find accessibility problems on a web page."
    skills = SKILLS
    submission = SubmitReport
    agent_report_type = Report
    workflow_report_type = AccessibilityWorkflowReport
    judge_prompt = read_prompt(ACCESSIBILITY.prompts / "judge.md")

    def __init__(self, args):
        super().__init__(args)
        # What the tools found, filled in during the run: the latest successful result of each
        self.axe = None
        self.content = None
        self.browser = args.backend == "gemini" and not getattr(args, "no_browser", True)
        self.browser_session = GuardedSession(policy=EgressPolicy(
            allowed_domains=[urlparse(args.url).hostname], block_private_addresses=BROWSER.block_private_addresses))
        self.states = []  # every state of the page the agent checked
        self.loaded_url = None
        self.actions = []  # what the agent did since loading the page
        self.changed = False  # whether it did anything since the last check
        self.lands_on = None  # in the workflow, the page of the site the browser ended on instead of this one

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("--output", type=Path, default=Path("reports/axe_raw.json"),
                            help="where to save axe's raw results (default reports/axe_raw.json)")

    @staticmethod
    def add_agent_arguments(parser):
        parser.add_argument("--no-browser", action="store_true",
                            help="don't give the agent a browser to explore the page with; it never has one with "
                                 "--backend ollama")

    def finish(self):
        if self.axe is not None and getattr(self.args, "output", None):
            save_axe(self.axe, self.args)

    def axe_version(self):
        return self.axe["testEngine"]["version"] if self.axe is not None else None

    # Agent

    def system_prompt(self):
        prompt = read_prompt(ACCESSIBILITY.prompts / "agent.md")
        return (prompt + "\n\n" + read_prompt(ACCESSIBILITY.prompts / "browser.md")) if self.browser else prompt

    def user_prompt(self):
        return f"Check the accessibility of {self.args.url}"

    def page_changed(self, action):
        self.actions.append(action)
        self.changed = True

    async def check_page(self, url, key, collect):
        result = {"url": url, "final_url": None, "status": None, "error": None, key: None}
        try:
            page = await self.browser_session.ensure_page()
            if url is not None and (url != self.loaded_url or self.changed):
                response = await page.goto(url, wait_until="networkidle")
                result["status"] = response.status if response else None
                self.loaded_url, self.actions, self.changed = url, [], True
            if self.loaded_url is None:
                result["error"] = "no page is loaded yet: give the URL"
                return result
            result["url"] = self.loaded_url
            if self.changed:
                await settle(page)
                self.states.append(PageState(id=f"s{len(self.states)}", url=page.url, actions=list(self.actions)))
                self.changed = False
            state = self.states[-1]
            setattr(state, key, await collect(page))
            result.update({key: getattr(state, key), "final_url": page.url, "state": state.id})
        except Exception as e:
            message = str(e)
            result["error"] = message.splitlines()[0] if message else type(e).__name__
        return result

    @docstring_with(max_elements_per_rule=ACCESSIBILITY.max_elements_per_rule)
    async def run_axe_tool(self, url: str | None = None):
        """Runs the axe-core automated accessibility checks for WCAG levels A and AA on the page in a headless
        browser. Given a URL, it loads that page first, unless the browser already shows it as loaded; without one,
        it checks the page as it is now. Returns the state the page was in, the page's status code when it was just
        loaded, the rules the page violates and the rules axe could not decide, which need review, each with its
        impact, WCAG tags and up to {max_elements_per_rule} of the elements involved, and the ids of the rules that
        every element passed. The state is s0 for the page as first loaded, and a new one once the page has changed.
        The final URL is only given when the page is no longer at the URL it was loaded from, and an error only
        when the page could not be checked.

        Args:
            url: The URL of the web page to check. Leave it out to check the page as it is now.
        """
        result = await self.check_page(url, "axe", axe_on)
        if result["axe"] is not None:
            self.axe = result["axe"]
        return axe_for_model(result)

    @docstring_with(max_page_items=ACCESSIBILITY.max_page_items)
    async def get_page_content_tool(self, url: str | None = None):
        """Returns what a reader sees on the page in a headless browser: its title, the lang attribute of the page,
        the start of its visible main text, its headings in order with the start of the text under each, its visible
        images and links, and the parts of the page that have their own lang attribute, with their text. Given a
        URL, it loads that page first, unless the browser already shows it as loaded; without one, it reads the page
        as it is now. Each image comes with its selector, src, alt text, the text of the link it is in and the text
        around it; each link with its selector, text, URL, aria-label and title attributes and the text around it.
        Both also give the heading of the section they are in. A passage in another language that is not listed in
        parts has no lang attribute of its own. Up to {max_page_items} images, links and parts are listed. Empty
        fields are left out: an image with no alt attribute has no alt field, while a decorative image has an empty
        one. Also returns the state the page was in: s0 for the page as first loaded, and a new one once the page
        has changed. The final URL is only given when the page is no longer at the URL it was loaded from, and an
        error only when the page could not be read.

        Args:
            url: The URL of the web page to read. Leave it out to read the page as it is now.
        """
        result = await self.check_page(url, "content", content_on)
        if result["content"] is not None:
            self.content = result["content"]
        return content_for_model(result)

    def tools(self):
        return [Tool(self.run_axe_tool, name="run_axe"), Tool(self.get_page_content_tool, name="get_page_content")]

    def toolsets(self):
        if not self.browser:
            return []
        tools = PlaywrightBrowserToolset(session=self.browser_session, max_content_tokens=BROWSER.max_content_tokens)
        return [ActionLog(tools.filtered(lambda ctx, tool: tool.name in BROWSER_TOOLS), session=self.browser_session,
                          on_action=self.page_changed)]

    def session(self):
        return self.browser_session

    def request_limit(self):
        return BROWSER.max_requests if self.browser else super().request_limit()

    def review(self, report):
        *_, unverified = check_report(report, self.states)
        return unverified

    def reject(self, unverified):
        return unbacked_message(unverified)

    def agent_report(self, submitted):
        assessments, reviews, agent_findings, unverified = check_report(submitted, self.states)
        findings = (axe_report_findings(self.states, assessments, reviews)
                    + [agent_report_finding(f) for f in agent_findings])
        return findings, dict(axe_version=self.axe_version(), unverified=unverified, browser=self.browser,
                              states=self.states)

    # Workflow

    def collect(self):
        axe_result = asyncio.run(run_axe(self.args.url))
        content_result = asyncio.run(get_page_content(self.args.url))
        for tool_name, result in (("run_axe", axe_result), ("get_page_content", content_result)):
            if result["error"]:
                print(f"====={tool_name} could not load {self.args.url}: {result['error']}=====")
        self.axe, self.content = axe_result["axe"], content_result["content"]
        # a page that ends on another page checked on its own, like one that refreshes to the home page, isn't
        # judged again
        landed = content_result["final_url"]
        if landed and landed != self.args.url and landed in getattr(self.args, "checked_elsewhere", ()):
            print(f"====={self.args.url} lands on {landed}, which is checked on its own=====")
            self.lands_on = landed
            return False
        return self.axe is not None or self.content is not None

    def instructions(self, skill_name, ids):
        skill = SKILLS[skill_name]
        criterion = ""
        if skill.values("wcag"):
            numbers = " and ".join(wcag_number(tag) for tag in skill.values("wcag"))
            criterion = f"Criterion: {skill.name} (WCAG {numbers}). "
        return f"{criterion}{ids}\n{skill_body(skill)}"

    def ask_questions(self, questions):
        add = questions.ask
        axe, content = self.axe, self.content

        if axe is not None:
            html_chars = ACCESSIBILITY.max_html_chars
            add("axe-violations", "Each id is a rule that axe-core found violated, with a few of the elements "
                                  "involved. Write a note for each id.", [
                (element_id, {"rule": rule["id"], "help": rule["help"], "elements_total": len(rule["nodes"]),
                              "examples": [{"html": node["html"][:html_chars], "problems": check_messages(node)}
                                           for node in rule["nodes"][:ACCESSIBILITY.max_violation_examples]]},
                 {"rule": rule["id"]})
                for element_id, rule in with_ids(axe["violations"], "violation")], skill="axe-results", answer=Note)
            to_review = [(rule, node) for rule in axe["incomplete"] for node in rule["nodes"]]
            add("axe-needs-review", "Each id is an element that axe-core could not decide, with the rule it was "
                                    "checked against. Answer pass or fail only if the information here shows which; "
                                    "otherwise answer unsure.", [
                (element_id, {"rule": rule["id"], "help": rule["help"], "html": node["html"][:html_chars],
                              "problems": check_messages(node)},
                 {"rule": rule["id"], "wcag": wcag_tags(rule), "selector": axe_selector(node),
                  "html": node["html"][:html_chars], "summary": node.get("failureSummary")})
                for element_id, (rule, node) in with_ids(to_review, "review")], skill="axe-results")

        if content is not None:
            add("alt-text", "Each id is an image.", [
                (element_id, item_for_model(without(image, "selector"), "link_text"), {"selector": image["selector"]})
                for element_id, image in with_ids(content["images"], "img")])
            # no destinations here: whether a link leads where it says is the link checker's job
            add("link-purpose", "Each id is a link.", [
                (element_id, item_for_model(without(link, "selector", "href"), "text"),
                 {"selector": link["selector"]})
                for element_id, link in with_ids(content["links"], "link")])
            add("link-purpose", "Each id is a group of links with the same text that lead to different places.", [
                (element_id, group_for_model(group), {"selector": ", ".join(link["selector"] for link in group)})
                for element_id, group in with_ids(repeated_link_texts(content["links"]), "repeat")])
            add("page-title", "Each id is a page, with its title, first headings and the start of its text.", [
                ("page-1", {"title": content["title"],
                           "headings": [heading["text"]
                                        for heading in content["headings"][:ACCESSIBILITY.max_title_headings]],
                           "text": content["text"][:ACCESSIBILITY.max_title_text_chars]},
                 {"selector": None})])
            add("page-language", 'The id "lang" is the page, with its lang and the start of its text. The other ids '
                                 'are the parts of it that have their own lang.',
                [("lang", item_for_model({"lang": content["lang"],
                                          "text": content["text"][:ACCESSIBILITY.max_text_chars]}, "text"),
                  {"selector": None})]
                + [(element_id, without(part, "selector"), {"selector": part["selector"]})
                   for element_id, part in with_ids(content["parts"], "part")],
                batched=False)
            # the headings every page shares, like the footer's, are judged once for the site, each with the
            # outline of that part of the page around it
            own = [heading for heading in content["headings"] if not heading.get("layout")]
            shared = [heading for heading in content["headings"] if heading.get("layout")]
            add("heading", "Each id is a heading.", [
                (element_id, item_for_model(without(heading, "selector"), "text"),
                 {"selector": heading["selector"]})
                for element_id, heading in with_ids(own, "h")],
                batched=False)
            outline = [f"h{heading['level']} {heading['text']}" for heading in shared]
            add("heading", "Each id is a heading in the part every page of the site shares, like its header or "
                           "footer, with the outline of the headings there.", [
                (element_id, {**item_for_model(without(heading, "selector", "layout"), "text"), "outline": outline},
                 {"selector": heading["selector"]})
                for element_id, heading in with_ids(shared, "shared")])

    def workflow_report(self, questions):
        findings, verdicts = build_findings(self.axe, questions.elements, questions.answers.by_id,
                                            questions.answers.omitted)
        return findings, verdicts, dict(axe_version=self.axe_version())
