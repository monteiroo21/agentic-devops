import argparse
import asyncio
import json
import re
import time
from argparse import Namespace
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from agent import run_agent_async
from analyzers.accessibility import AccessibilityAnalyzer
from analyzers.links import LinkAnalyzer
from common import CONFIG, IMPACTS, Impact, Verdict, read_skill, skill_files, skill_instructions, trace_to_logfire
from toolkits import TOOLKITS, element_id
from tools.axe import wcag_number
from tools.links import remember_results
from workflow import Questions

FLEET = CONFIG.fleet
EXAMPLES = "references/examples.md"  # a skill's examples, which its agent's prompt holds with it
CHECKS = {"links": LinkAnalyzer, "accessibility": AccessibilityAnalyzer}  # a skill's category is one of these
USAGE = ("llm_requests", "input_tokens", "output_tokens", "truncated_replies", "text_replies", "text_submissions",
         "replies_recorded", "rejected_submissions", "cut_off_retries")
IMPACT_IN_TEXT = re.compile(rf"[\s.;,]*\bimpact\s*[:=-]\s*({'|'.join(IMPACTS)})\b\.?", re.IGNORECASE)

class RatedVerdict(Verdict):
    impact: Impact | None = None  # pyright: ignore[reportInvalidTypeForm]
    suggested_fix: str | None = None

# the placeholders of the examples of a fail and an unsure that the agents are shown
PLACEHOLDERS = {"reason": "<what shows the problem, in a few words>", "unsure_reason": "<what is missing to decide>",
                "impact": "<critical, serious, moderate or minor>", "suggested_fix": "<how to fix it, in a few words>"}

# A fail or an unsure as record_findings takes it, under its item's id and then its skill, where a pass is the word
# "pass": each item and skill has one key, so a call can't give it both a pass and a fail
class FindingAnswer(BaseModel):
    reason: str = Field(description="What decides it, in a few words: for a fail, the fact that shows the problem; for "
                                    "an unsure, what is missing to decide.")
    verdict: Literal["fail", "unsure"]
    impact: Literal[*IMPACTS, ""] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="For a fail: how much the problem hurts the site's visitors, as the skill's Impact says. Empty "
                    "for an unsure.")
    suggested_fix: str = Field("", description="For a fail: how to fix it, in a few words. Empty if you can't tell.")

    @model_validator(mode="before")
    @classmethod
    def no_placeholders(cls, data):
        # a placeholder copied as it is counts as left out, so that a reason is asked for, not kept
        if isinstance(data, dict):
            data = {key: "" if isinstance(value, str) and value.strip() in PLACEHOLDERS.values() else value
                    for key, value in data.items()}
        return data

    @model_validator(mode="before")
    @classmethod
    def impact_given(cls, data):
        if isinstance(data, dict) and data.get("impact") is None:
            data = {**data, "impact": ""}  # left out, or null
        return data

    @field_validator("impact", mode="before")
    @classmethod
    def plain_impact(cls, value):
        # one that isn't an impact, like the example's placeholder, counts as left out: the fail is kept and asked for
        # its impact again, rather than the whole call refused, which would lose every answer in it
        if isinstance(value, str):
            value = value.strip().lower()
            return value if value in IMPACTS else ""
        return value

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

