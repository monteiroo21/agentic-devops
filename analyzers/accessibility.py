import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from analyzers.base import Analyzer
from common import (CONFIG, VERDICT_SEVERITY, AgentReport, Note, WorkflowReport, docstring_with, list_skills,
                    read_prompt, skill_body, split_backed, with_ids, without)
from tools.axe import axe_for_model, axe_selector, check_messages, run_axe, wcag_number, wcag_tags
from tools.page_content import content_for_model, get_page_content, item_for_model

ACCESSIBILITY = CONFIG.accessibility

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

class Report(AgentReport):
    axe_version: str | None
    findings: list[Finding]
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

def assessment_problem(assessment, axe):
    if axe is None:
        return "you never ran run_axe"
    if assessment.rule not in {rule["id"] for rule in axe["violations"]}:
        return "run_axe reported no violation of that rule"
    return None

def review_problem(review, axe):
    if axe is None:
        return "you never ran run_axe"
    to_review = {(rule["id"], axe_selector(node)) for rule in axe["incomplete"] for node in rule["nodes"]}
    if (review.rule, review.selector) not in to_review:
        return "run_axe did not list that element under needs_review for that rule"
    return None

def finding_problem(finding, content):
    if content is None:
        return "you never read the page with get_page_content"
    if finding.rule not in ELEMENT_LISTS:
        return None  # page-title is about the whole page
    kind = ELEMENT_LISTS[finding.rule]
    noun = kind.removesuffix("s")
    if finding.selector is None:
        return f"give the selector of the {noun}" if finding.rule in NEEDS_SELECTOR else None
    if finding.selector not in {item["selector"] for item in content[kind]}:
        return f"get_page_content returned no {noun} with that selector"
    return None

def check_report(report, axe, content):
    # splits the report into what the tool results back and what they don't
    assessments, unverified_assessments = split_backed(
        report.assessments, "assessment", lambda a: assessment_problem(a, axe))
    reviews, unverified_reviews = split_backed(report.reviews, "review", lambda r: review_problem(r, axe))
    agent_findings, unverified_findings = split_backed(
        report.agent_findings, "agent_finding", lambda f: finding_problem(f, content))
    return assessments, reviews, agent_findings, unverified_assessments + unverified_reviews + unverified_findings

def unbacked_message(unverified):
    lines = [f"- {item['kind']} {item['rule']}" + (f" on {item['selector']}" if item.get("selector") else "")
             + f": {item['reason']}" for item in unverified]
    return ("Report not accepted. These items are not backed by what the tools showed you:\n"
            + "\n".join(lines)
            + "\nCheck them, give them the rule and selector exactly as the tools returned them, or remove them, "
              "then call submit_report again.")

def axe_report_findings(axe, assessments, reviews):
    assessment_by_rule = {a.rule: a for a in assessments}
    review_by_element = {(r.rule, r.selector): r for r in reviews}
    findings = []
    for rule in axe["violations"]:
        assessment = assessment_by_rule.get(rule["id"])
        for node in rule["nodes"]:
            findings.append(Finding(
                rule=rule["id"],
                severity="error",
                wcag=wcag_tags(rule),
                selector=axe_selector(node),
                source="axe",
                html=node["html"][:ACCESSIBILITY.max_html_chars],
                summary=node.get("failureSummary"),
                note=assessment.note if assessment else None,
                suggested_fix=assessment.suggested_fix if assessment else None,
            ))
    for rule in axe["incomplete"]:
        for node in rule["nodes"]:
            review = review_by_element.get((rule["id"], axe_selector(node)))
            findings.append(Finding(
                rule=rule["id"],
                severity=REVIEW_SEVERITY[review.verdict] if review else "needs-review",
                wcag=wcag_tags(rule),
                selector=axe_selector(node),
                source="axe",
                html=node["html"][:ACCESSIBILITY.max_html_chars],
                summary=node.get("failureSummary"),
                verdict=review.verdict if review else None,
                evidence=review.evidence if review else None,
                note=review.note if review else None,
            ))
    return findings

