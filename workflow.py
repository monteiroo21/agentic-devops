import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pydantic import ValidationError
from pydantic_ai import (Agent, ModelResponse, NativeOutput, StructuredDict, UnexpectedModelBehavior,
                         capture_run_messages)
from analyzers import ANALYZERS
from chat_backends import read_response
from common import (CONFIG, Verdict, criteria_counts, make_parser, model_and_settings, parse_args, report_fields,
                    report_path, trace_to_logfire)

# The script runs the tools and asks the model about a few elements at a time

@dataclass
class Task:
    criterion: str  # what the model judges, e.g. alt-text or soft-404
    instructions: str
    items: list[dict]  # what the model sees about each element, with its id
    answer: type = Verdict
    batched: bool = True  # False when the model needs every item at once, like a heading outline

def add_task(tasks, elements, criterion, instructions, entries, known=(), **options):
    # entries are (id, what the model sees, what the report keeps); those whose answer is known aren't asked
    for element_id, _, kept in entries:
        elements[element_id] = {"criterion": criterion, **kept}
    items = [{"id": element_id, **shown} for element_id, shown, _ in entries if element_id not in known]
    if items:
        tasks.append(Task(criterion, instructions, items, **options))

def batches(task, size):
    if not task.batched:
        return [task.items]
    return [task.items[start:start + size] for start in range(0, len(task.items), size)]

def make_judge(args, system_prompt):
    model, settings = model_and_settings(args)
    return Agent(model, instructions=system_prompt, model_settings=settings, retries={"output": 0})

def ask_json(judge, message, key, schema):
    with capture_run_messages() as messages:
        try:
            data = judge.run_sync(message, output_type=NativeOutput(StructuredDict(schema, name=key))).output
        except UnexpectedModelBehavior:
            data = None
    reply = [sent for sent in messages if isinstance(sent, ModelResponse)][-1]
    return data, read_response(reply)

def ask(judge, task, items, usage):
    key, schema = task.answer.FORMAT
    asked = {item["id"] for item in items}
    answers, omitted, invented = {}, [], []
    pending = items
    for attempt in range(1 + CONFIG.workflow.max_retries):
        if attempt:
            print(f"=====Asking again about {len(pending)} element(s) left out=====")
        data, reply = ask_json(judge, task.instructions + "\n\n" + json.dumps(pending, ensure_ascii=False, indent=1),
                               key, schema)
        usage.update(llm_requests=1, input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                     truncated_replies=int(reply.truncated))
        if reply.thoughts:
            print(f"====={judge.model.model_name}'s thoughts=====\n{reply.thoughts}\n")

        returned = data.get(key) if isinstance(data, dict) else None
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

@dataclass
class Answers:
    by_id: dict = field(default_factory=dict)  # each element's valid answer
    omitted: set = field(default_factory=set)  # ids left out of the model's first answer for their batch
    invented: Counter = field(default_factory=Counter)  # ids the model made up, per criterion

def ask_tasks(judge, tasks, batch_size, usage, answers):
    for task in tasks:
        task_batches = batches(task, batch_size)
        for number, items in enumerate(task_batches, 1):
            print(f"\n====={task.criterion}: batch {number} of {len(task_batches)}, {len(items)} element(s)=====")
            batch_answers, batch_omitted, batch_invented = ask(judge, task, items, usage)
            answers.by_id.update(batch_answers)
            answers.omitted.update(batch_omitted)
            answers.invented[task.criterion] += len(batch_invented)

class Memory:
    # what the workflows of a site run share. An element shown to the model exactly as on an earlier page gets the
    # answer it got there instead of being asked again, and each distinct element is kept once, as an item with every
    # page it is on, for the orchestrator to review
    def __init__(self):
        self.items = {}

    def key(self, page, criterion, instructions, shown, batched):
        # an element judged on its own is the same question on any page; one judged together with the rest of its
        # page, like a heading in the page's outline, is only the same question on that page
        return (criterion, instructions, json.dumps(shown, sort_keys=True, ensure_ascii=False),
                None if batched else page)

    def known(self, page, criterion, instructions, entries, batched):
        # the answers already given for these entries, under the entries' own ids
        answers = {}
        for element_id, shown, _ in entries:
            item = self.items.get(self.key(page, criterion, instructions, shown, batched))
            if item is not None and item["answer"] is not None:
                answers[element_id] = item["answer"].model_copy(update={"id": element_id})
        return answers

    def record(self, page, criterion, skill, instructions, entries, answers, batched):
        for element_id, shown, kept in entries:
            item = self.items.setdefault(self.key(page, criterion, instructions, shown, batched), {
                "id": f"i{len(self.items) + 1}", "criterion": criterion, "skill": skill, "shown": shown,
                "kept": kept, "answer": None, "elements": []})
            if item["answer"] is None:
                item["answer"] = answers.get(element_id)  # one left unanswered is asked again on its next page
            item["elements"].append({"page": page, "id": element_id})

class Questions:
    # what an analyzer asks the model through, keeping every element and answer for its report
    def __init__(self, judge, batch_size, instructions, memory=None, page=None):
        self.judge = judge
        self.batch_size = batch_size
        self.instructions = instructions
        self.memory = memory
        self.page = page
        self.elements = {}
        self.answers = Answers()
        self.usage = Counter()

    def ask(self, criterion, ids, entries, skill=None, **options):
        # ids says what each entry is; the skill says how to judge it, and is the criterion's own unless given
        instructions = self.instructions(skill or criterion, ids)
        batched = options.get("batched", True)
        known = {}
        if self.memory is not None:
            known = self.memory.known(self.page, criterion, instructions, entries, batched)
            if known:
                print(f"\n=====Reusing {len(known)} {criterion} answer(s) from earlier pages=====")
        self.answers.by_id.update(known)
        tasks = []
        add_task(tasks, self.elements, criterion, instructions, entries, known, **options)
        ask_tasks(self.judge, tasks, self.batch_size, self.usage, self.answers)
        if self.memory is not None:
            self.memory.record(self.page, criterion, skill or criterion, instructions, entries, self.answers.by_id,
                               batched)

def run_workflow(args, analyzer, memory=None):
    # one page through one analyzer: its tools, then the model's answers; None if there was nothing to judge
    judge = make_judge(args, analyzer.judge_prompt)

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    if not analyzer.collect():
        return None
    analyzer.finish()

    questions = Questions(judge, args.batch_size, analyzer.instructions, memory, args.url)
    analyzer.ask_questions(questions)

    findings, verdicts, fields = analyzer.workflow_report(questions)
    return analyzer.workflow_report_type(
        **report_fields(args, started_at, start, questions.usage),
        counts=dict(Counter(finding.severity for finding in findings)),
        invalid_replies=questions.usage["invalid_replies"],
        batch_size=args.batch_size,
        criteria=criteria_counts(verdicts, questions.answers.invented),
        findings=findings,
        verdicts=verdicts,
        **fields,
    )

def main():
    trace_to_logfire()
    args = parse_args(make_parser("Find problems on a web page, asking the model about one question and a few "
                                  "elements at a time.", ANALYZERS, workflow=True))
    analyzer = ANALYZERS[args.analyzer](args)
    report = run_workflow(args, analyzer)
    if report is None:
        return
    path = report_path(f"{analyzer.name}-workflow", args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