# One answer as the code keeps it: record_findings makes it from a "pass", or from a FindingAnswer, with the item and
# skill it was given under
class AgentFinding(BaseModel):
    GIVEN: ClassVar = FindingAnswer  # what record_findings takes for a fail or an unsure
    # what a fail and an unsure look like, for the prompt and for a fail or unsure given as a bare word
    FAIL_EXAMPLE: ClassVar = {"reason": PLACEHOLDERS["reason"], "verdict": "fail", "impact": PLACEHOLDERS["impact"],
                              "suggested_fix": PLACEHOLDERS["suggested_fix"]}
    UNSURE_EXAMPLE: ClassVar = {"reason": PLACEHOLDERS["unsure_reason"], "verdict": "unsure"}
    item_id: str
    skill: str
    reason: str = ""
    verdict: Literal["pass", "fail", "unsure"]
    unreadable: str = ""  # why the answer as it was given couldn't be read, so it is refused
    impact: Literal[*IMPACTS, ""] = ""  # pyright: ignore[reportInvalidTypeForm]
    suggested_fix: str = ""

    @classmethod
    def fail_example(cls):
        return json.dumps(cls.FAIL_EXAMPLE, ensure_ascii=False)

    @classmethod
    def from_given(cls, item_id, skill, given):
        # one answer as record_findings was sent it, read on its own, so that one written wrong is refused alone: when
        # the call's schema checked them all, one such answer made the call refused with every answer in it, and qwen
        # wrote it all again (on 11-19, a pass written as an object, twice, 7 minutes each time)
        verdict = given.get("verdict") if isinstance(given, dict) else given
        verdict = verdict.strip().lower() if isinstance(verdict, str) else None
        if verdict == "pass":
            return cls(item_id=item_id, skill=skill, verdict="pass")  # written as an object too: its other fields go
        if verdict in ("fail", "unsure") and not isinstance(given, dict):
            return cls(item_id=item_id, skill=skill, verdict=verdict)  # a bare word: refused for its missing reason
        if verdict in ("fail", "unsure"):
            fields = {key: value for key, value in given.items() if value is not None}
            try:
                answer = cls.GIVEN.model_validate({"reason": "", **fields, "verdict": verdict})
            except ValidationError:
                pass
            else:
                return cls(item_id=item_id, skill=skill, **answer.model_dump())
        return cls(item_id=item_id, skill=skill, verdict="unsure",
                   unreadable=f'not an answer: give "pass", or for a fail an object like {cls.fail_example()}')

    def answer(self, element):
        failed = self.verdict == "fail"
        return RatedVerdict(id=element, reason=self.reason, verdict=self.verdict,
                            impact=(self.impact or None) if failed else None,
                            suggested_fix=(self.suggested_fix or None) if failed else None)

    def problem(self):
        if self.unreadable:
            return self.unreadable
        # a fail or an unsure given as a bare word, like "fail", has no reason either
        if self.verdict == "fail" and not self.reason.strip():
            return f"a fail needs its reason: give it as an object, like {self.fail_example()}"
        if self.verdict == "unsure" and not self.reason.strip():
            return ("an unsure needs its reason: give it as an object, like "
                    + json.dumps(self.UNSURE_EXAMPLE, ensure_ascii=False))
        return None

    def missing_impact(self):
        # a fail kept without the impact it needs, which a later answer can add
        return self.verdict == "fail" and not self.impact

class SubmitFindings(BaseModel):
    """Finish the task, once record_findings shows nothing unanswered."""
    model_config = ConfigDict(extra="forbid")

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
        # the skill as its agent's prompt holds it, under its name alone: qwen read "link-purpose (WCAG 2.4.4)" as one
        # name. Loaded with load_skill instead, the skills were read again at the start of every part, about 3.5
        # minutes each for the link agent's four on 11-19: from one part to the next, Ollama keeps only the start the
        # parts share, the prompt and the task
        about = f" (WCAG {', '.join(self.wcag)})" if self.wcag else ""
        parts = [f"## {self.name}", f"{self.description}{about}", skill_instructions(self)]
        examples = self.path.parent / EXAMPLES
        if examples.exists():
            parts += ["### Examples", examples.read_text().strip()]
        return "\n\n".join(parts)

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
    tools: tuple[str, ...]  # its tools' names, besides the ones every agent has: record_findings and submit_findings
    max_steps: int  # its model calls in a run
    prompt: str  # its system prompt, with {skills} where the skills of a task go, {answers_example} an example of
    # answers with them, and {fail_example} what a fail looks like
    toolkit: type  # what gives it its tools

    def system_prompt(self, skills, answers_example):
        return (self.prompt.replace("{skills}", "\n\n".join(skill.section() for skill in skills))
                .replace("{answers_example}", answers_example).replace("{fail_example}", AgentFinding.fail_example()))

