import argparse
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlparse
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict
from chat_backends import make_model
import logfire

load_dotenv()
logfire.configure(send_to_logfire="if-token-present")
logfire.instrument_pydantic_ai()
logfire.instrument_httpx()

CONFIG_PATH = Path(__file__).parent / "config.yaml"

class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ModelsConfig(Section):
    gemini: str
    ollama: str

class GeminiConfig(Section):
    thinking_level: str

class OllamaConfig(Section):
    url: str
    max_output_tokens: int

class AgentConfig(Section):
    max_rejections: int
    max_text_replies: int
    max_invalid_reports: int
    max_tool_retries: int
    max_requests: int

class WorkflowConfig(Section):
    batch_size: int
    max_retries: int

class BrowserConfig(Section):
    settle_timeout_ms: int
    block_private_addresses: bool  # false lets the agent's browser reach a site on this machine, like the test site
    max_content_tokens: int  # the most page text a browser tool returns, like the text after a click
    max_requests: int  # the agent's request limit when it has the browser, since exploring takes more turns

class AccessibilityConfig(Section):
    skills: Path
    prompts: Path
    axe_script: Path
    wcag_tags: list[str]
    max_elements_per_rule: int
    max_page_items: int
    max_text_chars: int
    max_html_chars: int
    max_violation_examples: int
    max_title_headings: int
    max_title_text_chars: int

class LinksConfig(Section):
    skills: Path
    prompts: Path
    timeout_s: float
    user_agent: str
    max_page_bytes: int
    max_page_text_chars: int
    workflow_page_text_chars: int

class Config(Section):
    models: ModelsConfig
    gemini: GeminiConfig
    ollama: OllamaConfig
    agent: AgentConfig
    workflow: WorkflowConfig
    browser: BrowserConfig
    accessibility: AccessibilityConfig
    links: LinksConfig

def load_config(path=CONFIG_PATH):
    config = Config.model_validate(yaml.safe_load(path.read_text()))
    config.accessibility.skills = path.parent / config.accessibility.skills
    config.accessibility.prompts = path.parent / config.accessibility.prompts
    config.accessibility.axe_script = path.parent / config.accessibility.axe_script
    config.links.skills = path.parent / config.links.skills
    config.links.prompts = path.parent / config.links.prompts
    return config

CONFIG = load_config()

# Command line and model

def make_parser(description, analyzers, workflow=False):
    # one command per analyzer, e.g. agent.py links URL
    models = CONFIG.models.model_dump()
    parser = argparse.ArgumentParser(description=description)
    commands = parser.add_subparsers(dest="analyzer", required=True, metavar="analyzer")
    for analyzer in analyzers.values():
        command = commands.add_parser(analyzer.name, help=analyzer.description, description=analyzer.description)
        command.add_argument("url", help="the page to check")
        command.add_argument("--backend", choices=models, default="gemini",
                             help="gemini (Google's API) or ollama (a local model)")
        command.add_argument("--model", help="the model to use; defaults to "
                             + ", ".join(f"{model} for {backend}" for backend, model in models.items()))
        command.add_argument("--no-think", action="store_true",
                             help="ollama only: answer without thinking first, which is much faster on a CPU")
        command.add_argument("--max-output-tokens", type=int, default=CONFIG.ollama.max_output_tokens,
                             help="ollama only: the most tokens the model may write per turn, thinking included "
                                  f"(default {CONFIG.ollama.max_output_tokens})")
        if workflow:
            command.add_argument("--batch-size", type=int, default=CONFIG.workflow.batch_size,
                                 help="how many elements the model judges in one call "
                                      f"(default {CONFIG.workflow.batch_size})")
        else:
            analyzer.add_agent_arguments(command)
        analyzer.add_arguments(command)
    return parser

def parse_args(parser):
    args = parser.parse_args()
    if urlparse(args.url).scheme not in ("http", "https"):
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
    if args.backend == "gemini" and (args.no_think or args.max_output_tokens != CONFIG.ollama.max_output_tokens):
        parser.error("--no-think and --max-output-tokens only work with --backend ollama")
    if getattr(args, "batch_size", 1) < 1:
        parser.error("--batch-size must be at least 1")
    return args

def model_name(args):
    return args.model or getattr(CONFIG.models, args.backend)

def model_and_settings(args):
    return make_model(args.backend, model_name(args), think=not args.no_think,
                      max_output_tokens=args.max_output_tokens, ollama_url=CONFIG.ollama.url,
                      thinking_level=CONFIG.gemini.thinking_level)

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
    skills_loaded: list[str] = []  # the skills the agent loaded, in the order it first loaded them
    skill_files_read: list[str] = []  # the files it read from them, as skill/path

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

FRONTMATTER = re.compile(r"---\n(.*?)\n---\n?(.*)", re.DOTALL)

@dataclass
class Skill:
    name: str
    description: str
    metadata: dict[str, str]  # the skills format only allows strings, so a list is separated by spaces
    path: Path

    def values(self, key):
        return self.metadata.get(key, "").split()

    def flag(self, key):
        return self.metadata.get(key) == "true"

    def rules(self):
        if not self.metadata:
            return []
        return self.values("rules") or [self.name]

def read_skill(path):
    match = FRONTMATTER.match(path.read_text())
    if match is None:
        raise ValueError(f"{path} doesn't start with a frontmatter between --- lines")
    return yaml.safe_load(match.group(1)) or {}, match.group(2).strip()

def list_skills(folder):
    skills = {}
    for path in sorted(folder.glob("*/SKILL.md")):
        front, _ = read_skill(path)
        metadata = front.get("metadata") or {}
        if front.get("name") != path.parent.name:
            raise ValueError(f"{path}: its name, {front.get('name')!r}, must be the name of its folder")
        if not front.get("description"):
            raise ValueError(f"{path} has no description")
        if not all(isinstance(value, str) for value in metadata.values()):
            raise ValueError(f"{path}: every metadata value must be a string")
        skills[front["name"]] = Skill(front["name"], front["description"], metadata, path)
    if not skills:
        raise ValueError(f"no skills in {folder}")
    return skills

def skill_instructions(skill):
    return read_skill(skill.path)[1]

def skill_files(skill):
    folder = skill.path.parent
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*")
                  if path.is_file() and path != skill.path)

def skill_body(skill):
    body = skill_instructions(skill)
    examples = skill.path.parent / "references" / "examples.md"
    if examples.exists():
        body += "\n\n" + examples.read_text().strip()
    return body

def skills_prompt(skills):
    entries = [f"<skill>\n<name>{skill.name}</name>\n<description>{skill.description}</description>\n</skill>"
               for skill in skills.values()]
    return "<available_skills>\n" + "\n".join(entries) + "\n</available_skills>"

def read_prompt(path, **values):
    # a prompt from the prompts folder, with its {placeholders} filled in
    return path.read_text().strip().format(**values)

# Agent: what the analyzers' tools and evidence checks share

def docstring_with(**values):
    def fill(function):
        function.__doc__ = function.__doc__.format(**values)
        return function
    return fill

def split_backed(items, kind, problem_of):
    backed, unverified = [], []
    for item in items:
        problem = problem_of(item)
        if problem:
            unverified.append({"kind": kind, **item.model_dump(), "reason": problem})
        else:
            backed.append(item)
    return backed, unverified

# Workflow: the answers the analyzers ask for, and how they are counted

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

def with_ids(items, prefix):
    return [(f"{prefix}-{number}", item) for number, item in enumerate(items, 1)]

def without(item, *keys):
    return {key: value for key, value in item.items() if key not in keys}

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
