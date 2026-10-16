import argparse
import asyncio
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
from pydantic_ai import (Agent, ModelResponse, ModelRetry, NativeOutput, RetryPromptPart, StructuredDict, ToolOutput,
                         ToolReturnPart, UnexpectedModelBehavior, UsageLimitExceeded, capture_run_messages)
from chat_backends import make_model, read_response

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

def model_name(args):
    return args.model or DEFAULT_MODELS[args.backend]

def model_and_settings(args):
    return make_model(args.backend, model_name(args), think=not args.no_think,
                      max_output_tokens=args.max_output_tokens)

# Reports

def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")

def report_path(prefix, args):
    url = urlparse(args.url)
    page = slug(url.path)  # e.g. blog-douro-valley-3-day-itinerary, empty for the homepage
    name = (model_name(args).replace(':', '-').replace('/', '-') + ("_no-think" if args.no_think else "")
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

def report_fields(args, started_at, start, usage):
    # what every report starts with: the run, and what the model used
    return dict(
        target=args.url,
        model=model_name(args),
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
MAX_INVALID_REPORTS = 3  # times a report that doesn't follow the schema is sent back
MAX_TOOL_RETRIES = 2  # times a tool call with invalid arguments is sent back, per tool
OUTPUT_RETRIES = MAX_TEXT_REPLIES + MAX_REJECTIONS + MAX_INVALID_REPORTS

def split_backed(items, kind, problem_of):
    backed, unverified = [], []
    for item in items:
        problem = problem_of(item)
        if problem:
            unverified.append({"kind": kind, **item.model_dump(), "reason": problem})
        else:
            backed.append(item)
    return backed, unverified

def run_agent(args, system_prompt, user_prompt, tools, submission, review, reject):
    model, settings = model_and_settings(args)
    name = model_name(args)
    agent = Agent(model, instructions=system_prompt, tools=tools, model_settings=settings,
                  output_type=ToolOutput(submission, name="submit_report"),
                  retries={"tools": MAX_TOOL_RETRIES, "output": OUTPUT_RETRIES})
    usage = Counter()

    @agent.output_validator
    def check_report(report):
        unverified = review(report)
        if not unverified or usage["rejected_submissions"] >= MAX_REJECTIONS:
            return report
        usage.update(rejected_submissions=1, unbacked_claims=len(unverified))
        print(f"=====Report rejected: {len(unverified)} unbacked item(s)=====")
        raise ModelRetry(reject(unverified))

    def show_turn(turn):
        if turn.thoughts:
            print(f"\n====={name}'s thoughts=====\n{turn.thoughts}\n")
        if turn.text:
            print(f"\n====={name} says=====\n{turn.text}\n")

        usage.update(llm_requests=1, input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                     truncated_replies=int(turn.truncated))

        if turn.truncated:
            print(f"====={name} was cut off at the output token limit=====")

        if not turn.tool_calls:
            if usage["text_replies"] >= MAX_TEXT_REPLIES:
                print(f"====={name} has no more function calls=====")
                return False
            usage["text_replies"] += 1
            print(f"=====No tool call, asking {name} to call a tool=====")
        for tool_name in turn.tool_calls:
            print(f"====={name} wants to use the {tool_name} tool=====")
        return True

    def show_request(request):
        # prints what goes back to the model: tool results, and what it is asked to fix
        for part in request.parts:
            if isinstance(part, ToolReturnPart):
                result = json.dumps(part.content, indent=2, ensure_ascii=False, default=str)
                print(f"=====Result of {part.tool_name}=====\n{result}\n")
            elif isinstance(part, RetryPromptPart):
                print(f"=====Sent back to {name}=====\n{part.model_response()}\n")

    async def run():
        # Tools run one at a time, in the order the model called them
        with agent.parallel_tool_call_execution_mode("sequential"):
            async with agent.iter(user_prompt) as agent_run:
                async for node in agent_run:
                    if Agent.is_call_tools_node(node):
                        # the model has replied, and its tool calls haven't run yet
                        if not show_turn(read_response(node.model_response)):
                            return None
                    elif Agent.is_model_request_node(node):
                        show_request(node.request)
                return agent_run.result.output

    try:
        submitted = asyncio.run(run())
    except (UnexpectedModelBehavior, UsageLimitExceeded) as e:
        # out of retries, a reply cut off in the middle of a tool call, or the request limit
        print(f"=====The run stopped: {e}=====")
        submitted = None

    if submitted is None:
        print("=====No valid report was submitted=====")
    else:
        print(f"====={name} has submitted the final report=====")
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
    for attempt in range(1 + MAX_RETRIES):
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
