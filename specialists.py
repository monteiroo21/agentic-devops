import argparse
import asyncio
import json
import time
from argparse import Namespace
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from agent import run_agent_async
from analyzers.accessibility import QUESTIONS as ACCESSIBILITY_QUESTIONS, AccessibilityAnalyzer
from analyzers.links import QUESTIONS as LINK_QUESTIONS, LinkAnalyzer, LinkNote
from common import CONFIG, Impact, Note, Verdict, read_skill, skill_body, trace_to_logfire
from tools.links import remember_results
from workflow import Questions

# The fleet's workers: each specialist is a small model that checks one thing on a page. It calls its own tool to see
# what to judge, and hands in its answers with a submit tool. Each one is defined in its own file in
# agents/specialists; this module runs them

FLEET = CONFIG.fleet
CHECKS = {"links": LinkAnalyzer, "accessibility": AccessibilityAnalyzer}
QUESTION_NAMES = {"links": LINK_QUESTIONS, "accessibility": ACCESSIBILITY_QUESTIONS}  # what each analyzer can build
SKILLS = {**LinkAnalyzer.skills, **AccessibilityAnalyzer.skills}
USAGE = ("llm_requests", "input_tokens", "output_tokens", "truncated_replies", "text_replies", "rejected_submissions")

# What a specialist hands in. Objects, unlike the workflow's lists: the submit tool's schema names each field for the
# model, which a list's positions can't

class SpecialistVerdict(BaseModel):
    id: str = Field(description="The item's id, exactly as the tool returned it.")
    reason: str = Field(description="What in the item decides the verdict, in a few words.")
    verdict: Literal["pass", "fail", "unsure"]

    def answer(self):
        return Verdict(id=self.id, reason=self.reason, verdict=self.verdict)

class SpecialistNote(BaseModel):
    id: str = Field(description="The item's id, exactly as the tool returned it.")
    note: str = Field(description="One short sentence: what is wrong, and who it affects.")
    suggested_fix: str = Field("", description="How to fix it, in a few words. Empty if you can't tell.")

    def answer(self):
        return Note(id=self.id, note=self.note, suggested_fix=self.suggested_fix or None)

class SpecialistLinkNote(BaseModel):
    id: str = Field(description="The item's id, exactly as the tool returned it.")
    note: str = Field(description="One short sentence: what is wrong.")
    likely_intentional: bool = Field(description="True only if the link shows that the problem is deliberate.")
    impact: Impact = Field(description="How much the problem hurts the site's visitors.")  # pyright: ignore[reportInvalidTypeForm]
    suggested_fix: str = Field("", description="How to fix it, in a few words. Empty if you can't tell.")

    def answer(self):
        return LinkNote(id=self.id, note=self.note, likely_intentional=self.likely_intentional, impact=self.impact,
                        suggested_fix=self.suggested_fix or None)

class SubmitVerdicts(BaseModel):
    """Hand in your verdicts: one for every id the tool returned."""
    verdicts: list[SpecialistVerdict]

    def entries(self):
        return self.verdicts

class SubmitNotes(BaseModel):
    """Hand in your notes: one for every id the tool returned."""
    notes: list[SpecialistNote]

    def entries(self):
        return self.notes

class SubmitLinkNotes(BaseModel):
    """Hand in your notes: one for every id the tool returned."""
    notes: list[SpecialistLinkNote]

    def entries(self):
        return self.notes

# What a specialist's file can name under answer: the submit tool it hands its answers in with, and their shape
ANSWERS = {"verdicts": ("submit_verdicts", SubmitVerdicts), "notes": ("submit_notes", SubmitNotes),
           "link-notes": ("submit_notes", SubmitLinkNotes)}
TOOL_ARGS = "\n\nArgs:\n    url: The URL of the page you were asked to check."  # the evidence tool's one argument

@dataclass(frozen=True)
class Specialist:
    name: str  # the kind of question it answers, as the analyzers name it, like alt-text
    description: str  # what it does, in the orchestrator's list of specialists
    check: str  # links or accessibility: the analyzer that collects what its tool returns
    skill: str  # how to judge: put in its prompt where {skill} is
    answer: str  # what it hands in, one of ANSWERS
    tool: str  # its evidence tool's name
    tool_description: str
    prompt: str  # its system prompt, with {skill} where the skill goes

    @property
    def submit(self):
        return ANSWERS[self.answer][0]

    @property
    def submission(self):
        return ANSWERS[self.answer][1]

