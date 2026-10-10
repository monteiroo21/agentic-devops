import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

AXE_JS = Path("node_modules/axe-core/axe.min.js").read_text()
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
MAX_ELEMENTS_PER_RULE = 10

def run_axe(url):
    result = {"url": url, "final_url": None, "status": None, "error": None, "axe": None}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            response = page.goto(url, wait_until="networkidle")
            page.add_script_tag(content=AXE_JS)
            result["axe"] = page.evaluate(
                """tags => axe.run(document, {runOnly: {type: 'tag', values: tags}})""",
                WCAG_TAGS,
            )
            result.update(final_url=page.url, status=response.status if response else None)
            browser.close()
    except Exception as e:
        message = str(e)
        result["error"] = message.splitlines()[0] if message else type(e).__name__
    return result

def axe_findings(results, page_url):
    findings = []
    for category, severity in (("violations", "error"), ("incomplete", "needs-review")):
        for rule in results[category]:
            for node in rule["nodes"]:
                findings.append({
                    "rule": rule["id"],
                    "severity": severity,
                    "impact": rule.get("impact"),
                    "wcag": [t for t in rule["tags"] if t.startswith("wcag")],
                    "page": page_url,
                    "selector": node["target"],
                    "html": node["html"][:300],
                    "summary": node.get("failureSummary"),
                    "source": "axe",
                })
    return findings

def rule_for_model(rule):
    # one entry per rule rather than per element, since elements that break the same rule repeat its details
    return {
        "rule": rule["id"],
        "help": rule["help"],
        "impact": rule["impact"],
        "wcag": [t for t in rule["tags"] if t.startswith("wcag")],
        "elements_total": len(rule["nodes"]),
        "elements": [{
            "selector": node["target"],
            "html": node["html"][:300],
            "problems": [check["message"] for check in node["any"] + node["all"] + node["none"]],
        } for node in rule["nodes"][:MAX_ELEMENTS_PER_RULE]],
    }

def for_model(tool_name, tool_result):
    if tool_name == "run_axe":
        # the raw axe results run to thousands of lines, mostly rules that passed or had nothing to check
        kept = {key: value for key, value in tool_result.items() if key != "axe" and value is not None}
        if kept.get("final_url") == tool_result["url"]:
            kept.pop("final_url")
        axe = tool_result["axe"]
        if axe is None:
            return kept
        # axe lists a rule under passes if any element passed it, even when others failed
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
    else:
        raise ValueError(f"Unknown tool: {tool_name}")

def parse_args():
    parser = argparse.ArgumentParser(description="Check a web page's accessibility with axe-core.")
    parser.add_argument("url", help="the page to check")
    parser.add_argument("--output", type=Path, default=Path("reports/axe_raw.json"),
                        help="where to save axe's raw results (default reports/axe_raw.json)")
    args = parser.parse_args()
    if urlparse(args.url).scheme not in ("http", "https"):
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
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
        }
    ]

    # Until the agent loop is in place, run the tool directly and show what the model would get back
    result = process_tool("run_axe", {"url": args.url})
    if result["error"]:
        print(f"Could not check {args.url}: {result['error']}")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result["axe"], indent=2))
    findings = axe_findings(result["axe"], args.url)
    print(f"axe {result['axe']['testEngine']['version']}: "
          f"{len(result['axe']['violations'])} violated rules, "
          f"{len(result['axe']['incomplete'])} needing review, {len(findings)} element findings")
    print(f"=====Result of run_axe=====\n{json.dumps(for_model('run_axe', result), indent=2, ensure_ascii=False)}")

if __name__ == "__main__":
    main()