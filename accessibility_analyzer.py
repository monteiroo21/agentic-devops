import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from common import (CONFIG, AgentReport, list_skills, make_parser, parse_args, report_fields, report_path, run_agent,
                    skills_prompt, split_backed)
from tools.axe import RUN_AXE, axe_for_model, axe_selector, run_axe, wcag_tags
from tools.page_content import GET_PAGE_CONTENT, content_for_model, get_page_content

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

def process_tool(tool_name, tool_input):
    if tool_name == "run_axe":
        return run_axe(tool_input["url"])
    elif tool_name == "get_page_content":
        return get_page_content(tool_input["url"])
    else:
        raise ValueError(f"Unknown tool: {tool_name}")

def for_model(tool_name, tool_result):
    return axe_for_model(tool_result) if tool_name == "run_axe" else content_for_model(tool_result)

def make_axe_parser(description, **options):
    parser = make_parser(description, **options)
    parser.add_argument("--output", type=Path, default=Path("reports/axe_raw.json"),
                        help="where to save axe's raw results (default reports/axe_raw.json)")
    return parser

def save_axe(axe, args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(axe, indent=2))

def main():
    args = parse_args(make_axe_parser("Find accessibility problems on a web page."))

    system_prompt = """
        You are a web accessibility analyzer. Your goal is to find the accessibility problems on the page the user
        gives you, measured against WCAG 2.2 levels A and AA, including the ones an automated checker cannot judge.

        Start by calling run_axe and get_page_content on the URL. You can call both at once.

        Judge run_axe's results with the axe-results skill below. axe only checks that things exist, not that they
        make sense, so also judge what get_page_content returns with each of the other skills, and give each
        problem you find the name of its skill as its rule.

        Only report what breaks a criterion, not what could merely be better. Only report what the tools showed
        you, and refer to elements by their selector exactly as the tools returned it. Not being able to check
        something is a reason for manual-review, never for fail. You do not check whether links work or whether a
        link's destination matches its text.
        When you have finished, hand in your report with submit_report: your comments on violations go in
        assessments, your decisions on needs_review items in reviews, and the problems you found in agent_findings.

        # Skills

    """ + skills_prompt(SKILLS)

    # What the tools found, filled in during the run: the latest successful result of each
    axe = None
    content = None

    def run_tool(tool_name, tool_input):
        nonlocal axe, content
        tool_result = process_tool(tool_name, tool_input)
        if tool_name == "run_axe" and tool_result["axe"] is not None:
            axe = tool_result["axe"]
        elif tool_name == "get_page_content" and tool_result["content"] is not None:
            content = tool_result["content"]
        return for_model(tool_name, tool_result)

    def run_axe_tool(url: str):
        """
        Args:
            url: The URL of the web page to check.
        """
        return run_tool("run_axe", {"url": url})

    def get_page_content_tool(url: str):
        """
        Args:
            url: The URL of the web page to read.
        """
        return run_tool("get_page_content", {"url": url})

    tools = [Tool(run_axe_tool, name="run_axe", description=RUN_AXE),
             Tool(get_page_content_tool, name="get_page_content", description=GET_PAGE_CONTENT)]

    def review(report):
        *_, unverified = check_report(report, axe, content)
        return unverified

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    submitted, usage = run_agent(args, system_prompt, f"Check the accessibility of {args.url}", tools, SubmitReport,
                                 review, unbacked_message)

    if axe is not None:
        save_axe(axe, args)

    if submitted is None:
        return

    assessments, reviews, agent_findings, unverified = check_report(submitted, axe, content)
    findings = ((axe_report_findings(axe, assessments, reviews) if axe is not None else [])
                + [agent_report_finding(f) for f in agent_findings])
    report = Report(
        **report_fields(args, started_at, start, usage),
        counts=dict(Counter(f.severity for f in findings)),
        narrative=submitted.narrative,
        rejected_submissions=usage["rejected_submissions"],
        unbacked_claims=usage["unbacked_claims"],
        text_replies=usage["text_replies"],
        axe_version=axe["testEngine"]["version"] if axe is not None else None,
        findings=findings,
        unverified=unverified,
    )
    path = report_path("accessibility", args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()