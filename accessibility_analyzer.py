import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

AXE_JS = Path("node_modules/axe-core/axe.min.js").read_text()
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
MAX_ELEMENTS_PER_RULE = 10
MAX_PAGE_ITEMS = 50
MAX_TEXT_CHARS = 2000

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
            headings.push({level: Number(el.localName[1]), text: clean(el.innerText)});
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
        }
    ]

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
    content = process_tool("get_page_content", {"url": args.url})
    print(f"=====Result of get_page_content=====\n"
          f"{json.dumps(for_model('get_page_content', content), indent=2, ensure_ascii=False)}")

if __name__ == "__main__":
    main()