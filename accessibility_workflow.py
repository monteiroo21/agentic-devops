import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ValidationError
from accessibility_analyzer import (MAX_TEXT_CHARS, WCAG_FOR_RULE, Finding, axe_selector, check_messages,
                                    get_page_content, item_for_model, make_chat, make_parser, parse_args,
                                    report_path, run_axe, wcag_tags)

MAX_RETRIES = 2  # times a batch is asked again about the elements its answer left out

class Verdict(BaseModel):
    id: str
    reason: str
    verdict: Literal["pass", "fail", "unsure"]

class Note(BaseModel):
    id: str
    note: str
    suggested_fix: str | None = None

def answers_schema(key, properties, required):
    return {
        "type": "object",
        "properties": {
            key: {"type": "array", "items": {"type": "object", "properties": properties, "required": required}},
        },
        "required": [key],
    }

# the reason comes before the verdict, so the model explains before it decides
ANSWER_FORMATS = {
    Verdict: ("verdicts", answers_schema("verdicts", {
        "id": {"type": "string"},
        "reason": {"type": "string"},
        "verdict": {"type": "string", "enum": ["pass", "fail", "unsure"]},
    }, ["id", "reason", "verdict"])),
    Note: ("notes", answers_schema("notes", {
        "id": {"type": "string"},
        "note": {"type": "string"},
        "suggested_fix": {"type": "string"},
    }, ["id", "note"])),
}

SYSTEM_PROMPT = """
    You are a web accessibility reviewer. You judge one WCAG 2.2 criterion at a time for a few elements of a web
    page, using only the information you are given about them.

    For every element, give a short reason and then a verdict:
    - pass: it meets the criterion. A few words of reason are enough.
    - fail: it does not. The reason must say what in the element shows the problem.
    - unsure: the information you were given is not enough to decide.
    Only fail what breaks the criterion, not what could merely be better. Answer for every id you are given,
    and only for those.
"""

VIOLATIONS = """axe-core has confirmed these accessibility violations, so don't judge them again. For each id,
write a note explaining the problem and who it affects in one or two sentences, and suggest a fix if you can tell.
Only suggest specific colours if you know their contrast ratio meets the requirement."""

NEEDS_REVIEW = """Criterion: the axe-core rule named in each element. axe-core could not decide these elements.
Answer pass or fail only if the information here shows which; otherwise answer unsure, for example for a colour
contrast over an image, which depends on the pixels behind the text."""

ALT_TEXT = """Criterion: alt-text (WCAG 1.1.1). Does each image's alt text say what the image adds to the page?
Look out for file names, "image" or "photo", and alt text that repeats the link text. An empty alt is right for
a decorative image, and for an image inside a link whose text already says where it goes. An image with no alt
field has no alt attribute at all, which fails."""

LINK_PURPOSE = """Criterion: link-purpose (WCAG 2.4.4). Can a user tell where each link goes from its text,
together with its context and heading? The text around a link counts as explaining it. Look out for "click here"
or "read more" with nothing around them to explain. Don't judge whether the destination matches the text."""

REPEATED_LINKS = """Criterion: link-purpose (WCAG 2.4.4). Each id is a group of links with the same text that
lead to different places. Fail a group if its links' context and headings don't tell them apart."""

PAGE_TITLE = """Criterion: page-title (WCAG 2.4.2). Does the page title say what the page is about? Compare it
with the page's first headings and the start of its text."""

PAGE_LANGUAGE = """Criterion: page-language (WCAG 3.1.1 and 3.1.2). For the id "lang": does the page's lang
attribute match the language of its text, and is every passage in another language listed as one of the parts?
For each part: does its lang match the language of its text? Names of people and places don't need their own
lang. An element with no lang field has no lang attribute."""

HEADING = """Criterion: heading (WCAG 1.3.1 and 2.4.6). The headings are listed in page order. Does each heading
describe the text under it (section_text), and does its level fit where it sits in the outline? A heading may
have little text under it when its subheadings follow straight away."""

@dataclass
class Task:
    criterion: str  # a rule from WCAG_FOR_RULE, or axe-violations or axe-needs-review
    instructions: str
    items: list[dict]  # what the model sees about each element, with its id
    answer: type = Verdict
    batched: bool = True  # False when the model needs every item at once, like a heading outline

def with_ids(items, prefix):
    return [(f"{prefix}-{number}", item) for number, item in enumerate(items, 1)]

def without(item, *keys):
    return {key: value for key, value in item.items() if key not in keys}

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
    # the questions to ask, and what the report keeps about each element, by id
    tasks = []
    elements = {}

    def add(criterion, instructions, entries, **options):
        # entries are (id, what the model sees, what the report keeps)
        for element_id, _, kept in entries:
            elements[element_id] = {"criterion": criterion, **kept}
        if entries:
            tasks.append(Task(criterion, instructions,
                              [{"id": element_id, **shown} for element_id, shown, _ in entries], **options))

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