def load_specialist(path):
    # a specialist's file: frontmatter that says what it is, and its system prompt as the body
    front, prompt = read_skill(path)
    tool = front.get("tool") or {}
    missing = ([key for key in ("name", "description", "check", "skill", "answer") if not front.get(key)]
               + [f"tool.{key}" for key in ("name", "description") if not tool.get(key)])
    if missing:
        raise ValueError(f"{path} has no {', '.join(missing)}")
    name, check, skill, answer = front["name"], front["check"], front["skill"], front["answer"]
    problems = []
    if name != path.stem:
        problems.append(f"its name, {name!r}, must be the name of its file")
    if check not in CHECKS:
        problems.append(f"check must be one of {', '.join(CHECKS)}")
    elif name not in QUESTION_NAMES[check]:
        problems.append(f"the {check} analyzer has no questions named {name!r}; it has {', '.join(QUESTION_NAMES[check])}")
    elif skill not in CHECKS[check].skills:
        problems.append(f"there is no {check} skill named {skill!r}")
    if answer not in ANSWERS:
        problems.append(f"answer must be one of {', '.join(ANSWERS)}")
    else:
        problems += [f"its prompt doesn't mention {word}" for word in ("{skill}", tool["name"], ANSWERS[answer][0])
                     if word not in prompt]
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return Specialist(name, front["description"], check, skill, answer, tool["name"], tool["description"], prompt)

def load_specialists(folder, names):
    # the specialists the config lists, in its order, which is the order the orchestrator sees them in
    if len(set(names)) != len(names):
        raise ValueError(f"the config lists a specialist twice: {', '.join(names)}")
    unknown = [name for name in names if not (folder / f"{name}.md").exists()]
    if unknown:
        raise ValueError(f"no file in {folder} for {', '.join(unknown)}")
    return {name: load_specialist(folder / f"{name}.md") for name in names}

SPECIALISTS = load_specialists(FLEET.agents / "specialists", FLEET.specialists)

def page_args(check, url, model, no_think, max_output_tokens, site_pages=()):
    # what a page's analyzer and its specialists run with. site_pages: the site's pages that are checked on their
    # own, so one the browser lands on isn't judged twice
    return Namespace(url=url, analyzer=CHECKS[check].name, backend="ollama", model=model, no_think=no_think,
                     max_output_tokens=max_output_tokens, output=None, checked_elsewhere=set(site_pages))

class PageWork:
    # one check's work on one page, shared by that check's specialists: the analyzer collects the page once, and the
    # questions keep every element and answer for the page's report. memory, shared across pages, gives an element
    # judged on an earlier page the answer it got there
    def __init__(self, check, args, memory=None):
        self.analyzer = CHECKS[check](args)
        self.questions = Questions(None, self.analyzer.instructions, memory, args.url)
        self.collected = None  # True once collected, False if there was nothing to judge

    async def collect(self):
        if self.collected is None:
            self.collected = await asyncio.to_thread(self.analyzer.collect)
        return self.collected

@dataclass
class Run:
    # one specialist's run on one page
    specialist: str
    url: str
    status: str = "checked"  # checked, nothing to check, nothing new, lands on another page, or failed
    lands_on: str | None = None
    error: str | None = None
    expected: list[str] = field(default_factory=list)  # the ids its tool showed it
    known: int = 0  # elements left out of the tool's result, since they were judged on other pages
    answers: dict = field(default_factory=dict)  # by id
    first_missing: list[str] | None = None  # the ids its first submission had no answer for
    invented: int = 0  # answers for ids its tool never showed
    tool_calls: int = 0
    usage: Counter = field(default_factory=Counter)
    collect_s: float = 0.0  # the time the page's tools took, if this run was the first on it

    def record(self):
        return {
            "specialist": self.specialist, "url": self.url, "status": self.status, "lands_on": self.lands_on,
            "error": self.error, "items": len(self.expected), "known": self.known,
            "answered": sum(element_id in self.answers for element_id in self.expected),
            "unanswered": [element_id for element_id in self.expected if element_id not in self.answers],
            "omitted_first": len(self.first_missing or []), "invented": self.invented, "tool_calls": self.tool_calls,
            **{key: self.usage[key] for key in USAGE},
            "model_s": round(self.usage["model_s"], 1), "collect_s": round(self.collect_s, 1),
        }

def same_page(url, other):
    first, second = urlsplit(url), urlsplit(other)
    return ((first.hostname, first.path.rstrip("/"), first.query)
            == (second.hostname, second.path.rstrip("/"), second.query))

def system_prompt(specialist):
    return specialist.prompt.replace("{skill}", skill_body(SKILLS[specialist.skill]))