def load_agent(path, skills_folder):
    front, prompt = read_skill(path)
    missing = [key for key in ("name", "description", "skills", "tools", "max_steps") if not front.get(key)]
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
        others = [file for file in skill_files(skills[skill]) if file != EXAMPLES]
        if others:
            problems.append(f"the {skill} skill has files besides its examples, {', '.join(others)}, which its "
                            "agent's prompt doesn't hold")
    toolkit = TOOLKITS.get(name)
    if toolkit is None:
        problems.append(f"no toolkit gives the tools of an agent named {name!r}; the toolkits are for "
                        + ", ".join(TOOLKITS))
    elif set(toolkit.tools_given) != set(tools):
        problems.append(f"its toolkit gives {', '.join(toolkit.tools_given)}, but its file lists {', '.join(tools)}")
    if not isinstance(front["max_steps"], int) or front["max_steps"] < 2:
        problems.append("max_steps must be a number, at least 2: a tool call and the submission")
    problems += [f"its prompt doesn't mention {word}"
                 for word in ("{skills}", "{answers_example}", "{fail_example}", "record_findings", "submit_findings")
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
                     prompt, toolkit)

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
    batch: str | None = None  # like 2 of 3, when the task's items were split into runs
    status: str = "checked"  # checked, nothing to check, nothing new, lands on another page, partly answered or failed
    lands_on: str | None = None
    error: str | None = None
    expected: list[tuple[str, str]] = field(default_factory=list)  # the (item id, skill) pairs it was given
    dropped: list[tuple[str, str]] = field(default_factory=list)  # pairs taken out of expected, made needless by a fail
    known: int = 0  # pairs left out of what it was given, since they were answered before
    answers: dict = field(default_factory=dict)  # by (item id, skill)
    first_missing: list | None = None  # the pairs its first submission had no accepted answer for
    refused: dict = field(default_factory=dict)  # by pair: why its latest answer wasn't accepted, until one is
    needs_impact: list = field(default_factory=list)  # pairs whose fail was kept without its impact, until one is given
    changed: list = field(default_factory=list)  # (pair, earlier verdict, later verdict that replaced it)
    not_replaced: list = field(default_factory=list)  # (pair, recorded verdict, later verdict not taken)
    impact_asked: bool = False  # whether a submission was sent back for those impacts, which is done once
    refusals: int = 0  # answers not accepted, like a pass on a page whose destination it didn't read
    invented: int = 0  # answers for ids or skills its tools never showed
    skill_first: int = 0  # answers given by skill and then by item id, which were read the other way round
    nested: int = 0  # answers given inside the same item's answer for another skill, which were taken out of it
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
            "dropped": [f"{item_id} {skill}" for item_id, skill in self.dropped],
            "no_impact": [f"{item_id} {skill}" for item_id, skill in self.needs_impact],
            "changed": [f"{item_id} {skill}: {before}, then {after}"
                        for (item_id, skill), before, after in self.changed],
            "not_replaced": [f"{item_id} {skill}: {recorded}, then {sent}"
                             for (item_id, skill), recorded, sent in self.not_replaced],
            "omitted_first": len(self.first_missing or []), "refusals": self.refusals, "invented": self.invented,
            "skill_first": self.skill_first, "nested": self.nested, "tool_calls": dict(self.tool_calls), "destinations_read": self.read,
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
            "reason": answer.reason, "verdict": answer.verdict, "impact": answer.impact,
            "suggested_fix": answer.suggested_fix}

CUT_OFF_MESSAGE = ("Your last reply was cut off at the output limit before it was complete, so none of it was taken. "
                   "Send it again in smaller calls: at most 5 items per record_findings call.")

def task_prompt(run, toolkit):
    part = " Its items are split into parts: the tools show only this part's." if run.batch else ""
    return f"Task {run.task}: check the page {run.url}.{part}{toolkit.task_text()}"

def not_asked_why(run, work, toolkit, pair):
    # worded the same for every answer it applies to, so that record_findings says each why once, with its answers
    item_id, skill = pair
    if item_id in toolkit.batch:
        failed = [earlier for (answered, earlier), answer in run.answers.items()
                  if answered == item_id and answer.verdict == "fail" and skill in toolkit.needless_after_fail(earlier)]
        if pair in run.dropped and failed:
            return f"no answer is needed after the item's {failed[0]} fail"
        return "the tools didn't list this skill for the item: answer only the skills they listed for it"
    if element_id(*pair) in work.questions.elements:
        return "this part doesn't ask it"  # in another part, or answered before
    return "there is no item with this id in this part"

# by a later verdict, the earlier ones it replaces for the same item and skill: only a more decided answer replaces
# one, and nothing replaces a fail. qwen's later fails were right each time seen (cargo-duo on 11-17, the 2026 calendar
# and Returns & refunds on 11-18), and a pass copied from an earlier call shouldn't undo one
REPLACES = {"fail": ("pass", "unsure"), "pass": ("unsure",)}

