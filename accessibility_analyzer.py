import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from common import (AgentReport, list_skills, make_parser, parse_args, report_fields, report_path, run_agent,
                    skills_prompt, split_backed)

AXE_JS = Path("node_modules/axe-core/axe.min.js").read_text()
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
MAX_ELEMENTS_PER_RULE = 10
MAX_PAGE_ITEMS = 50
MAX_TEXT_CHARS = 2000

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

SKILLS = list_skills("accessibility")
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

# Runs in the page: what a reader sees, for the judgements axe can't make
PAGE_CONTENT_JS = r"""() => {
    const clean = s => (s || "").replace(/\s+/g, " ").trim();
    const visible = el => el.getClientRects().length > 0;
    const selector = el => {
        const parts = [];
        for (; el && el !== document.documentElement; el = el.parentElement) {
            if (el.id) { parts.unshift("#" + CSS.escape(el.id)); break; }
            const sameTag = [...el.parentElement.children].filter(c => c.localName === el.localName);
            parts.unshift(sameTag.length > 1 ? `${el.localName}:nth-of-type(${sameTag.indexOf(el) + 1})` : el.localName);
        }
        return parts.join(" > ");
    };
    // the sentence-level context WCAG 2.4.4 allows: the paragraph, list item or table cell around the element
    const context = el => {
        const block = el.parentElement.closest("p, li, td, th, dd, figure, blockquote");
        return block ? clean(block.innerText).slice(0, 300) : null;
    };
    const seen = [];  // the headings so far, in document order
    // the last heading before the element whose parent also contains it, so a card's
    // heading doesn't carry over to the next card
    const headingFor = el => {
        for (let i = seen.length - 1; i >= 0; i--) {
            if (seen[i].parentElement.contains(el)) return clean(seen[i].innerText);
        }
        return null;
    };
    const headings = [], images = [], links = [];
    for (const el of document.querySelectorAll("h1, h2, h3, h4, h5, h6, img, a[href]")) {
        if (!visible(el)) continue;
        if (/^h\d$/.test(el.localName)) {
            seen.push(el);
            headings.push({selector: selector(el), level: Number(el.localName[1]), text: clean(el.innerText)});
        } else if (el.localName === "img") {
            const link = el.closest("a[href]");
            images.push({selector: selector(el), src: el.getAttribute("src"), alt: el.getAttribute("alt"),
                         link_text: link ? clean(link.innerText) : null, context: context(el),
                         heading: headingFor(el)});
        } else {
            links.push({selector: selector(el), text: clean(el.innerText), href: el.href,
                        aria_label: el.getAttribute("aria-label"), title: el.getAttribute("title"),
                        context: context(el), heading: headingFor(el)});
        }
    }
    // the start of the text under each heading, up to the next one, to judge whether the heading describes it
    const sectionText = seen.map(() => "");
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let current = -1;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        while (current + 1 < seen.length
               && seen[current + 1].compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) current++;
        const parent = node.parentElement;
        if (current < 0 || seen[current].contains(node) || sectionText[current].length >= 200
            || !visible(parent) || parent.closest("script, style, noscript, template")) continue;
        sectionText[current] = clean(sectionText[current] + " " + node.textContent);
    }
    headings.forEach((heading, i) => { heading.section_text = sectionText[i].slice(0, 200) || null; });
    // passages marked with their own language (WCAG 3.1.2); plain text alone can't show whether a passage is marked
    const parts = [...document.body.querySelectorAll("[lang]")].filter(visible).map(el => (
        {selector: selector(el), lang: el.getAttribute("lang"), text: clean(el.innerText).slice(0, 300)}));
    return {
        title: document.title,
        lang: document.documentElement.getAttribute("lang"),
        text: clean((document.querySelector("main") || document.body).innerText),
        headings, images, links, parts,
    };
}"""

def visit(url, key, collect):
    result = {"url": url, "final_url": None, "status": None, "error": None, key: None}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            response = page.goto(url, wait_until="networkidle")
            result[key] = collect(page)
            result.update(final_url=page.url, status=response.status if response else None)
            browser.close()
    except Exception as e:
        message = str(e)
        result["error"] = message.splitlines()[0] if message else type(e).__name__
    return result

def run_axe(url):
    def collect(page):
        page.add_script_tag(content=AXE_JS)
        return page.evaluate(
            """tags => axe.run(document, {runOnly: {type: 'tag', values: tags}})""",
            WCAG_TAGS,
        )
    return visit(url, "axe", collect)

def get_page_content(url):
    return visit(url, "content", lambda page: page.evaluate(PAGE_CONTENT_JS))

def wcag_tags(rule):
    return [t for t in rule["tags"] if t.startswith("wcag")]

