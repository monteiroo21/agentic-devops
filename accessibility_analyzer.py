import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlparse
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, ValidationError
from chat_backends import GeminiChat, OllamaChat

load_dotenv()

AXE_JS = Path("node_modules/axe-core/axe.min.js").read_text()
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
MAX_ELEMENTS_PER_RULE = 10
MAX_PAGE_ITEMS = 50
MAX_TEXT_CHARS = 2000

class Assessment(BaseModel):
    rule: str
    note: str
    suggested_fix: str | None = None

class Review(BaseModel):
    rule: str
    selector: str
    verdict: Literal["pass", "fail", "manual-review"]
    evidence: str
    note: str

AgentRule = Literal["alt-text", "link-purpose", "page-title", "page-language", "heading"]
# same tag format as axe's, so the report can group both by success criterion
WCAG_FOR_RULE = {"alt-text": ["wcag111"], "link-purpose": ["wcag244"], "page-title": ["wcag242"],
                 "page-language": ["wcag311", "wcag312"], "heading": ["wcag131", "wcag246"]}
# where get_page_content lists the elements each rule is about; page-title is about the whole page
ELEMENT_LISTS = {"alt-text": ("images", "image"), "link-purpose": ("links", "link"),
                 "heading": ("headings", "heading"), "page-language": ("parts", "part")}
NEEDS_SELECTOR = {"alt-text", "link-purpose"}

class AgentFinding(BaseModel):
    rule: AgentRule
    selector: str | None = None  # None for a problem with the page as a whole
    evidence: str
    note: str
    suggested_fix: str | None = None

class SubmitReport(BaseModel):
    narrative: str
    assessments: list[Assessment] = []
    reviews: list[Review] = []
    agent_findings: list[AgentFinding] = []

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

class Report(BaseModel):
    target: str
    model: str
    thinking: bool = True  # False for local runs with --no-think
    started_at: str
    duration_s: float
    llm_requests: int
    input_tokens: int
    output_tokens: int
    axe_version: str | None
    counts: dict[str, int]  # findings per severity
    narrative: str
    findings: list[Finding]
    unverified: list[dict] = []  # submitted items no tool result backs, kept after the last rejection
    rejected_submissions: int = 0  # times the agent submitted items it hadn't checked
    unbacked_claims: int = 0  # total unbacked items across those submissions
    text_replies: int = 0  # times the model wrote text instead of calling a tool

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

def axe_selector(node):
    # axe gives a list, one selector per frame or shadow root; a single string is easier for the model to repeat
    return " ".join(str(part) for part in node["target"])

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
    kind, noun = ELEMENT_LISTS[finding.rule]
    if finding.selector is None:
        return f"give the selector of the {noun}" if finding.rule in NEEDS_SELECTOR else None
    if finding.selector not in {item["selector"] for item in content[kind]}:
        return f"get_page_content returned no {noun} with that selector"
    return None

def split_backed(items, kind, problem_of):
    backed, unverified = [], []
    for item in items:
        problem = problem_of(item)
        if problem:
            unverified.append({"kind": kind, **item.model_dump(), "reason": problem})
        else:
            backed.append(item)
    return backed, unverified

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
            "problems": [check["message"] for check in node["any"] + node["all"] + node["none"]],
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
            "headings": content["headings"],
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
    elif tool_name == "submit_report":
        try:
            return SubmitReport.model_validate(tool_input)
        except ValidationError as e:
            return {"error": f"Invalid report, fix it and call submit_report again: {e}"}
    else:
        raise ValueError(f"Unknown tool: {tool_name}")

DEFAULT_MODELS = {"gemini": "gemini-3.1-flash-lite", "ollama": "qwen3:4b"}
DEFAULT_MAX_OUTPUT_TOKENS = 4096

def parse_args():
    parser = argparse.ArgumentParser(description="Find accessibility problems on a web page.")
    parser.add_argument("url", help="the page to check")
    parser.add_argument("--output", type=Path, default=Path("reports/axe_raw.json"),
                        help="where to save axe's raw results (default reports/axe_raw.json)")
    parser.add_argument("--backend", choices=DEFAULT_MODELS, default="gemini",
                        help="gemini (Google's API) or ollama (a local model)")
    parser.add_argument("--model", help="the model to use; defaults to "
                        + ", ".join(f"{model} for {backend}" for backend, model in DEFAULT_MODELS.items()))
    parser.add_argument("--no-think", action="store_true",
                        help="ollama only: answer without thinking first, which is much faster on a CPU")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS,
                        help="ollama only: the most tokens the model may write per turn, thinking included "
                             f"(default {DEFAULT_MAX_OUTPUT_TOKENS})")
    args = parser.parse_args()
    if urlparse(args.url).scheme not in ("http", "https"):
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
    if args.backend == "gemini" and (args.no_think or args.max_output_tokens != DEFAULT_MAX_OUTPUT_TOKENS):
        parser.error("--no-think and --max-output-tokens only work with --backend ollama")
    return args

