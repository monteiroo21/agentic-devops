import time
from collections import Counter
from datetime import datetime, timezone
from pydantic import BaseModel
from accessibility_analyzer import ACCESSIBILITY, SKILLS, WCAG_FOR_RULE, Finding, make_axe_parser, save_axe
from common import (VERDICT_SEVERITY, Answers, Note, WorkflowReport, add_task, ask_tasks, criteria_counts,
                    make_judge, parse_args, report_fields, report_path, skill_body, with_ids, without)
from tools.axe import axe_selector, check_messages, run_axe, wcag_number, wcag_tags
from tools.page_content import get_page_content, item_for_model

SYSTEM_PROMPT = """
    You are a web accessibility reviewer. You judge one WCAG 2.2 criterion at a time for a few elements of a web
    page, using only the information you are given about them.

    For every element, give a short reason and then a verdict:
    - pass: it meets the criterion. A few words of reason are enough.
    - fail: the information shows that it does not. The reason must say what in the element shows the problem.
    - unsure: the information you were given is not enough to decide. If you can't check something, the answer
      is unsure, never fail.
    Only fail what breaks the criterion, not what could merely be better. Answer for every id you are given,
    and only for those.
"""

def instructions(skill_name, ids):
    skill = SKILLS[skill_name]
    criterion = ""
    if skill.values("wcag"):
        numbers = " and ".join(wcag_number(tag) for tag in skill.values("wcag"))
        criterion = f"Criterion: {skill.name} (WCAG {numbers}). "
    return f"{criterion}{ids}\n{skill_body(skill)}"

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

def build_tasks(axe, content):
    tasks = []
    elements = {}

    def add(criterion, ids, entries, skill=None, **options):
        add_task(tasks, elements, criterion, instructions(skill or criterion, ids), entries, **options)

    if axe is not None:
        html_chars = ACCESSIBILITY.max_html_chars
        add("axe-violations", "Each id is a rule that axe-core found violated, with a few of the elements involved. "
                              "Write a note for each id.", [
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
            (element_id, item_for_model(without(link, "selector", "href"), "text"), {"selector": link["selector"]})
            for element_id, link in with_ids(content["links"], "link")])
        add("link-purpose", "Each id is a group of links with the same text that lead to different places.", [
            (element_id, group_for_model(group), {"selector": ", ".join(link["selector"] for link in group)})
            for element_id, group in with_ids(repeated_link_texts(content["links"]), "repeat")])
        add("page-title", 'The id "title" is the page, with its title, first headings and the start of its text.', [
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
            (element_id, item_for_model(without(heading, "selector"), "text"), {"selector": heading["selector"]})
            for element_id, heading in with_ids(content["headings"], "h")],
            batched=False)
    return tasks, elements

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

def main():
    args = parse_args(make_axe_parser("Find accessibility problems on a web page, asking the model about one "
                                      "criterion and a few elements at a time.", workflow=True))
    judge = make_judge(args, SYSTEM_PROMPT)

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()

    axe_result = run_axe(args.url)
    content_result = get_page_content(args.url)
    for tool_name, result in (("run_axe", axe_result), ("get_page_content", content_result)):
        if result["error"]:
            print(f"====={tool_name} could not load {args.url}: {result['error']}=====")
    axe, content = axe_result["axe"], content_result["content"]
    if axe is None and content is None:
        return
    if axe is not None:
        save_axe(axe, args)

    tasks, elements = build_tasks(axe, content)
    usage = Counter()
    answers = Answers()
    ask_tasks(judge, tasks, args.batch_size, usage, answers)

    findings, verdicts = build_findings(axe, elements, answers.by_id, answers.omitted)
    report = AccessibilityWorkflowReport(
        **report_fields(args, started_at, start, usage),
        counts=dict(Counter(finding.severity for finding in findings)),
        invalid_replies=usage["invalid_replies"],
        batch_size=args.batch_size,
        criteria=criteria_counts(verdicts, answers.invented),
        axe_version=axe["testEngine"]["version"] if axe is not None else None,
        findings=findings,
        verdicts=verdicts,
    )
    path = report_path("accessibility-workflow", args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