def wcag_number(tag):
    digits = tag.removeprefix("wcag")
    return f"{digits[0]}.{digits[1]}.{digits[2:]}"

def axe_selector(node):
    # axe gives a list, one selector per frame or shadow root; a single string is easier for the model to repeat
    return " ".join(str(part) for part in node["target"])

def check_messages(node):
    return [check["message"] for check in node["any"] + node["all"] + node["none"]]

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
                html=node["html"][:300],
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
                html=node["html"][:300],
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

def rule_for_model(rule):
    return {
        "rule": rule["id"],
        "help": rule["help"],
        "impact": rule["impact"],
        "wcag": wcag_tags(rule),
        "elements_total": len(rule["nodes"]),
        "elements": [{
            "selector": axe_selector(node),
            "html": node["html"][:300],
            "problems": check_messages(node),
        } for node in rule["nodes"][:MAX_ELEMENTS_PER_RULE]],
    }

def page_fields(tool_result, data_key):
    kept = {key: value for key, value in tool_result.items() if key != data_key and value is not None}
    if kept.get("final_url") == tool_result["url"]:
        kept.pop("final_url")
    return kept

def item_for_model(item, own_text):
    # leaves out empty fields, and the context when it is just the item's own text
    kept = {key: value for key, value in item.items() if value is not None}
    if kept.get("context") == item.get(own_text):
        kept.pop("context", None)
    return kept

def for_model(tool_name, tool_result):
    if tool_name == "get_page_content":
        kept = page_fields(tool_result, "content")
        content = tool_result["content"]
        if content is None:
            return kept
        text = content["text"]
        return {
            **kept,
            **{key: content[key] for key in ("title", "lang") if content[key] is not None},
            "text": text[:MAX_TEXT_CHARS],
            **({"truncated": True} if len(text) > MAX_TEXT_CHARS else {}),
            "headings": [item_for_model(heading, "text") for heading in content["headings"]],
            "images_total": len(content["images"]),
            "images": [item_for_model(image, "link_text") for image in content["images"][:MAX_PAGE_ITEMS]],
            "links_total": len(content["links"]),
            "links": [item_for_model(link, "text") for link in content["links"][:MAX_PAGE_ITEMS]],
            "parts_total": len(content["parts"]),
            "parts": content["parts"][:MAX_PAGE_ITEMS],
        }
    if tool_name == "run_axe":
        kept = page_fields(tool_result, "axe")
        axe = tool_result["axe"]
        if axe is None:
            return kept
        problem_rules = {rule["id"] for rule in axe["violations"] + axe["incomplete"]}
        return {
            **kept,
            "violations": [rule_for_model(rule) for rule in axe["violations"]],
            "needs_review": [rule_for_model(rule) for rule in axe["incomplete"]],
            "passed_rules": [rule["id"] for rule in axe["passes"] if rule["id"] not in problem_rules],
        }
    return tool_result

def process_tool(tool_name, tool_input):
    if tool_name == "run_axe":
        return run_axe(tool_input["url"])
    elif tool_name == "get_page_content":
        return get_page_content(tool_input["url"])
    else:
        raise ValueError(f"Unknown tool: {tool_name}")

def make_axe_parser(description, **options):
    parser = make_parser(description, **options)
    parser.add_argument("--output", type=Path, default=Path("reports/axe_raw.json"),
                        help="where to save axe's raw results (default reports/axe_raw.json)")
    return parser

def save_axe(axe, args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(axe, indent=2))

RUN_AXE = ("Loads a web page in a headless browser and runs the axe-core automated accessibility checks for WCAG "
           "levels A and AA on it. Returns the page's status code, the rules the page violates and the rules axe "
           "could not decide, which need review, each with its impact, WCAG tags and up to "
           f"{MAX_ELEMENTS_PER_RULE} of the elements involved, and the ids of the rules that every element passed. "
           "The final URL is only given when the page redirected, and an error only when the page could not be "
           "checked.")

GET_PAGE_CONTENT = ("Loads a web page in a headless browser and returns what a reader sees: its title, the lang "
                    "attribute of the page, the start of its visible main text, its headings in order with the start "
                    "of the text under each, its visible images and links, and the parts of the page that have their "
                    "own lang attribute, with their text. Each image comes with its selector, src, alt text, the text "
                    "of the link it is in and the text around it; each link with its selector, text, URL, aria-label "
                    "and title attributes and the text around it. Both also give the heading of the section they are "
                    "in. A passage in another language that is not listed in parts has no lang attribute of its own. "
                    f"Up to {MAX_PAGE_ITEMS} images, links and parts are listed. Empty fields are left out: an image "
                    "with no alt attribute has no alt field, while a decorative image has an empty one. The final URL "
                    "is only given when the page redirected, and an error only when the page could not be loaded.")

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