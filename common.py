import argparse
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlparse
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from chat_backends import GeminiChat, OllamaChat

load_dotenv()

# Command line and model

DEFAULT_MODELS = {"gemini": "gemini-3.1-flash-lite", "ollama": "qwen3:4b-instruct"}
DEFAULT_MAX_OUTPUT_TOKENS = 4096

def make_parser(description, workflow=False):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("url", help="the page to check")
    parser.add_argument("--backend", choices=DEFAULT_MODELS, default="gemini",
                        help="gemini (Google's API) or ollama (a local model)")
    parser.add_argument("--model", help="the model to use; defaults to "
                        + ", ".join(f"{model} for {backend}" for backend, model in DEFAULT_MODELS.items()))
    parser.add_argument("--no-think", action="store_true",
                        help="ollama only: answer without thinking first, which is much faster on a CPU")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS,
                        help="ollama only: the most tokens the model may write per turn, thinking included "
                             f"(default {DEFAULT_MAX_OUTPUT_TOKENS})")
    if workflow:
        parser.add_argument("--batch-size", type=int, default=8,
                            help="how many elements the model judges in one call (default 8)")
    return parser

def parse_args(parser):
    args = parser.parse_args()
    if urlparse(args.url).scheme not in ("http", "https"):
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
    if args.backend == "gemini" and (args.no_think or args.max_output_tokens != DEFAULT_MAX_OUTPUT_TOKENS):
        parser.error("--no-think and --max-output-tokens only work with --backend ollama")
    if getattr(args, "batch_size", 1) < 1:
        parser.error("--batch-size must be at least 1")
    return args

def make_chat(args, system_prompt, tools):
    model = args.model or DEFAULT_MODELS[args.backend]
    if args.backend == "ollama":
        return OllamaChat(model, system_prompt, tools, think=not args.no_think,
                          max_output_tokens=args.max_output_tokens)
    return GeminiChat(model, system_prompt, tools)

# Reports

def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")

