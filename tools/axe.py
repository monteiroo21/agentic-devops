from common import CONFIG
from tools.browser import page_fields, visit

ACCESSIBILITY = CONFIG.accessibility
AXE_JS = ACCESSIBILITY.axe_script.read_text()

async def axe_on(page):
    await page.add_script_tag(content=AXE_JS)
    return await page.evaluate(
        """tags => axe.run(document, {runOnly: {type: 'tag', values: tags}})""",
        ACCESSIBILITY.wcag_tags,
    )

async def run_axe(url):
    return await visit(url, "axe", axe_on)

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
