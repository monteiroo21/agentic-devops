import argparse
import asyncio
import json
import time
from argparse import Namespace
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, Field
from agent import run_agent_async
from analyzers.accessibility import AccessibilityAnalyzer
from analyzers.links import LinkAnalyzer
from common import CONFIG, Impact, Verdict, read_skill, trace_to_logfire
from toolkits import TOOLKITS, element_id
from tools.axe import wcag_number
from tools.links import remember_results
from workflow import Questions

FLEET = CONFIG.fleet
CHECKS = {"links": LinkAnalyzer, "accessibility": AccessibilityAnalyzer}  # a skill's category is one of these
USAGE = ("llm_requests", "input_tokens", "output_tokens", "truncated_replies", "text_replies", "text_submissions",
         "rejected_submissions")

class RatedVerdict(Verdict):
    impact: Impact | None = None  # pyright: ignore[reportInvalidTypeForm]
    suggested_fix: str | None = None

class AgentFinding(BaseModel):
    item_id: str = Field(description="The item's id, exactly as the tools returned it.")
    skill: str = Field(description="The skill this finding is for, as named under Skills.")
    reason: str = Field(description="What decides the verdict, in a few words.")
    verdict: Literal["pass", "fail", "unsure"]

    def answer(self, element):
        return Verdict(id=element, reason=self.reason, verdict=self.verdict)

    def problem(self):
        return None  # why the finding can't be kept as it is, if it can't

class RatedAgentFinding(AgentFinding):
    impact: Impact | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="For a fail: how much the problem hurts the site's visitors. Leave it out otherwise.")
    suggested_fix: str = Field("", description="For a fail: how to fix it, in a few words. Empty if you can't tell.")

    def answer(self, element):
        failed = self.verdict == "fail"
        return RatedVerdict(id=element, reason=self.reason, verdict=self.verdict,
                            impact=self.impact if failed else None,
                            suggested_fix=(self.suggested_fix or None) if failed else None)

    def problem(self):
        if self.verdict == "fail" and self.impact is None:
            return "a fail needs its impact"
        return None

class SubmitFindings(BaseModel):
    """Hand in your findings: one for every item and skill the tools listed."""
    findings: list[AgentFinding]

class SubmitRatedFindings(BaseModel):
    """Hand in your findings: one for every item and skill the tools listed, with the impact and a fix for each
    fail."""
    findings: list[RatedAgentFinding]

ANSWERS = {"findings": SubmitFindings, "rated-findings": SubmitRatedFindings}  # what an agent's file can name

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

    def section(self):
        # what the agent's prompt shows of it, where {skills} is: its instructions, then its examples
        _, body = read_skill(self.path)
        about = f" (WCAG {', '.join(self.wcag)})" if self.wcag else ""
        section = f"## {self.name}{about}\n\n{self.description}\n\n{body}"
        examples = self.path.parent / "references" / "examples.md"
        if examples.exists():
            section += "\n\n### Examples\n\n" + examples.read_text().strip()
        return section

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
    tools: tuple[str, ...]  # its tools' names, besides submit_findings
    max_steps: int  # its model calls in a run
    answer: str  # what it hands in, one of ANSWERS
    prompt: str  # its system prompt, with {skills} where the skills of a task go
    toolkit: type  # what gives it its tools

    @property
    def submission(self):
        return ANSWERS[self.answer]

    def system_prompt(self, skills):
        return self.prompt.replace("{skills}", "\n\n".join(skill.section() for skill in skills))

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
    problems += [f"its prompt doesn't mention {word}" for word in ("{skills}", *tools, "submit_findings")
                 if word not in prompt]
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

def page_args(check, url, model, no_think, max_output_tokens, site_pages=()):
    # what a page's analyzer and its agents run with. site_pages: the site's pages that are checked on their own, so
    # one the browser lands on isn't judged twice. The link agent reads the linked pages itself
    return Namespace(url=url, analyzer=CHECKS[check].name, backend="ollama", model=model, no_think=no_think,
                     max_output_tokens=max_output_tokens, output=None, checked_elsewhere=set(site_pages),
                     read_linked_pages=False)

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
    batch: str | None = None  # like 2 of 3, when the task's items were split into runs
    status: str = "checked"  # checked, nothing to check, nothing new, lands on another page, partly answered or failed
    lands_on: str | None = None
    error: str | None = None
    expected: list[tuple[str, str]] = field(default_factory=list)  # the (item id, skill) pairs it was given
    known: int = 0  # pairs left out of what it was given, since they were answered before
    answers: dict = field(default_factory=dict)  # by (item id, skill)
    first_missing: list | None = None  # the pairs its first submission had no accepted answer for
    refused: dict = field(default_factory=dict)  # by pair: why its latest answer wasn't accepted, until one is
    refusals: int = 0  # answers not accepted, like a pass on a page whose destination it didn't read
    invented: int = 0  # answers for ids or skills its tools never showed
    tool_calls: Counter = field(default_factory=Counter)  # by tool
    read: list[str] = field(default_factory=list)  # the items whose linked page it read
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
            "omitted_first": len(self.first_missing or []), "refusals": self.refusals, "invented": self.invented,
            "tool_calls": dict(self.tool_calls), "destinations_read": self.read,
            **{key: self.usage[key] for key in USAGE},
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