def main():
    args = parse_args()
    
    tools = [
        {
            "name": "run_axe",
            "description": "Loads a web page in a headless browser and runs the axe-core automated accessibility "
                           "checks for WCAG levels A and AA on it. Returns the page's status code, the rules the "
                           "page violates and the rules axe could not decide, which need review, each with its "
                           f"impact, WCAG tags and up to {MAX_ELEMENTS_PER_RULE} of the elements involved, and the "
                           "ids of the rules that every element passed. The final URL is only given when the page redirected, "
                           "and an error only when the page could not be checked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the web page to check."
                    }
                },
                "required": ["url"]
            }
        },
        {
            "name": "get_page_content",
            "description": "Loads a web page in a headless browser and returns what a reader sees: its title, the "
                           "lang attribute of the page, the start of its visible main text, its headings in order, "
                           "its visible images and links, and the parts of the page that have their own lang "
                           "attribute, with their text. Each image comes with its selector, src, alt text, "
                           "the text of the link it is in and the text around it; each link with its selector, "
                           "text, URL, aria-label and title attributes and the text around it. Both also give the "
                           "heading of the section they are in. A passage in another language that is not listed "
                           f"in parts has no lang attribute of its own. Up to {MAX_PAGE_ITEMS} images, links and "
                           "parts are listed. Empty fields are left out: an image with no alt attribute has no alt field, "
                           "while a decorative image has an empty one. The final URL is only given when the page "
                           "redirected, and an error only when the page could not be loaded.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the web page to read."
                    }
                },
                "required": ["url"]
            }
        },
        {
            "name": "submit_report",
            "description": "Hand in the final report. Call this once, after you have finished investigating.",
            "parameters": {
                "type": "object",
                "properties": {
                    "narrative": {
                        "type": "string",
                        "description": "A 2-4 sentence summary of the page's accessibility. Only mention things the tools showed."
                    },
                    "assessments": {
                        "type": "array",
                        "description": "One entry per rule in run_axe's violations. Empty if there are none.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rule": {"type": "string", "description": "The axe rule, exactly as run_axe returned it."},
                                "note": {"type": "string", "description": "One or two sentences explaining the problem and who it affects."},
                                "suggested_fix": {"type": "string", "description": "How to fix it, if you can tell."}
                            },
                            "required": ["rule", "note"]
                        }
                    },
                    "reviews": {
                        "type": "array",
                        "description": "One entry per element in run_axe's needs_review. Empty if there are none.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rule": {"type": "string", "description": "The axe rule, exactly as run_axe returned it."},
                                "selector": {"type": "string", "description": "The element's selector, exactly as run_axe returned it."},
                                "verdict": {"type": "string", "enum": ["pass", "fail", "manual-review"],
                                            "description": "pass or fail if the tool results show which, otherwise manual-review."},
                                "evidence": {"type": "string", "description": "What in the tool results supports the verdict, or what is missing to decide."},
                                "note": {"type": "string", "description": "One or two sentences explaining the verdict."}
                            },
                            "required": ["rule", "selector", "verdict", "evidence", "note"]
                        }
                    },
                    "agent_findings": {
                        "type": "array",
                        "description": "Problems you found with get_page_content that axe does not report. Empty if there are none.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rule": {"type": "string", "enum": list(get_args(AgentRule)), "description": "The kind of problem."},
                                "selector": {"type": "string", "description": "The element with the problem, exactly as get_page_content "
                                                                              "returned it. Leave it out for the page title, or for a "
                                                                              "problem with the page as a whole."},
                                "evidence": {"type": "string", "description": "What in the tool results shows the problem, e.g. the alt "
                                                                              "text, or a link's text and its context."},
                                "note": {"type": "string", "description": "One or two sentences explaining the problem and who it affects."},
                                "suggested_fix": {"type": "string", "description": "How to fix it, if you can tell."}
                            },
                            "required": ["rule", "evidence", "note"]
                        }
                    }
                },
                "required": ["narrative", "assessments", "reviews", "agent_findings"]
            }
        }
    ]

    system_prompt = """
        You are a web accessibility analyzer. Your goal is to find the accessibility problems on the page the user
        gives you, measured against WCAG 2.2 levels A and AA, including the ones an automated checker cannot judge.

        Start by calling run_axe and get_page_content on the URL. You can call both at once.

        run_axe gives you axe-core's results. Its violations are already confirmed: do not judge them again, just
        explain each one and suggest a fix. Its needs_review items are ones axe could not decide. Judge them only
        if the tools show you what you need. If they don't, for example a colour contrast over an image, which
        depends on the pixels behind the text, say they need manual review instead of guessing.

        axe only checks that things exist, not that they make sense. Use get_page_content to judge:
        - alt-text (1.1.1): does each alt text say what the image adds? Look out for file names, "image" or
          "photo", and alt text that repeats the link text. An empty alt is right for a decorative image, and for
          an image inside a link whose text already says where it goes.
        - link-purpose (2.4.4): can a user tell where each link goes from its text together with its context and
          heading? Look out for "click here" or "read more" with nothing around them to explain, and the same text
          leading to different places.
        - page-title (2.4.2): does the title say what the page is about?
        - page-language (3.1.1, 3.1.2): does lang match the language of the text, and is every passage in another
          language listed in parts with the right lang? Names of people and places don't need their own lang.
        - heading (1.3.1, 2.4.6): do the headings describe the sections under them, and do their levels show how
          the page is organised?

        Only report what the tools showed you, and refer to elements by their selector exactly as the tools
        returned it. You do not check whether links work or whether a link's destination matches its text.
        When you have finished, hand in your report with submit_report: your comments on violations go in
        assessments, your decisions on needs_review items in reviews, and the problems you found in agent_findings.
    """

    model = args.model or DEFAULT_MODELS[args.backend]
    if args.backend == "ollama":
        chat = OllamaChat(model, system_prompt, tools, think=not args.no_think,
                          max_output_tokens=args.max_output_tokens)
    else:
        chat = GeminiChat(model, system_prompt, tools)

    chat.add_user_message(f"Check the accessibility of {args.url}")

    # What the tools found, filled in during the run: the latest successful result of each
    axe = None
    content = None
    submitted = None

    # Metrics for the report
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    llm_requests = 0
    input_tokens = 0
    output_tokens = 0

    MAX_REJECTIONS = 2
    rejected_submissions = 0
    unbacked_claims = 0

    MAX_TEXT_REPLIES = 2
    text_replies = 0

    while True:
        turn = chat.generate()

        if turn.thoughts:
            print(f"\n====={chat.model}'s thoughts=====\n{turn.thoughts}\n")
        if turn.text:
            print(f"\n====={chat.model} says=====\n{turn.text}\n")

        llm_requests += 1
        input_tokens += turn.input_tokens
        output_tokens += turn.output_tokens

        if turn.truncated:
            print(f"====={chat.model} was cut off at the output token limit=====")

        if not turn.tool_calls:
            if text_replies >= MAX_TEXT_REPLIES:
                print(f"====={chat.model} has no more function calls=====")
                break
            # Local models can't be forced to call a tool, and small ones sometimes write the report as text
            text_replies += 1
            print(f"=====No tool call, asking {chat.model} to call a tool=====")
            chat.add_user_message("You replied with text, but only tool calls are acted on. If you have finished, "
                                  "hand in your report by calling the submit_report tool; otherwise call the tool "
                                  "you need next. Do not write the report as text.")
            continue

        # Answer every tool call in this response, not just the last one
        results = []
        for call in sorted(turn.tool_calls, key=lambda call: call.name == "submit_report"):
            tool_name = call.name
            print(f"====={chat.model} wants to use the {tool_name} tool=====")

            tool_result = process_tool(tool_name, call.args)

            if isinstance(tool_result, SubmitReport):
                *_, unverified = check_report(tool_result, axe, content)
                if not unverified or rejected_submissions >= MAX_REJECTIONS:
                    submitted = tool_result
                    continue
                # Send the report back: the agent must look before it claims
                rejected_submissions += 1
                unbacked_claims += len(unverified)
                print(f"=====Report rejected: {len(unverified)} unbacked item(s)=====")
                tool_result = {"error": unbacked_message(unverified)}
            elif tool_name == "run_axe" and tool_result["axe"] is not None:
                axe = tool_result["axe"]
            elif tool_name == "get_page_content" and tool_result["content"] is not None:
                content = tool_result["content"]

            model_result = for_model(tool_name, tool_result)
            print(f"=====Result of {tool_name}=====\n{json.dumps(model_result, indent=2, ensure_ascii=False)}\n")
            results.append((call, model_result))

        if submitted is not None:
            print(f"====={chat.model} has submitted the final report=====")
            break

        chat.add_tool_results(results)

    if axe is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(axe, indent=2))

    if submitted is None:
        print("=====No valid report was submitted=====")
        return

    assessments, reviews, agent_findings, unverified = check_report(submitted, axe, content)
    findings = ((axe_report_findings(axe, assessments, reviews) if axe is not None else [])
                + [agent_report_finding(f) for f in agent_findings])
    report = Report(
        target=args.url,
        model=chat.model,
        thinking=not args.no_think,
        started_at=started_at,
        duration_s=round(time.perf_counter() - start, 1),
        llm_requests=llm_requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        axe_version=axe["testEngine"]["version"] if axe is not None else None,
        counts=dict(Counter(f.severity for f in findings)),
        narrative=submitted.narrative,
        findings=findings,
        unverified=unverified,
        rejected_submissions=rejected_submissions,
        unbacked_claims=unbacked_claims,
        text_replies=text_replies,
    )
    report_name = (chat.model.replace(':', '-').replace('/', '-') + ("_no-think" if args.no_think else "")
                   + "_" + urlparse(args.url).netloc.replace(".", "-")
                   + "_" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    report_path = Path("reports") / f"accessibility_{report_name}.json"
    report_path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {report_path}=====")

if __name__ == "__main__":
    main()