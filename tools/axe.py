from common import CONFIG
from tools.browser import page_fields, visit

ACCESSIBILITY = CONFIG.accessibility
AXE_JS = ACCESSIBILITY.axe_script.read_text()

RUN_AXE = ("Loads a web page in a headless browser and runs the axe-core automated accessibility checks for WCAG "
           "levels A and AA on it. Returns the page's status code, the rules the page violates and the rules axe "
           "could not decide, which need review, each with its impact, WCAG tags and up to "
           f"{ACCESSIBILITY.max_elements_per_rule} of the elements involved, and the ids of the rules that every "
           "element passed. The final URL is only given when the page redirected, and an error only when the page "
           "could not be checked.")

def run_axe(url):
    def collect(page):
        page.add_script_tag(content=AXE_JS)
        return page.evaluate(
            """tags => axe.run(document, {runOnly: {type: 'tag', values: tags}})""",
            ACCESSIBILITY.wcag_tags,
        )
    return visit(url, "axe", collect)

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

def rule_for_model(rule):
    return {
        "rule": rule["id"],
        "help": rule["help"],
        "impact": rule["impact"],
        "wcag": wcag_tags(rule),
        "elements_total": len(rule["nodes"]),
        "elements": [{
            "selector": axe_selector(node),
            "html": node["html"][:ACCESSIBILITY.max_html_chars],
            "problems": check_messages(node),
        } for node in rule["nodes"][:ACCESSIBILITY.max_elements_per_rule]],
    }

def axe_for_model(tool_result):
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