def task_prompt(run, toolkit):
    part = f" Its items are split into parts, and this is part {run.batch}: the tools show only this part's." \
        if run.batch else ""
    return f"Task {run.task}: check the page {run.url}.{part}{toolkit.task_text()}"

async def run_batch(agent, work, skills, toolkit, run, batch):
    toolkit.start(run, batch)
    run.expected = [(item_id, skill) for item_id in batch for skill in toolkit.needs[item_id]]
    expected = set(run.expected)

    def calls_missing():
        return [tool for tool in toolkit.listing if not run.tool_calls[tool]]

    def review(submitted):
        # keeps every accepted answer, from this submission and the earlier ones, and returns the pairs still without one
        if calls_missing():
            return ["no tool call"]
        for entry in submitted.findings:
            pair = (entry.item_id, entry.skill)
            if pair not in expected:
                if element_id(*pair) not in work.questions.elements:
                    run.invented += 1  # an item answered before, shown again for its page's outline, isn't
            elif pair not in run.answers:
                why = entry.problem() or toolkit.refused(entry)
                if why:
                    run.refused[pair] = why
                    run.refusals += 1
                else:
                    run.answers[pair] = entry.answer(element_id(*pair))
                    run.refused.pop(pair, None)
        missing = run.unanswered()
        if run.first_missing is None:
            run.first_missing = missing
        return missing

    def reject(missing):
        uncalled = calls_missing()
        if uncalled:
            return (f"Not accepted: you haven't called {' and '.join(uncalled)} yet. Call it with {run.url}, check "
                    "every item it lists, then call submit_findings again.")
        unanswered = {}
        for item_id, skill in missing:
            if (item_id, skill) not in run.refused:
                unanswered.setdefault(item_id, []).append(skill)
        refused = [f"- {item_id} {skill}: {run.refused[(item_id, skill)]}" for item_id, skill in missing
                   if (item_id, skill) in run.refused]
        parts = ["Not accepted yet."]
        if unanswered:
            parts.append("These items have no finding for these skills: "
                         + "; ".join(f"{item_id} ({', '.join(skills)})" for item_id, skills in unanswered.items())
                         + ".")
        if refused:
            parts.append("These findings weren't accepted:\n" + "\n".join(refused) + "\n")
        parts.append("The findings you gave for the others are kept. Call submit_findings again, with findings for "
                     "only these.")
        return " ".join(parts)

    submitted, usage = await run_agent_async(
        work.analyzers["links"].args, agent.system_prompt(skills), task_prompt(run, toolkit), toolkit.tools(),
        agent.submission, review, reject, request_limit=agent.max_steps, output_name="submit_findings",
        text_submission=True)
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
    questions.record()

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
    size = FLEET.batch_size if toolkit.batched else len(ids)
    batches = [ids[start:start + size] for start in range(0, len(ids), size)]
    runs = []
    for number, batch in enumerate(batches, 1):
        run = new_run(batch=f"{number} of {len(batches)}" if len(batches) > 1 else None,
                      known=toolkit.total - pairs if number == 1 else 0, collect_s=collect_s if number == 1 else 0.0)
        print(f"\n====={agent.name} on {work.url}, task {task_id}" + (f", part {run.batch}" if run.batch else "")
              + f": {len(batch)} item(s)=====")
        await run_batch(agent, work, skills, toolkit, run, batch)
        runs.append(run)
    return runs

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
    parser.add_argument("--model", default=CONFIG.models.ollama,
                        help=f"the agent's Ollama model (default {CONFIG.models.ollama})")
    parser.add_argument("--no-think", action="store_true",
                        help="the agent answers without thinking first, which is much faster on a CPU")
    parser.add_argument("--max-output-tokens", type=int, default=CONFIG.ollama.max_output_tokens,
                        help="the most tokens the agent may write per turn, thinking included "
                             f"(default {CONFIG.ollama.max_output_tokens})")
    args = parser.parse_args()
    if urlsplit(args.url).scheme not in ("http", "https") or not urlsplit(args.url).hostname:
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
    agent = agents[args.agent]
    unknown = [skill for skill in args.skills or [] if skill not in agent.skills]
    if unknown:
        parser.error(f"{agent.name} has no skill {', '.join(unknown)}; its skills are {', '.join(agent.skills)}")

    remember_results()
    work = PageWork(lambda check: page_args(check, args.url, args.model, args.no_think, args.max_output_tokens))
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
