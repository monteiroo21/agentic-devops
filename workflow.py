import asyncio
import json
import re
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
                    report_path, trace_to_logfire, without)

# The script runs the tools and asks the model about a few elements at a time

FINISHED = object()  # what next() gives for a page whose questions are all asked

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

def batches(items, size, batched=True):
    if not batched:
        return [items]
    return [items[start:start + size] for start in range(0, len(items), size)]

def fresh_id(element_id, taken):
    # the element's own id, or the next free one like it when another page's element already has it
    if element_id not in taken:
        return element_id
    prefix = re.sub(r"-\d+$", "", element_id)
    number = 1
    while f"{prefix}-{number}" in taken:
        number += 1
    return f"{prefix}-{number}"

async def ask_json(agent, message, key, schema):
    with capture_run_messages() as messages:
        try:
            data = (await agent.run(message, output_type=NativeOutput(StructuredDict(schema, name=key)))).output
        except UnexpectedModelBehavior:
            data = None
    reply = [sent for sent in messages if isinstance(sent, ModelResponse)][-1]
    return data, read_response(reply)

class Judge:
    # the model the questions of one or more pages are asked through, and what it used
    def __init__(self, args, system_prompt):
        model, settings = model_and_settings(args)
        self.agent = Agent(model, instructions=system_prompt, model_settings=settings, retries={"output": 0})
        self.batch_size = args.batch_size
        self.queue = []  # (questions, task) asked since the last flush
        self.usage = Counter()
        self.invented = Counter()  # ids the model made up, per criterion

    async def ask(self, task, items):
        key, schema = task.answer.FORMAT
        asked = {item["id"] for item in items}
        answers, omitted = {}, []
        pending = items
        for attempt in range(1 + CONFIG.workflow.max_retries):
            if attempt:
                print(f"=====Asking again about {len(pending)} element(s) left out=====")
            start = time.perf_counter()
            data, reply = await ask_json(self.agent, task.instructions + "\n\n"
                                         + json.dumps(pending, ensure_ascii=False, indent=1), key, schema)
            seconds = time.perf_counter() - start
            self.usage.update(llm_requests=1, input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                              truncated_replies=int(reply.truncated), model_s=seconds)
            print(f"=====Answered in {seconds:.1f}s: {reply.input_tokens} tokens in, {reply.output_tokens} out=====")
            if reply.thoughts:
                print(f"====={self.agent.model.model_name}'s thoughts=====\n{reply.thoughts}\n")

            returned = data.get(key) if isinstance(data, dict) else None
            if not isinstance(returned, list):
                self.usage["invalid_replies"] += 1
                print(f"=====Not an answer in the expected format=====\n{reply.text}\n")
                returned = []
            for raw in returned:
                try:
                    answer = task.answer.model_validate(raw)
                except ValidationError:
                    continue  # an answer that doesn't follow the format counts as no answer
                if answer.id not in asked:
                    self.invented[task.criterion] += 1
                elif answer.id not in answers:
                    answers[answer.id] = answer
                    print(f"{answer.id}: " + (f"{answer.verdict} | {answer.reason}" if isinstance(answer, Verdict)
                                              else answer.note))

            pending = [item for item in pending if item["id"] not in answers]
            if attempt == 0:
                omitted = [item["id"] for item in pending]
            if not pending:
                break
        return answers, omitted

    async def flush(self):
        # answers every question asked since the last flush. A question's batches follow each other, so the model's
        # server can reuse the instructions they start with
        groups = {}
        for questions, task in self.queue:
            # one question from every page shares its batches, unless the model needs a page's items on their own
            key = (task.criterion, task.instructions) if task.batched else id(task)
            groups.setdefault(key, []).append((questions, task))
        self.queue = []
        for tasks in groups.values():
            await self.ask_group(tasks)

    async def ask_group(self, tasks):
        task = tasks[0][1]  # the question all of them ask
        items = []  # what the model is asked, once for elements that pages show alike
        targets = {}  # by the id an item is asked under: each page's questions and id that get its answer
        asked_as = {}  # by an element's key: the id it is asked under
        for questions, page_task in tasks:
            for item in page_task.items:
                key = questions.key(page_task, item)
                if key not in asked_as:
                    asked_as[key] = fresh_id(item["id"], targets)
                    items.append({**item, "id": asked_as[key]})
                    targets[asked_as[key]] = []
                targets[asked_as[key]].append((questions, item["id"]))

        task_batches = batches(items, self.batch_size, task.batched)
        limit = asyncio.Semaphore(CONFIG.workflow.parallel_requests)

        async def ask_batch(number, batch):
            async with limit:
                pages = {questions for item in batch for questions, _ in targets[item["id"]]}
                print(f"\n====={task.criterion}: batch {number} of {len(task_batches)}, {len(batch)} element(s)"
                      + (f" from {len(pages)} pages" if len(pages) > 1 else "") + "=====")
                return await self.ask(task, batch)

        for answers, omitted in await asyncio.gather(*(ask_batch(number, batch)
                                                       for number, batch in enumerate(task_batches, 1))):
            for asked_id, answer in answers.items():
                for questions, element_id in targets[asked_id]:
                    questions.answers.by_id[element_id] = answer.model_copy(update={"id": element_id})
            for asked_id in omitted:
                for questions, element_id in targets[asked_id]:
                    questions.answers.omitted.add(element_id)

