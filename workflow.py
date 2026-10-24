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

def add_task(tasks, elements, criterion, instructions, entries, **options):
    # entries are (id, what the model sees, what the report keeps)
    for element_id, _, kept in entries:
        elements[element_id] = {"criterion": criterion, **kept}
    if entries:
        tasks.append(Task(criterion, instructions,
                          [{"id": element_id, **shown} for element_id, shown, _ in entries], **options))

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

class Questions:
    # what an analyzer asks the model through, keeping every element and answer for its report
    def __init__(self, judge, batch_size, instructions):
        self.judge = judge
        self.batch_size = batch_size
        self.instructions = instructions
        self.elements = {}
        self.answers = Answers()
        self.usage = Counter()

    def ask(self, criterion, ids, entries, skill=None, **options):
        # ids says what each entry is; the skill says how to judge it, and is the criterion's own unless given
        tasks = []
        add_task(tasks, self.elements, criterion, self.instructions(skill or criterion, ids), entries, **options)
        ask_tasks(self.judge, tasks, self.batch_size, self.usage, self.answers)

def main():
    trace_to_logfire()
    args = parse_args(make_parser("Find problems on a web page, asking the model about one question and a few "
                                  "elements at a time.", ANALYZERS, workflow=True))
    analyzer = ANALYZERS[args.analyzer](args)
    judge = make_judge(args, analyzer.judge_prompt)

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    if not analyzer.collect():
        return
    analyzer.finish()

    questions = Questions(judge, args.batch_size, analyzer.instructions)
    analyzer.ask_questions(questions)

    findings, verdicts, fields = analyzer.workflow_report(questions)
    report = analyzer.workflow_report_type(
        **report_fields(args, started_at, start, questions.usage),
        counts=dict(Counter(finding.severity for finding in findings)),
        invalid_replies=questions.usage["invalid_replies"],
        batch_size=args.batch_size,
        criteria=criteria_counts(verdicts, questions.answers.invented),
        findings=findings,
        verdicts=verdicts,
        **fields,
    )
    path = report_path(f"{analyzer.name}-workflow", args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
