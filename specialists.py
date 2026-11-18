import argparse
import asyncio
import json
import re
import time
from argparse import Namespace
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_ai import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from agent import SkillTools, run_agent_async
from analyzers.accessibility import AccessibilityAnalyzer
from analyzers.links import LinkAnalyzer
from common import CONFIG, IMPACTS, Impact, Verdict, read_skill, trace_to_logfire
from toolkits import TOOLKITS, element_id, filled
from tools.axe import wcag_number
from tools.links import remember_results
from workflow import Questions

FLEET = CONFIG.fleet
CHECKS = {"links": LinkAnalyzer, "accessibility": AccessibilityAnalyzer}  # a skill's category is one of these
USAGE = ("llm_requests", "input_tokens", "output_tokens", "truncated_replies", "text_replies", "text_submissions",
         "rejected_submissions", "cut_off_retries")
IMPACT_IN_TEXT = re.compile(rf"[\s.;,]*\bimpact\s*[:=-]\s*({'|'.join(IMPACTS)})\b\.?", re.IGNORECASE)

class RatedVerdict(Verdict):
    impact: Impact | None = None  # pyright: ignore[reportInvalidTypeForm]
    suggested_fix: str | None = None

class AgentFinding(BaseModel):
    item_id: str = Field(description="The item's id, exactly as the tools returned it.")
    skill: str = Field(description="The skill this finding is for, as named under Skills.")
    reason: str = Field("", description="For a fail or an unsure: what decides it, in a few words. A pass can leave "
                                        "it empty.")
    verdict: Literal["pass", "fail", "unsure"]

    def answer(self, element):
        return Verdict(id=element, reason=self.reason, verdict=self.verdict)

    def problem(self):
        if self.verdict == "fail" and not self.reason.strip():
            return 'a fail needs "reason": the fact that shows the problem, in a few words'
        if self.verdict == "unsure" and not self.reason.strip():
            return 'an unsure needs "reason": what is missing to decide, in a few words'
        return None

    def missing_impact(self):
        return False  # whether it is a fail kept without the impact it needs, which a later finding can add