@dataclass
class Answers:
    by_id: dict = field(default_factory=dict)  # each element's valid answer
    omitted: set = field(default_factory=set)  # ids left out of the model's first answer for their batch

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
    # what an analyzer asks the judge through, keeping every element and answer of its page for its report
    def __init__(self, judge, instructions, memory=None, page=None):
        self.judge = judge
        self.instructions = instructions
        self.memory = memory
        self.page = page
        self.elements = {}
        self.answers = Answers()
        self.unrecorded = []  # what the memory keeps once the judge has answered

    def ask(self, criterion, ids, entries, skill=None, **options):
        # ids says what each entry is; the skill says how to judge it, and is the criterion's own unless given.
        # The judge answers at the page's next yield, or once it has asked all its questions
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
        self.judge.queue += [(self, task) for task in tasks]
        if self.memory is not None:
            self.unrecorded.append((criterion, skill or criterion, instructions, entries, batched))

    def key(self, task, item):
        # in a site run, elements shown alike get one answer, as the memory keeps them as one item
        if self.memory is None:
            return (self, item["id"])
        return self.memory.key(self.page, task.criterion, task.instructions, without(item, "id"), task.batched)

    def record(self):
        for criterion, skill, instructions, entries, batched in self.unrecorded:
            self.memory.record(self.page, criterion, skill, instructions, entries, self.answers.by_id, batched)
        self.unrecorded = []

async def run_workflows(analyzers, judge, memory=None):
    # each page through its analyzer: its tools, then the model's answers, with the pages' questions asked together.
    # A page's report is None if there was nothing to judge on it. When pages share requests, each report counts
    # everything the judge used so far
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    pages = []
    for analyzer in analyzers:
        collect_start = time.perf_counter()
        collected = await asyncio.to_thread(analyzer.collect)
        judge.usage["collect_s"] += time.perf_counter() - collect_start
        if collected:
            analyzer.finish()
            pages.append((analyzer, Questions(judge, analyzer.instructions, memory, analyzer.args.url)))

    # an analyzer yields where its later questions need the answers so far; one without yields asks all at once
    steps = [iter(analyzer.ask_questions(questions) or ()) for analyzer, questions in pages]
    while steps:
        steps = [step for step in steps if next(step, FINISHED) is not FINISHED]
        await judge.flush()
        for _, questions in pages:
            questions.record()

    reports = {}
    for analyzer, questions in pages:
        findings, verdicts, fields = analyzer.workflow_report(questions)
        reports[analyzer] = analyzer.workflow_report_type(
            **report_fields(analyzer.args, started_at, start, judge.usage),
            counts=dict(Counter(finding.severity for finding in findings)),
            invalid_replies=judge.usage["invalid_replies"],
            batch_size=judge.batch_size,
            criteria=criteria_counts(verdicts, judge.invented),
            findings=findings,
            verdicts=verdicts,
            **fields,
        )
    return [reports.get(analyzer) for analyzer in analyzers]

def main():
    trace_to_logfire()
    args = parse_args(make_parser("Find problems on a web page, asking the model about one question and a few "
                                  "elements at a time.", ANALYZERS, workflow=True))
    analyzer = ANALYZERS[args.analyzer](args)
    [report] = asyncio.run(run_workflows([analyzer], Judge(args, analyzer.judge_prompt)))
    if report is None:
        return
    path = report_path(f"{analyzer.name}-workflow", args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
