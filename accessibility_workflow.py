import time
from collections import Counter
from datetime import datetime, timezone
from functools import partial
from pydantic import BaseModel
from accessibility_analyzer import (MAX_TEXT_CHARS, WCAG_FOR_RULE, Finding, axe_selector, check_messages,
                                    get_page_content, item_for_model, make_axe_parser, run_axe, save_axe,
                                    wcag_tags)
from common import (VERDICT_SEVERITY, Answers, Note, WorkflowReport, add_task, ask_tasks, criteria_counts,
                    make_judge, parse_args, report_fields, report_path, with_ids, without)

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

VIOLATIONS = """axe-core has confirmed these accessibility violations, so don't judge them again. For each id,
write a note explaining the problem and who it affects in one or two sentences, and suggest a fix if you can tell.
Only suggest specific colours if you know their contrast ratio meets the requirement."""

NEEDS_REVIEW = """Criterion: the axe-core rule named in each element. axe-core could not decide these elements.
Answer pass or fail only if the information here shows which; otherwise answer unsure, for example for a colour
contrast over an image, which depends on the pixels behind the text. Not being able to tell the colours is a
reason for unsure, not for fail."""

ALT_TEXT = """Criterion: alt-text (WCAG 1.1.1). Does each image's alt text say what the image adds to the page?
Look out for file names, "image" or "photo", and alt text that repeats the link text. An empty alt is right for
a decorative image, and for an image inside a link whose text already says where it goes. An image with no alt
field has no alt attribute at all, which fails."""

LINK_PURPOSE = """Criterion: link-purpose (WCAG 2.4.4). Can a user tell where each link goes? Every item is a
link, and its text is everything inside it, so a link around a card can include a name and a price. Most links
are clear from their own text alone, like a menu link that names a section or a link that names a topic: those
pass without any context. Only when the text alone is vague, like "click here" or "read more", do its context and
heading matter, and then the text around the link counts as explaining it. Judge only whether a user can tell
where the link goes, not whether the topic is explained there, and not whether the destination matches the text."""

REPEATED_LINKS = """Criterion: link-purpose (WCAG 2.4.4). Each id is a group of links with the same text that
lead to different places. Fail a group if its links' context and headings don't tell them apart."""

PAGE_TITLE = """Criterion: page-title (WCAG 2.4.2). Does the page title say what the page is about? Compare it
with the page's first headings and the start of its text."""

PAGE_LANGUAGE = """Criterion: page-language (WCAG 3.1.1 and 3.1.2). For the id "lang": does the page's lang
attribute match the language of its text, and is every passage in another language listed as one of the parts?
For each part: does its lang match the language of its text? Names of people and places don't need their own
lang. An element with no lang field has no lang attribute."""

HEADING = """Criterion: heading (WCAG 1.3.1 and 2.4.6). The headings are listed in page order. Does each heading
describe the content under it (section_text), and does its level fit where it sits in the outline? A heading with
no section_text that is followed by lower-level headings is fine: those subheadings are its content. A heading over
a list of links passes if it names what the links have in common. Skipped heading levels alone aren't a WCAG
failure."""

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
    # the questions to ask, and what the report keeps about each element, by id;
    # the criterion is a rule from WCAG_FOR_RULE, or axe-violations or axe-needs-review
    tasks = []
    elements = {}
    add = partial(add_task, tasks, elements)

    if axe is not None:
        add("axe-violations", VIOLATIONS, [
            (element_id, {"rule": rule["id"], "help": rule["help"], "elements_total": len(rule["nodes"]),
                          "examples": [{"html": node["html"][:300], "problems": check_messages(node)}
                                       for node in rule["nodes"][:3]]},
             {"rule": rule["id"]})
            for element_id, rule in with_ids(axe["violations"], "violation")], answer=Note)
        to_review = [(rule, node) for rule in axe["incomplete"] for node in rule["nodes"]]
        add("axe-needs-review", NEEDS_REVIEW, [
            (element_id, {"rule": rule["id"], "help": rule["help"], "html": node["html"][:300],
                          "problems": check_messages(node)},
             {"rule": rule["id"], "wcag": wcag_tags(rule), "selector": axe_selector(node),
              "html": node["html"][:300], "summary": node.get("failureSummary")})
            for element_id, (rule, node) in with_ids(to_review, "review")])

    if content is not None:
        add("alt-text", ALT_TEXT, [
            (element_id, item_for_model(without(image, "selector"), "link_text"), {"selector": image["selector"]})
            for element_id, image in with_ids(content["images"], "img")])
        # no destinations here: whether a link leads where it says is the link checker's job
        add("link-purpose", LINK_PURPOSE, [
            (element_id, item_for_model(without(link, "selector", "href"), "text"), {"selector": link["selector"]})
            for element_id, link in with_ids(content["links"], "link")])
        add("link-purpose", REPEATED_LINKS, [
            (element_id, group_for_model(group), {"selector": ", ".join(link["selector"] for link in group)})
            for element_id, group in with_ids(repeated_link_texts(content["links"]), "repeat")])
        add("page-title", PAGE_TITLE, [
            ("title", {"title": content["title"],
                       "headings": [heading["text"] for heading in content["headings"][:5]],
                       "text": content["text"][:500]},
             {"selector": None})])
        add("page-language", PAGE_LANGUAGE,
            [("lang", item_for_model({"lang": content["lang"], "text": content["text"][:MAX_TEXT_CHARS]}, "text"),
              {"selector": None})]
            + [(element_id, without(part, "selector"), {"selector": part["selector"]})
               for element_id, part in with_ids(content["parts"], "part")],
            batched=False)
        add("heading", HEADING, [
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
                    html=node["html"][:300],
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