async def run_specialist(specialist, work):
    # the specialist on one page: its tool shows it the elements not judged before, and it hands in an answer for each
    run = Run(specialist.name, work.analyzer.args.url)
    start = time.perf_counter()
    collected = await work.collect()
    run.collect_s = time.perf_counter() - start
    if not collected:
        lands_on = getattr(work.analyzer, "lands_on", None)
        if lands_on:
            run.status, run.lands_on = "lands on another page", lands_on
        else:
            run.status, run.error = "failed", "the tools couldn't read the page"
        return run

    groups, total = [], 0
    for criterion, ids, entries, options in work.analyzer.questions_for(specialist.name, work.questions):
        total += len(entries)
        for task in work.questions.prepare(criterion, ids, entries, **options):
            groups.append({"what": ids, "items": task.items})
    run.expected = [item["id"] for group in groups for item in group["items"]]
    run.known = total - len(run.expected)
    if not run.expected:
        # nothing for the model to do: no such elements on the page, or all judged on earlier pages
        run.status = "nothing new" if total else "nothing to check"
        work.questions.record()
        return run

    def evidence(url: str):
        if not same_page(url, run.url):
            return {"error": f"you were asked to check {run.url}: call {specialist.tool} with that URL"}
        run.tool_calls += 1
        shown = groups[0] if len(groups) == 1 else {"groups": groups}
        return {"page": run.url, **shown, **({"already_judged_elsewhere": run.known} if run.known else {})}

    evidence.__doc__ = specialist.tool_description + TOOL_ARGS

    def review(submitted):
        # keeps every valid answer, from this submission and the earlier ones, and returns the ids still without one
        if not run.tool_calls:
            return ["no tool call"]
        for entry in submitted.entries():
            if entry.id not in run.expected:
                run.invented += 1
            elif entry.id not in run.answers:
                run.answers[entry.id] = entry.answer()
        missing = [element_id for element_id in run.expected if element_id not in run.answers]
        if run.first_missing is None:
            run.first_missing = missing
        return missing

    def reject(missing):
        if not run.tool_calls:
            return (f"Not accepted: you haven't called {specialist.tool} yet. Call it with {run.url}, judge every item "
                    f"it returns, then call {specialist.submit} again.")
        return (f"Not accepted yet: these ids have no answer: {', '.join(missing)}. The answers you gave are kept. "
                f"Call {specialist.submit} again, with answers for only these ids.")

    submitted, usage = await run_agent_async(
        work.analyzer.args, system_prompt(specialist), f"Check the page {run.url}.",
        [Tool(evidence, name=specialist.tool)], specialist.submission, review, reject,
        request_limit=FLEET.specialist_max_requests, output_name=specialist.submit)
    run.usage = usage
    if submitted is None:
        run.error = "it stopped before it handed in all its answers"
        if not run.answers:
            run.status = "failed"

    work.questions.answers.by_id.update(run.answers)
    work.questions.answers.omitted.update(run.first_missing if run.first_missing is not None else run.expected)
    work.questions.record()
    return run

def main():
    trace_to_logfire()
    parser = argparse.ArgumentParser(description="Run one of the fleet's specialists on one page and print its answers.")
    parser.add_argument("specialist", choices=SPECIALISTS, help="which specialist")
    parser.add_argument("url", help="the page to check")
    parser.add_argument("--model", default=CONFIG.models.ollama,
                        help=f"the specialist's Ollama model (default {CONFIG.models.ollama})")
    parser.add_argument("--no-think", action="store_true",
                        help="the specialist answers without thinking first, which is much faster on a CPU")
    parser.add_argument("--max-output-tokens", type=int, default=CONFIG.ollama.max_output_tokens,
                        help="the most tokens the specialist may write per turn, thinking included "
                             f"(default {CONFIG.ollama.max_output_tokens})")
    args = parser.parse_args()
    if urlsplit(args.url).scheme not in ("http", "https") or not urlsplit(args.url).hostname:
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")

    remember_results()
    specialist = SPECIALISTS[args.specialist]
    work = PageWork(specialist.check,
                    page_args(specialist.check, args.url, args.model, args.no_think, args.max_output_tokens))
    run = asyncio.run(run_specialist(specialist, work))
    print(f"\n====={specialist.name} on {args.url}: {run.status}=====")
    for element_id in run.expected:
        answer = run.answers.get(element_id)
        if answer is None:
            print(f"{element_id}: unanswered")
        elif isinstance(answer, Verdict):
            print(f"{element_id}: {answer.verdict} | {answer.reason}")
        else:
            print(f"{element_id}: note | {answer.note}")
    print(json.dumps(run.record(), indent=2))

if __name__ == "__main__":
    main()