class RatedAgentFinding(AgentFinding):
    impact: Literal[*IMPACTS, ""] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="For a fail: how much the problem hurts the site's visitors, as the skill's Impact says. Empty "
                    "for an unsure.")
    suggested_fix: str = Field("", description="For a fail: how to fix it, in a few words. Empty if you can't tell.")

    @model_validator(mode="before")
    @classmethod
    def impact_given(cls, data):
        if isinstance(data, dict) and data.get("impact") is None:
            data = {**data, "impact": ""}  # left out, or null
        return data

    @field_validator("impact", mode="before")
    @classmethod
    def plain_impact(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def impact_from_text(self):
        if not self.impact:
            for name in ("suggested_fix", "reason"):
                found = IMPACT_IN_TEXT.search(getattr(self, name))
                if found:
                    self.impact = found[1].lower()
                    setattr(self, name, IMPACT_IN_TEXT.sub("", getattr(self, name)).strip())
                    break
        return self

    def answer(self, element):
        failed = self.verdict == "fail"
        return RatedVerdict(id=element, reason=self.reason, verdict=self.verdict,
                            impact=(self.impact or None) if failed else None,
                            suggested_fix=(self.suggested_fix or None) if failed else None)

    def missing_impact(self):
        return self.verdict == "fail" and not self.impact

class SubmitFindings(BaseModel):
    """Finish the task, once next_item says every item is recorded."""
    model_config = ConfigDict(extra="forbid")

ANSWERS = {"findings": AgentFinding, "rated-findings": RatedAgentFinding}  # what an agent's file can name: what record_answer takes

@dataclass(frozen=True)
class SkillFile:
    name: str
    description: str
    category: str  # links or accessibility: the check its findings are reported under
    wcag: tuple[str, ...]  # the WCAG criteria it checks, like 2.4.4; none for a links skill
    help_url: str | None  # a page explaining it, for the report
    tools: tuple[str, ...]  # the agent's tools it uses
    path: Path  # its SKILL.md

    @property
    def wcag_tags(self):
        # as axe-core names the criteria, like wcag244, which is how the report keeps them
        return ["wcag" + number.replace(".", "") for number in self.wcag]

    def line(self):
        # its name alone before the colon, the name load_skill takes: qwen read "link-purpose (WCAG 2.4.4)" as one
        about = f" (WCAG {', '.join(self.wcag)})" if self.wcag else ""
        return f"- {self.name}: {self.description}{about}"

def load_skill(path):
    front, _ = read_skill(path)
    missing = [key for key in ("name", "description", "category", "tools") if not front.get(key)]
    if missing:
        raise ValueError(f"{path} has no {', '.join(missing)}")
    wcag = front.get("wcag")
    wcag = () if wcag is None else (wcag,) if isinstance(wcag, str) else tuple(wcag)
    problems = []
    if front["name"] != path.parent.name:
        problems.append(f"its name, {front['name']!r}, must be the name of its folder")
    if front["category"] not in CHECKS:
        problems.append(f"category must be one of {', '.join(CHECKS)}")
    if not all(isinstance(number, str) and number.replace(".", "").isdigit() for number in wcag):
        problems.append('wcag must be null, a criterion like "2.4.4" or a list of them, each in quotes')
    if not isinstance(front["tools"], list):
        problems.append("tools must be a list")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return SkillFile(front["name"], " ".join(front["description"].split()), front["category"], wcag,
                     front.get("help_url"), tuple(front["tools"]), path)

@dataclass(frozen=True)
class AgentFile:
    key: str  # its file's name, like link, which is also the folder its skills are in
    name: str  # like link-agent
    description: str  # what it does, in the orchestrator's list of agents
    skills: dict  # by name: its SkillFiles, in its file's order
    tools: tuple[str, ...]  # its tools' names, besides the ones every agent has: load_skill (and read_skill_file for
    max_steps: int  # its model calls in a run, besides FLEET.steps_per_answer for each answer it is asked for
    answer: str  # the findings it records, one of ANSWERS
    prompt: str  # its system prompt, with {skills} where the skills of a task go
    toolkit: type  # what gives it its tools

    @property
    def finding(self):
        return ANSWERS[self.answer]  # what record_answer takes

    def system_prompt(self, skills):
        return self.prompt.replace("{skills}", "\n".join(skill.line() for skill in skills))

def load_agent(path, skills_folder):
    front, prompt = read_skill(path)
    missing = [key for key in ("name", "description", "skills", "tools", "max_steps", "answer") if not front.get(key)]
    if missing:
        raise ValueError(f"{path} has no {', '.join(missing)}")
    name, tools = front["name"], tuple(front["tools"])
    problems = []
    skills = {}
    for skill in front["skills"]:
        skill_path = skills_folder / path.stem / skill / "SKILL.md"
        if not skill_path.exists():
            problems.append(f"it has no skill file {skill_path.relative_to(skills_folder.parent)}")
            continue
        skills[skill] = load_skill(skill_path)
        unknown = [tool for tool in skills[skill].tools if tool not in tools]
        if unknown:
            problems.append(f"the {skill} skill uses {', '.join(unknown)}, which the agent doesn't have")
    toolkit = TOOLKITS.get(name)
    if toolkit is None:
        problems.append(f"no toolkit gives the tools of an agent named {name!r}; the toolkits are for "
                        + ", ".join(TOOLKITS))
    elif set(toolkit.tools_given) != set(tools):
        problems.append(f"its toolkit gives {', '.join(toolkit.tools_given)}, but its file lists {', '.join(tools)}")
    if front["answer"] not in ANSWERS:
        problems.append(f"answer must be one of {', '.join(ANSWERS)}")
    if not isinstance(front["max_steps"], int) or front["max_steps"] < 2:
        problems.append("max_steps must be a number, at least 2: a tool call and the submission")
    problems += [f"its prompt doesn't mention {word}"
                 for word in ("{skills}", "load_skill", "next_item", "record_answer", "submit_findings")
                 if word not in prompt]
    unused = [tool for tool in tools if not any(tool in skill.tools for skill in skills.values())]
    if unused:
        problems.append(f"none of its skills lists {', '.join(unused)}, so no task would be offered it")
    partial = [tool for tool in tools if tool in prompt and not all(tool in skill.tools for skill in skills.values())]
    if partial:
        problems.append(f"its prompt names {', '.join(partial)}, which only some of its skills list: name it in their "
                        "instructions instead")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return AgentFile(path.stem, name, " ".join(front["description"].split()), skills, tools, front["max_steps"],
                     front["answer"], prompt, toolkit)

def load_agents(folder, skills_folder, keys):
    # the agents the config lists, in its order, which is the order the orchestrator sees them in, by name
    if len(set(keys)) != len(keys):
        raise ValueError(f"the config lists an agent twice: {', '.join(keys)}")
    unknown = [key for key in keys if not (folder / f"{key}.md").exists()]
    if unknown:
        raise ValueError(f"no file in {folder} for {', '.join(unknown)}")
    agents = {}
    for key in keys:
        agent = load_agent(folder / f"{key}.md", skills_folder)
        if agent.name in agents:
            raise ValueError(f"two agents are named {agent.name}")
        agents[agent.name] = agent
    owners = Counter(skill for agent in agents.values() for skill in agent.skills)
    shared = [skill for skill, count in owners.items() if count > 1]
    if shared:
        raise ValueError(f"a skill belongs to one agent only: {', '.join(shared)} belong to more")
    return agents

AGENTS = load_agents(FLEET.folder, FLEET.skills, FLEET.agents)
AGENT_OF = {skill: agent for agent in AGENTS.values() for skill in agent.skills}  # by skill name

BACKENDS = list(CONFIG.models.model_dump())  # where the agents' model runs: ollama on this machine, or gemini in the cloud

def agent_model(parser, backend, model, no_think, max_output_tokens):
    if backend != "ollama" and (no_think or max_output_tokens != FLEET.max_output_tokens):
        parser.error(f"--no-think and --max-output-tokens only work with the ollama backend, not {backend}")
    return model or getattr(CONFIG.models, backend)

def page_args(check, url, backend, model, no_think, max_output_tokens, site_pages=()):
    return Namespace(url=url, analyzer=CHECKS[check].name, backend=backend, model=model, no_think=no_think,
                     max_output_tokens=max_output_tokens, timeout_s=FLEET.timeout_s, output=None,
                     checked_elsewhere=set(site_pages), read_linked_pages=False)

class PageWork:
    def __init__(self, args_for, memory=None):
        self.analyzers = {check: analyzer(args_for(check)) for check, analyzer in CHECKS.items()}
        self.url = self.analyzers["links"].args.url
        self.collected = dict.fromkeys(CHECKS)  # by check: True once collected, False if there was nothing to judge
        self.collect_s = Counter()  # by check: the time collecting took
        self.questions = Questions(None, lambda skill, ids: skill, memory, self.url)

    async def collect(self, check):
        if self.collected[check] is None:
            start = time.perf_counter()
            self.collected[check] = await asyncio.to_thread(self.analyzers[check].collect)
            self.collect_s[check] = time.perf_counter() - start
        return self.collected[check]

    @property
    def lands_on(self):
        # the page of the site the browser ended on instead of this one, which is checked on its own
        return self.analyzers["accessibility"].lands_on

@dataclass
class Run:
    task: str
    agent: str
    skills: list[str]
    url: str
    batch: str | None = None  # unused since a task is one run: kept for the reports' shape
    status: str = "checked"  # checked, nothing to check, nothing new, lands on another page, partly answered or failed
    lands_on: str | None = None
    error: str | None = None
    expected: list[tuple[str, str]] = field(default_factory=list)  # the (item id, skill) pairs it was given
    dropped: list[tuple[str, str]] = field(default_factory=list)  # pairs taken out of expected, made needless by a fail
    known: int = 0  # pairs left out of what it was given, since they were answered before
    answers: dict = field(default_factory=dict)  # by (item id, skill)
    first_missing: list | None = None  # the pairs its first submission had no accepted answer for
    refused: dict = field(default_factory=dict)  # by pair: why its latest answer wasn't accepted, until one is
    needs_impact: list = field(default_factory=list)  # pairs whose fail was kept without its impact
    impact_refused: set = field(default_factory=set)  # pairs whose fail was sent back once for its impact
    already_answered: list = field(default_factory=list)  # (pair, recorded verdict, later verdict not taken)
    refusals: int = 0  # answers not accepted, like a pass on a page whose destination it didn't read
    invented: int = 0  # answers for ids or skills its tools never showed
    tool_calls: Counter = field(default_factory=Counter)  # by tool
    read: list[str] = field(default_factory=list)  # the items whose linked page it read
    skills_loaded: list[str] = field(default_factory=list)  # with load_skill, in the order it first loaded them
    skill_files_read: list[str] = field(default_factory=list)  # with read_skill_file, as skill/path
    findings: list[dict] = field(default_factory=list)  # its accepted answers, with what the code adds to them
    usage: Counter = field(default_factory=Counter)
    collect_s: float = 0.0  # the time the page's tools took, if this run was the first to need them

    def unanswered(self):
        return [pair for pair in self.expected if pair not in self.answers]

    def record(self):
        return {
            "task": self.task, "agent": self.agent, "skills": self.skills, "url": self.url, "batch": self.batch,
            "status": self.status, "lands_on": self.lands_on, "error": self.error, "items": len(self.expected),
            "known": self.known, "answered": sum(pair in self.answers for pair in self.expected),
            "unanswered": [f"{item_id} {skill}" for item_id, skill in self.unanswered()],
            "dropped": [f"{item_id} {skill}" for item_id, skill in self.dropped],
            "no_impact": [f"{item_id} {skill}" for item_id, skill in self.needs_impact],
            "already_answered": [f"{item_id} {skill}: {recorded}, then {sent}"
                                 for (item_id, skill), recorded, sent in self.already_answered],
            "omitted_first": len(self.first_missing or []), "refusals": self.refusals, "invented": self.invented,
            "tool_calls": dict(self.tool_calls), "destinations_read": self.read, "skills_loaded": self.skills_loaded,
            "skill_files_read": self.skill_files_read, **{key: self.usage[key] for key in USAGE},
            "model_s": round(self.usage["model_s"], 1), "collect_s": round(self.collect_s, 1),
            "findings": self.findings,
        }

def finding(run, work, pair, answer):
    # what the agent answered, with what the code adds: the skill's category, the WCAG criteria, and the page
    item_id, skill = pair
    kept = work.questions.elements[element_id(item_id, skill)]
    return {"item_id": item_id, "skill": skill, "category": kept["category"],
            "wcag": [wcag_number(tag) for tag in kept.get("wcag") or [] if tag[4:].isdigit()], "page_url": run.url,
            "reason": answer.reason, "verdict": answer.verdict, "impact": getattr(answer, "impact", None),
            "suggested_fix": getattr(answer, "suggested_fix", None)}

CUT_OFF_MESSAGE = ("Your last reply was cut off at the output limit before it was complete, so none of it was taken. "
                   "Send it again, shorter: one answer per record_answer call, with a reason of a few words.")
PROGRESS = "Progress:"  # how the note that stands for the recorded items' messages starts

def task_prompt(run):
    return f"Task {run.task}: check the page {run.url}."

def keep_answer(run, work, toolkit, entry):
    pair = item_id, skill = entry.item_id, entry.skill
    current = toolkit.current
    if item_id != current:
        if element_id(*pair) not in work.questions.elements:
            run.invented += 1
        run.refusals += 1
        return {"error": f"you are checking {current}: record its answers, then call next_item for the next item"}
    needs = toolkit.still_needs(current)
    if pair in run.answers:
        recorded = run.answers[pair]
        if entry.verdict != recorded.verdict:
            run.already_answered.append((pair, recorded.verdict, entry.verdict))
        elif pair in run.needs_impact and getattr(entry, "impact", None):
            run.answers[pair] = recorded.model_copy(update={
                "impact": entry.impact, "suggested_fix": entry.suggested_fix or recorded.suggested_fix})
            run.needs_impact.remove(pair)
            return filled({"recorded": f"the impact of {item_id}'s {skill} fail", "still_needs": needs or None})
        return filled({"already_recorded": f"{item_id} {skill} is already recorded as {recorded.verdict}, which is "
                                           "final", "still_needs": needs or None})
    if pair not in run.expected:
        run.refusals += 1
        if pair in run.dropped:
            return filled({"error": f"{item_id} needs no {skill} answer after its fail", "still_needs": needs or None})
        return {"error": f"{item_id} isn't asked about {skill}; it still needs {', '.join(needs) or 'nothing'}"}
    why = (f"load the {skill} skill with load_skill first" if skill not in run.skills_loaded
           else entry.problem() or toolkit.refused(entry))
    if not why and entry.missing_impact() and pair not in run.impact_refused:
        run.impact_refused.add(pair)
        why = ("a fail needs its impact: critical, serious, moderate or minor, as the skill's Impact says. Give it "
               "again with its impact and suggested_fix")
    if why:
        run.refused[pair] = why
        run.refusals += 1
        return {"error": why, "still_needs": needs}
    run.answers[pair] = entry.answer(element_id(*pair))
    run.refused.pop(pair, None)
    if entry.missing_impact():
        run.needs_impact.append(pair)
    result = {"recorded": f"{item_id} {skill}: {entry.verdict}"}
    if entry.verdict == "fail":
        dropped = [later for later in toolkit.needless_after_fail(skill)
                   if (item_id, later) in run.expected and (item_id, later) not in run.answers]
        for later in dropped:
            run.expected.remove((item_id, later))
            run.refused.pop((item_id, later), None)
            run.dropped.append((item_id, later))
        if dropped:
            result["not_needed"] = f"after this fail, {item_id} needs no {', '.join(dropped)} answer"
    needs = toolkit.still_needs(item_id)
    result.update({"still_needs": needs} if needs else {"next": "call next_item for the next item"})
    return result

def tool_calls(message):
    if not isinstance(message, ModelResponse):
        return []
    return [part for part in message.parts if isinstance(part, ToolCallPart)]

def gave_item(message, item_id):
    # whether this request holds the next_item result that gave the item
    return isinstance(message, ModelRequest) and any(
        isinstance(part, ToolReturnPart) and part.tool_name == "next_item" and isinstance(part.content, dict)
        and part.content.get("id") == item_id for part in message.parts)

def is_progress(part):
    return isinstance(part, UserPromptPart) and isinstance(part.content, str) and part.content.startswith(PROGRESS)

def without_progress(messages):
    kept = []
    for message in messages:
        if isinstance(message, ModelRequest) and any(is_progress(part) for part in message.parts):
            parts = [part for part in message.parts if not is_progress(part)]
            if not parts:
                continue
            message = replace(message, parts=parts)
        kept.append(message)
    return kept

def recorded_items_out(toolkit, kept_tools):
    def prune(messages):
        handed = [index for index, message in enumerate(messages)
                  if any(call.tool_name == "next_item" for call in tool_calls(message))]
        if not handed:
            return messages
        start, end = handed[0], len(messages)
        if toolkit.current is not None and toolkit.still_needs(toolkit.current):
            end = next((index - 1 for index in range(len(messages) - 1, start, -1)
                        if gave_item(messages[index], toolkit.current)), end)
        if end <= start:
            return messages
        exchanges = []  # a model reply, with the results of the tools it called
        for message in messages[start:end]:
            if isinstance(message, ModelResponse) or not exchanges:
                exchanges.append([])
            exchanges[-1].append(message)
        kept, removed = [], 0
        for response, *requests in exchanges:
            calls = tool_calls(response)
            ids = {call.tool_call_id for call in calls if call.tool_name in kept_tools}
            if not ids:
                removed += 1 + len(requests)
            elif len(ids) == len(calls):
                kept += [response, *requests]
            else:
                kept.append(replace(response, parts=[part for part in calls if part.tool_call_id in ids]))
                kept += [replace(request, parts=parts) for request in requests
                         if (parts := [part for part in request.parts if getattr(part, "tool_call_id", None) in ids])]
                removed += 1
        left = any(toolkit.still_needs(item_id) for item_id in toolkit.batch)
        note = (f"{PROGRESS} {toolkit.recorded()} of {len(toolkit.batch)} items are recorded, and their messages "
                "were removed." + ("" if end < len(messages) else " Call next_item for the next item." if left
                                   else " Every item is recorded: call submit_findings."))
        print(f"=====Removed {removed} message(s) of recorded items from the conversation=====")
        return (without_progress(messages[:start]) + without_progress(kept)
                + [ModelRequest(parts=[UserPromptPart(note)])] + messages[end:])
    return prune

async def run_items(agent, work, skills, toolkit, run, items):
    toolkit.start(run, items, lambda entry: keep_answer(run, work, toolkit, entry))
    run.expected = [(item_id, skill) for item_id in items for skill in toolkit.needs[item_id]]
    skill_tools = SkillTools({skill.name: skill for skill in skills}, load_once=True, with_examples=True)
    run.skills_loaded, run.skill_files_read = skill_tools.loaded, skill_tools.files_read

    def review(submitted):
        if not toolkit.handed:
            return ["no item"]
        missing = run.unanswered()
        if run.first_missing is None:
            run.first_missing = missing
        return missing

    def reject(missing):
        if not toolkit.handed:
            return (f"Not accepted: you haven't checked any item yet. Call next_item with {run.url}, record an answer "
                    "for each skill it needs with record_answer, and go on until next_item says every item is "
                    "recorded. Then call submit_findings.")
        left = len(dict.fromkeys(item_id for item_id, _ in missing))
        return (f"Not accepted yet: {left} item(s) still need answers. Call next_item with {run.url} to go on, record "
                "an answer for each skill with record_answer, and call submit_findings once next_item says every "
                "item is recorded.")

    kept_tools = {"load_skill", "read_skill_file", *toolkit.page_tools}
    submitted, usage = await run_agent_async(
        work.analyzers["links"].args, agent.system_prompt(skills), task_prompt(run),
        toolkit.offered_tools() + skill_tools.tools() + [toolkit.next_item_tool(), toolkit.record_tool(agent.finding)],
        SubmitFindings, review, reject, request_limit=agent.max_steps + FLEET.steps_per_answer * len(run.expected),
        process_history=recorded_items_out(toolkit, kept_tools), output_name="submit_findings", text_submission=True,
        cut_off_message=CUT_OFF_MESSAGE, max_rejections=CONFIG.agent.max_rejections + len(items) // 10)
    run.usage = usage
    if submitted is None:
        run.error = "it stopped before it handed in all its findings"
    if not run.answers:
        run.status = "failed"
        run.error = run.error or "none of its findings could be accepted"
    elif len(run.answers) < len(run.expected):
        run.status = "partly answered"  # the orchestrator can send the task again for the rest
    run.findings = [finding(run, work, pair, answer) for pair, answer in run.answers.items()]

    questions = work.questions
    questions.answers.by_id.update({element_id(*pair): answer for pair, answer in run.answers.items()})
    questions.answers.omitted.update(element_id(*pair)
                                     for pair in (run.first_missing if run.first_missing is not None else run.expected))

async def run_task(agent, work, skill_names, task_id):
    skills = [agent.skills[name] for name in skill_names]
    toolkit = agent.toolkit(work, skills)
    first = [check for check in toolkit.checks if work.collected[check] is None]
    for check in toolkit.checks:
        await work.collect(check)
    collect_s = sum(work.collect_s[check] for check in first)

    def new_run(**fields):
        return Run(task_id, agent.name, skill_names, work.url, **fields)

    unavailable = toolkit.unavailable()
    if unavailable:
        status, error, lands_on = unavailable
        return [new_run(status=status, error=error, lands_on=lands_on, collect_s=collect_s)]
    ids = toolkit.prepare()
    pairs = sum(len(toolkit.needs[item_id]) for item_id in ids)
    if not ids:
        # nothing for the model to do: no such items on the page, or all judged on earlier pages
        work.questions.record()
        return [new_run(status="nothing new" if toolkit.total else "nothing to check", known=toolkit.total,
                        collect_s=collect_s)]
    run = new_run(known=toolkit.total - pairs, collect_s=collect_s)
    print(f"\n====={agent.name} on {work.url}, task {task_id}: {len(ids)} item(s)=====")
    await run_items(agent, work, skills, toolkit, run, ids)
    work.questions.forget({element_id(*pair) for pair in run.dropped})
    work.questions.record()
    return [run]

def task_status(runs):
    # how a task ended, over its runs
    statuses = {run.status for run in runs}
    if len(statuses) == 1:
        return statuses.pop()
    if not any(run.answers for run in runs):
        return "failed"
    return "partly answered"  # some parts were answered, and some weren't

def main():
    trace_to_logfire()
    agents = {agent.key: agent for agent in AGENTS.values()}
    parser = argparse.ArgumentParser(description="Run one of the fleet's agents on one page and print its findings.")
    parser.add_argument("agent", choices=agents, help="which agent, by its file's name")
    parser.add_argument("url", help="the page to check")
    parser.add_argument("--skills", nargs="+", metavar="SKILL",
                        help="the skills to load (default: all of the agent's)")
    parser.add_argument("--backend", choices=BACKENDS, default="ollama",
                        help="where the agent's model runs: ollama (a local model, the default) or gemini (Google's "
                             "API, in the cloud)")
    parser.add_argument("--model", help="the agent's model; defaults to "
                                        + ", ".join(f"{model} for {backend}"
                                                    for backend, model in CONFIG.models.model_dump().items()))
    parser.add_argument("--no-think", action="store_true",
                        help="ollama only: the agent answers without thinking first, which is much faster on a CPU")
    parser.add_argument("--max-output-tokens", type=int, default=FLEET.max_output_tokens,
                        help="ollama only: the most tokens the agent may write per turn, thinking included "
                             f"(default {FLEET.max_output_tokens})")
    args = parser.parse_args()
    if urlsplit(args.url).scheme not in ("http", "https") or not urlsplit(args.url).hostname:
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
    args.model = agent_model(parser, args.backend, args.model, args.no_think, args.max_output_tokens)
    agent = agents[args.agent]
    unknown = [skill for skill in args.skills or [] if skill not in agent.skills]
    if unknown:
        parser.error(f"{agent.name} has no skill {', '.join(unknown)}; its skills are {', '.join(agent.skills)}")

    remember_results()
    work = PageWork(lambda check: page_args(check, args.url, args.backend, args.model, args.no_think,
                                            args.max_output_tokens))
    runs = asyncio.run(run_task(agent, work, list(dict.fromkeys(args.skills or agent.skills)), "t1"))
    print(f"\n====={agent.name} on {args.url}: {task_status(runs)}=====")
    for run in runs:
        for found in run.findings:
            rated = f" | {found['impact']}" if found["impact"] else ""
            print(f"{found['item_id']} {found['skill']}: {found['verdict']}{rated} | {found['reason']}")
        for item_id, skill in run.unanswered():
            why = run.refused.get((item_id, skill))
            print(f"{item_id} {skill}: unanswered" + (f" ({why})" if why else ""))
    print(json.dumps([run.record() for run in runs], indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