def report_path(prefix, model, args):
    url = urlparse(args.url)
    page = slug(url.path)  # e.g. blog-douro-valley-3-day-itinerary, empty for the homepage
    name = (model.replace(':', '-').replace('/', '-') + ("_no-think" if args.no_think else "")
            + "_" + slug(url.netloc) + (f"_{page}" if page else "")
            + "_" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    path = Path("reports") / f"{prefix}_{name}.json"
    path.parent.mkdir(exist_ok=True)
    return path

class BaseReport(BaseModel):
    mode: str  # agent or workflow
    target: str
    model: str
    thinking: bool = True  # False for local runs with --no-think
    started_at: str
    duration_s: float
    llm_requests: int
    input_tokens: int
    output_tokens: int
    truncated_replies: int  # replies cut off at the output token limit
    counts: dict[str, int]  # findings per severity

class AgentReport(BaseReport):
    mode: str = "agent"
    narrative: str
    rejected_submissions: int = 0  # times the agent submitted items it hadn't checked
    unbacked_claims: int = 0  # total unbacked items across those submissions
    text_replies: int = 0  # times the model wrote text instead of calling a tool; always 0 for Gemini, which must call one

class WorkflowReport(BaseReport):
    mode: str = "workflow"
    invalid_replies: int  # replies that weren't JSON in the expected format
    batch_size: int
    criteria: dict[str, dict[str, int]]  # per criterion: elements, each verdict, omitted_first, invented

def report_fields(args, chat, started_at, start, usage):
    # what every report starts with: the run, and what the model used
    return dict(
        target=args.url,
        model=chat.model,
        thinking=not args.no_think,
        started_at=started_at,
        duration_s=round(time.perf_counter() - start, 1),
        llm_requests=usage["llm_requests"],
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        truncated_replies=usage["truncated_replies"],
    )

# Agent: the model calls the tools itself and submits one report

MAX_REJECTIONS = 2  # times a report with unbacked items is sent back
MAX_TEXT_REPLIES = 2  # times the model is asked to call a tool instead of writing text

def split_backed(items, kind, problem_of):
    backed, unverified = [], []
    for item in items:
        problem = problem_of(item)
        if problem:
            unverified.append({"kind": kind, **item.model_dump(), "reason": problem})
        else:
            backed.append(item)
    return backed, unverified

def run_agent(chat, run_tool, submission, review, reject):
    # Lets the model call tools until it submits a report. run_tool runs any other tool and returns what the
    # model sees, review lists the submitted items no tool result backs, and reject says why they were sent back
    usage = Counter()
    submitted = None

    while True:
        turn = chat.generate()

        if turn.thoughts:
            print(f"\n====={chat.model}'s thoughts=====\n{turn.thoughts}\n")
        if turn.text:
            print(f"\n====={chat.model} says=====\n{turn.text}\n")

        usage.update(llm_requests=1, input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                     truncated_replies=int(turn.truncated))

        if turn.truncated:
            print(f"====={chat.model} was cut off at the output token limit=====")

        if not turn.tool_calls:
            if usage["text_replies"] >= MAX_TEXT_REPLIES:
                print(f"====={chat.model} has no more function calls=====")
                break
            # Local models can't be forced to call a tool, and small ones sometimes write the report as text
            usage["text_replies"] += 1
            print(f"=====No tool call, asking {chat.model} to call a tool=====")
            chat.add_user_message("You replied with text, but only tool calls are acted on. If you have finished, "
                                  "hand in your report by calling the submit_report tool; otherwise call the tool "
                                  "you need next. Do not write the report as text.")
            continue

        # Answer every tool call in this response, not just the last one
        results = []
        for call in sorted(turn.tool_calls, key=lambda call: call.name == "submit_report"):
            print(f"====={chat.model} wants to use the {call.name} tool=====")

            if call.name != "submit_report":
                result = run_tool(call.name, call.args)
            else:
                try:
                    report = submission.model_validate(call.args)
                except ValidationError as e:
                    result = {"error": f"Invalid report, fix it and call submit_report again: {e}"}
                else:
                    unverified = review(report)
                    if not unverified or usage["rejected_submissions"] >= MAX_REJECTIONS:
                        submitted = report
                        continue
                    # Send the report back: the agent must look before it claims
                    usage.update(rejected_submissions=1, unbacked_claims=len(unverified))
                    print(f"=====Report rejected: {len(unverified)} unbacked item(s)=====")
                    result = {"error": reject(unverified)}

            print(f"=====Result of {call.name}=====\n{json.dumps(result, indent=2, ensure_ascii=False)}\n")
            results.append((call, result))

        if submitted is not None:
            print(f"====={chat.model} has submitted the final report=====")
            break

        chat.add_tool_results(results)

    if submitted is None:
        print("=====No valid report was submitted=====")
    return submitted, usage

# Workflow: the script runs the tools and asks the model about a few elements at a time

MAX_RETRIES = 2  # times a batch is asked again about the elements its answer left out

def answers_schema(key, properties, required):
    return {
        "type": "object",
        "properties": {
            key: {"type": "array", "items": {"type": "object", "properties": properties, "required": required}},
        },
        "required": [key],
    }

# FORMAT is the key the answers come under and their JSON schema.
# The reason comes before the verdict, so the model explains before it decides
class Verdict(BaseModel):
    FORMAT: ClassVar = ("verdicts", answers_schema("verdicts", {
        "id": {"type": "string"},
        "reason": {"type": "string"},
        "verdict": {"type": "string", "enum": ["pass", "fail", "unsure"]},
    }, ["id", "reason", "verdict"]))
    id: str
    reason: str
    verdict: Literal["pass", "fail", "unsure"]

class Note(BaseModel):
    FORMAT: ClassVar = ("notes", answers_schema("notes", {
        "id": {"type": "string"},
        "note": {"type": "string"},
        "suggested_fix": {"type": "string"},
    }, ["id", "note"]))
    id: str
    note: str
    suggested_fix: str | None = None

VERDICT_SEVERITY = {"pass": "info", "fail": "warning", "unsure": "needs-review", "unanswered": "needs-review"}

@dataclass
class Task:
    criterion: str  # what the model judges, e.g. alt-text or soft-404
    instructions: str
    items: list[dict]  # what the model sees about each element, with its id
    answer: type = Verdict
    batched: bool = True  # False when the model needs every item at once, like a heading outline

def with_ids(items, prefix):
    return [(f"{prefix}-{number}", item) for number, item in enumerate(items, 1)]

def without(item, *keys):
    return {key: value for key, value in item.items() if key not in keys}

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

def ask(chat, task, items, usage):
    # asks about items until each has a valid answer or the retries run out
    key, schema = task.answer.FORMAT
    asked = {item["id"] for item in items}
    answers, omitted, invented = {}, [], []
    pending = items
    for attempt in range(1 + MAX_RETRIES):
        if attempt:
            print(f"=====Asking again about {len(pending)} element(s) left out=====")
        reply = chat.ask_json(task.instructions + "\n\n" + json.dumps(pending, ensure_ascii=False, indent=1), schema)
        usage.update(llm_requests=1, input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
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

@dataclass
class Answers:
    by_id: dict = field(default_factory=dict)  # each element's valid answer
    omitted: set = field(default_factory=set)  # ids left out of the model's first answer for their batch
    invented: Counter = field(default_factory=Counter)  # ids the model made up, per criterion

def ask_tasks(chat, tasks, batch_size, usage, answers):
    for task in tasks:
        task_batches = batches(task, batch_size)
        for number, items in enumerate(task_batches, 1):
            print(f"\n====={task.criterion}: batch {number} of {len(task_batches)}, {len(items)} element(s)=====")
            batch_answers, batch_omitted, batch_invented = ask(chat, task, items, usage)
            answers.by_id.update(batch_answers)
            answers.omitted.update(batch_omitted)
            answers.invented[task.criterion] += len(batch_invented)

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