def batches(task, size):
    if not task.batched:
        return [task.items]
    return [task.items[start:start + size] for start in range(0, len(task.items), size)]

def ask(chat, task, items, usage):
    # asks about items until each has a valid answer or the retries run out
    key, schema = ANSWER_FORMATS[task.answer]
    asked = {item["id"] for item in items}
    answers, omitted, invented = {}, [], []
    pending = items
    for attempt in range(1 + MAX_RETRIES):
        if attempt:
            print(f"=====Asking again about {len(pending)} element(s) left out=====")
        reply = chat.ask_json(task.instructions + "\n\n" + json.dumps(pending, ensure_ascii=False, indent=1), schema)
        usage.update(requests=1, input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                     truncated_replies=int(reply.truncated))
        if reply.thoughts:
            print(f"====={chat.model}'s thoughts=====\n{reply.thoughts}\n")

        returned = reply.data.get(key) if isinstance(reply.data, dict) else None
        if not isinstance(returned, list):
            usage["invalid_replies"] += 1
            print(f"=====Not an answer in the expected format=====\n{reply.text}\n")
            returned = []
        for raw in returned:
            try:
                answer = task.answer.model_validate(raw)
            except ValidationError:
                continue  # an answer that doesn't follow the format counts as no answer
            if answer.id not in asked:
                invented.append(answer.id)
            elif answer.id not in answers:
                answers[answer.id] = answer
                print(f"{answer.id}: " + (f"{answer.verdict} | {answer.reason}" if isinstance(answer, Verdict)
                                          else answer.note))

        pending = [item for item in pending if item["id"] not in answers]
        if attempt == 0:
            omitted = [item["id"] for item in pending]
        if not pending:
            break
    return answers, omitted, invented

class ElementVerdict(BaseModel):
    criterion: str
    id: str
    selector: str | None
    verdict: str  # pass, fail, unsure, or unanswered when no valid answer came even after asking again
    reason: str | None = None
    omitted_first: bool = False  # left out of the model's first answer for its batch

class WorkflowReport(BaseModel):
    mode: str = "workflow"
    target: str
    model: str
    thinking: bool = True
    started_at: str
    duration_s: float
    llm_requests: int
    input_tokens: int
    output_tokens: int
    invalid_replies: int  # replies that weren't JSON in the expected format
    truncated_replies: int  # replies cut off at the output token limit
    batch_size: int
    axe_version: str | None
    counts: dict[str, int]  # findings per severity
    criteria: dict[str, dict[str, int]]  # per criterion: elements, each verdict, omitted_first, invented
    findings: list[Finding]
    verdicts: list[ElementVerdict]  # every element judged, passes included

VERDICT_SEVERITY = {"pass": "info", "fail": "warning", "unsure": "needs-review", "unanswered": "needs-review"}

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

def criteria_counts(verdicts, invented):
    counts = {}
    for verdict in verdicts:
        criterion = counts.setdefault(verdict.criterion, Counter())
        criterion["elements"] += 1
        criterion[verdict.verdict] += 1
        criterion["omitted_first"] += verdict.omitted_first
    for criterion, number in invented.items():
        if number:
            counts.setdefault(criterion, Counter())["invented"] += number
    return {criterion: dict(numbers) for criterion, numbers in counts.items()}

def main():
    parser = make_parser("Find accessibility problems on a web page, asking the model about one criterion "
                         "and a few elements at a time.")
    parser.add_argument("--batch-size", type=int, default=8,
                        help="how many elements the model judges in one call (default 8)")
    args = parse_args(parser)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    chat = make_chat(args, SYSTEM_PROMPT, tools=[])

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
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(axe, indent=2))

    tasks, elements = build_tasks(axe, content)
    usage = Counter()
    answers, omitted, invented = {}, set(), Counter()
    for task in tasks:
        task_batches = batches(task, args.batch_size)
        for number, items in enumerate(task_batches, 1):
            print(f"\n====={task.criterion}: batch {number} of {len(task_batches)}, {len(items)} element(s)=====")
            batch_answers, batch_omitted, batch_invented = ask(chat, task, items, usage)
            answers.update(batch_answers)
            omitted.update(batch_omitted)
            invented[task.criterion] += len(batch_invented)

    findings, verdicts = build_findings(axe, elements, answers, omitted)
    report = WorkflowReport(
        target=args.url,
        model=chat.model,
        thinking=not args.no_think,
        started_at=started_at,
        duration_s=round(time.perf_counter() - start, 1),
        llm_requests=usage["requests"],
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        invalid_replies=usage["invalid_replies"],
        truncated_replies=usage["truncated_replies"],
        batch_size=args.batch_size,
        axe_version=axe["testEngine"]["version"] if axe is not None else None,
        counts=dict(Counter(finding.severity for finding in findings)),
        criteria=criteria_counts(verdicts, invented),
        findings=findings,
        verdicts=verdicts,
    )
    path = report_path("accessibility-workflow", chat.model, args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