def keep_findings(run, work, toolkit, findings):
    expected = set(run.expected)
    given = {(entry.item_id, entry.skill) for entry in findings if not entry.unreadable}
    kept, refused, not_asked, changed = 0, [], {}, []
    for entry in findings:
        pair = (entry.item_id, entry.skill)
        if pair not in expected:
            if element_id(*pair) not in work.questions.elements:
                run.invented += 1  # an item answered before, shown again for its page's outline, isn't
            not_asked[pair] = not_asked_why(run, work, toolkit, pair)
            continue
        recorded = run.answers.get(pair)
        if entry.unreadable and recorded is not None:
            continue  # the answer recorded before stays
        if recorded is not None and entry.verdict == recorded.verdict:
            if pair in run.needs_impact and entry.impact:
                run.answers[pair] = recorded.model_copy(update={
                    "impact": entry.impact, "suggested_fix": entry.suggested_fix or recorded.suggested_fix})
                run.needs_impact.remove(pair)
                kept += 1
            continue
        if recorded is not None and recorded.verdict not in REPLACES.get(entry.verdict, ()):
            run.not_replaced.append((pair, recorded.verdict, entry.verdict))  # not said back, only kept for scoring
            continue
        why = entry.problem() or toolkit.refused(entry)
        if why:
            run.refused[pair] = why  # an answer recorded before stays
            run.refusals += 1
            refused.append((pair, why))
            continue
        run.answers[pair] = entry.answer(element_id(*pair))
        run.refused.pop(pair, None)
        kept += 1
        if recorded is not None:
            run.changed.append((pair, recorded.verdict, entry.verdict))
            changed.append((pair, recorded.verdict, entry.verdict))
        if entry.missing_impact():
            run.needs_impact.append(pair)
        if entry.verdict == "fail":
            # a fail can make the item's other skills needless, like a link's redirect after its soft-404, so they
            # aren't asked. One answered before or in the same call keeps its answer: on 11-19 a false soft-404 fail
            # dropped the right text-mismatch fail given with it, twice (link-17, and Returns & refunds)
            for skill in toolkit.needless_after_fail(entry.skill):
                later = (entry.item_id, skill)
                if later in expected and later not in run.answers and later not in given:
                    expected.discard(later)
                    run.expected.remove(later)
                    run.refused.pop(later, None)
                    run.dropped.append(later)
    return kept, refused, list(not_asked.items()), changed

async def run_batch(agent, work, skills, toolkit, run, batch):
    toolkit.start(run, batch, lambda findings: keep_findings(run, work, toolkit, findings))
    run.expected = [(item_id, skill) for item_id in batch for skill in toolkit.needs[item_id]]

    def review(submitted):
        if toolkit.uncalled():
            return ["no tool call"]
        missing = run.unanswered()
        if run.first_missing is None:
            run.first_missing = missing
        if run.needs_impact and not run.impact_asked:
            run.impact_asked = True
            missing = missing + [pair for pair in run.expected if pair in run.needs_impact]
        return missing

    def reject(missing):
        uncalled = toolkit.uncalled()
        if uncalled:
            return (f"Not accepted: you haven't called {' and '.join(uncalled)} yet. Call it with {run.url}, check "
                    "every item it lists, record your findings with record_findings, then call submit_findings.")
        unanswered, refused, impacts = {}, {}, {}  # refused: by why, so that each is said once
        for item_id, skill in missing:
            if (item_id, skill) in run.needs_impact:
                impacts.setdefault(item_id, []).append(skill)
            elif (item_id, skill) in run.refused:
                refused.setdefault(run.refused[(item_id, skill)], {}).setdefault(item_id, []).append(skill)
            else:
                unanswered.setdefault(item_id, []).append(skill)

        def listed(by_item):
            return "; ".join(f"{item_id} ({', '.join(skills)})" for item_id, skills in by_item.items())

        parts = ["Not accepted yet."]
        if unanswered:
            parts.append(f"These items have no finding for these skills: {listed(unanswered)}.")
        if refused:
            parts.append("These findings weren't accepted:\n"
                         + "\n".join(f"- {listed(by_item)}: {why}" for why, by_item in refused.items()) + "\n")
        if impacts:
            parts.append(f'These fails were recorded without their impact: {listed(impacts)}. Give each again as a '
                         'finding, with its "impact".')
        parts.append("The findings you gave for the others are kept, so don't send them again. Record these with "
                     "record_findings, then call submit_findings.")
        return " ".join(parts)

    def read_reply(text):
        result = toolkit.record_reply(AgentFinding, text)
        if result is None:
            return None
        told = (f"Your reply held answers, so they were recorded as if you had sent them to record_findings, which "
                f"returned: {json.dumps(result, ensure_ascii=False)}. Send the rest by calling record_findings, not "
                "in your reply, then call submit_findings.")
        return told, "error" not in result and not result["unanswered"]

    submitted, usage = await run_agent_async(
        work.analyzers["links"].args, agent.system_prompt(skills, toolkit.answers_example(AgentFinding)),
        task_prompt(run, toolkit),
        toolkit.offered_tools() + [toolkit.record_tool(AgentFinding)], SubmitFindings, review,
        reject, request_limit=agent.max_steps, output_name="submit_findings", text_submission=True,
        cut_off_message=CUT_OFF_MESSAGE, read_reply=read_reply, stuck=lambda: toolkit.stuck)
    run.usage = usage
    if submitted is None:
        run.error = toolkit.stuck or "it stopped before it handed in all its findings"
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
    work.questions.forget({element_id(*pair) for run in runs for pair in run.dropped})
    work.questions.record()
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