def agent_report_finding(finding):
    return Finding(
        rule=finding.rule,
        severity="warning",
        wcag=WCAG_FOR_RULE[finding.rule],
        selector=finding.selector,
        source="agent",
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

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("--output", type=Path, default=Path("reports/axe_raw.json"),
                            help="where to save axe's raw results (default reports/axe_raw.json)")

    def finish(self):
        if self.axe is not None:
            save_axe(self.axe, self.args)

    def axe_version(self):
        return self.axe["testEngine"]["version"] if self.axe is not None else None

    # Agent

    def system_prompt(self):
        return read_prompt(ACCESSIBILITY.prompts / "agent.md")

    def user_prompt(self):
        return f"Check the accessibility of {self.args.url}"

    @docstring_with(max_elements_per_rule=ACCESSIBILITY.max_elements_per_rule)
    def run_axe_tool(self, url: str):
        """Loads a web page in a headless browser and runs the axe-core automated accessibility checks for WCAG
        levels A and AA on it. Returns the page's status code, the rules the page violates and the rules axe could
        not decide, which need review, each with its impact, WCAG tags and up to {max_elements_per_rule} of the
        elements involved, and the ids of the rules that every element passed. The final URL is only given when the
        page redirected, and an error only when the page could not be checked.

        Args:
            url: The URL of the web page to check.
        """
        result = run_axe(url)
        if result["axe"] is not None:
            self.axe = result["axe"]
        return axe_for_model(result)

    @docstring_with(max_page_items=ACCESSIBILITY.max_page_items)
    def get_page_content_tool(self, url: str):
        """Loads a web page in a headless browser and returns what a reader sees: its title, the lang attribute of
        the page, the start of its visible main text, its headings in order with the start of the text under each,
        its visible images and links, and the parts of the page that have their own lang attribute, with their text.
        Each image comes with its selector, src, alt text, the text of the link it is in and the text around it;
        each link with its selector, text, URL, aria-label and title attributes and the text around it. Both also
        give the heading of the section they are in. A passage in another language that is not listed in parts has
        no lang attribute of its own. Up to {max_page_items} images, links and parts are listed. Empty fields are
        left out: an image with no alt attribute has no alt field, while a decorative image has an empty one. The
        final URL is only given when the page redirected, and an error only when the page could not be loaded.

        Args:
            url: The URL of the web page to read.
        """
        result = get_page_content(url)
        if result["content"] is not None:
            self.content = result["content"]
        return content_for_model(result)

    def tools(self):
        return [Tool(self.run_axe_tool, name="run_axe"), Tool(self.get_page_content_tool, name="get_page_content")]

    def review(self, report):
        *_, unverified = check_report(report, self.axe, self.content)
        return unverified

    def reject(self, unverified):
        return unbacked_message(unverified)

    def agent_report(self, submitted):
        assessments, reviews, agent_findings, unverified = check_report(submitted, self.axe, self.content)
        findings = ((axe_report_findings(self.axe, assessments, reviews) if self.axe is not None else [])
                    + [agent_report_finding(f) for f in agent_findings])
        return findings, dict(axe_version=self.axe_version(), unverified=unverified)

    # Workflow

    def collect(self):
        axe_result = run_axe(self.args.url)
        content_result = get_page_content(self.args.url)
        for tool_name, result in (("run_axe", axe_result), ("get_page_content", content_result)):
            if result["error"]:
                print(f"====={tool_name} could not load {self.args.url}: {result['error']}=====")
        self.axe, self.content = axe_result["axe"], content_result["content"]
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
            add("page-title", 'The id "title" is the page, with its title, first headings and the start of its '
                              'text.', [
                ("title", {"title": content["title"],
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
            add("heading", "Each id is a heading.", [
                (element_id, item_for_model(without(heading, "selector"), "text"),
                 {"selector": heading["selector"]})
                for element_id, heading in with_ids(content["headings"], "h")],
                batched=False)

    def workflow_report(self, questions):
        findings, verdicts = build_findings(self.axe, questions.elements, questions.answers.by_id,
                                            questions.answers.omitted)
        return findings, verdicts, dict(axe_version=self.axe_version())
