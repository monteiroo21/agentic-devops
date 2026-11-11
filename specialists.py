import argparse
import asyncio
import json
import math
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
from analyzers.links import QUESTIONS as LINK_QUESTIONS, LinkAnalyzer, linked_page_entries
from common import CONFIG, Impact, Note, Verdict, docstring_with, read_skill, skill_body, trace_to_logfire
from tools.links import compact, fetch_page_text, remember_results
from workflow import Questions

FLEET = CONFIG.fleet
CHECKS = {"links": LinkAnalyzer, "accessibility": AccessibilityAnalyzer}
QUESTION_NAMES = {"links": LINK_QUESTIONS, "accessibility": ACCESSIBILITY_QUESTIONS}  # what each analyzer can build
SKILLS = {**LinkAnalyzer.skills, **AccessibilityAnalyzer.skills}
USAGE = ("llm_requests", "input_tokens", "output_tokens", "truncated_replies", "text_replies", "text_submissions",
         "rejected_submissions")

# What a specialist hands in. Objects, unlike the workflow's lists: the submit tool's schema names each field for the
# model, which a list's positions can't

class SpecialistVerdict(BaseModel):
    id: str = Field(description="The item's id, exactly as the tool returned it.")
    reason: str = Field(description="What in the item decides the verdict, in a few words.")
    verdict: Literal["pass", "fail", "unsure"]

    def answer(self):
        return Verdict(id=self.id, reason=self.reason, verdict=self.verdict)

    def problem(self):
        return None  # why the answer can't be kept as it is, if it can't

class RatedVerdict(Verdict):
    impact: Impact | None = None  # pyright: ignore[reportInvalidTypeForm]
    suggested_fix: str | None = None

class SpecialistRatedVerdict(SpecialistVerdict):
    impact: Impact | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="For a fail: how much the problem hurts the site's visitors. Leave it out otherwise.")
    suggested_fix: str = Field("", description="For a fail: how to fix it, in a few words. Empty if you can't tell.")

    def answer(self):
        failed = self.verdict == "fail"
        return RatedVerdict(id=self.id, reason=self.reason, verdict=self.verdict,
                            impact=self.impact if failed else None,
                            suggested_fix=(self.suggested_fix or None) if failed else None)

    def problem(self):
        if self.verdict == "fail" and self.impact is None:
            return "a fail needs its impact"
        return None

class SpecialistNote(BaseModel):
    id: str = Field(description="The item's id, exactly as the tool returned it.")
    note: str = Field(description="One short sentence: what is wrong, and who it affects.")
    suggested_fix: str = Field("", description="How to fix it, in a few words. Empty if you can't tell.")

    def answer(self):
        return Note(id=self.id, note=self.note, suggested_fix=self.suggested_fix or None)

    def problem(self):
        return None

class SubmitVerdicts(BaseModel):
    """Hand in your verdicts: one for every id the tool returned."""
    verdicts: list[SpecialistVerdict]

    def entries(self):
        return self.verdicts

class SubmitRatedVerdicts(BaseModel):
    """Hand in your verdicts: one for every id you were given, with the impact and a fix for each fail."""
    verdicts: list[SpecialistRatedVerdict]

    def entries(self):
        return self.verdicts

class SubmitNotes(BaseModel):
    """Hand in your notes: one for every id the tool returned."""
    notes: list[SpecialistNote]

    def entries(self):
        return self.notes

# What a specialist's file can name under answer: the submit tool it hands its answers in with, and their shape
ANSWERS = {"verdicts": ("submit_verdicts", SubmitVerdicts), "rated-verdicts": ("submit_verdicts", SubmitRatedVerdicts),
           "notes": ("submit_notes", SubmitNotes)}
TOOL_ARGS = "\n\nArgs:\n    url: The URL of the page you were asked to check."  # a shown-items tool's one argument

def same_page(url, other):
    first, second = urlsplit(url), urlsplit(other)
    return ((first.hostname, first.path.rstrip("/"), first.query)
            == (second.hostname, second.path.rstrip("/"), second.query))

def not_this_page(run, tool_name):
    return {"error": f"you were asked to check {run.url}: call {tool_name} with that URL"}

def already_judged(run):
    return {"already_judged_elsewhere": run.known} if run.known else {}

# A specialist's toolkit: the tools it gets on one page, and what they let it hand in

class ShownItems:
    # the usual toolkit: one tool, described in the specialist's file, that shows it every item to judge at once
    def __init__(self, specialist, work, run):
        self.specialist, self.work, self.run = specialist, work, run
        self.first = specialist.tools[0]  # the tool it must call before it hands in its answers
        self.groups = []

    def prepare(self):
        # the items to judge, less those judged on earlier pages: how many there are in all, and the ids still to judge
        questions = self.work.questions
        total = 0
        for criterion, ids, entries, options in self.work.analyzer.questions_for(self.specialist.name, questions):
            total += len(entries)
            for task in questions.prepare(criterion, ids, entries, **options):
                self.groups.append({"what": ids, "items": task.items})
        return total, [item["id"] for group in self.groups for item in group["items"]]

    def tools(self):
        run = self.run

        def evidence(url: str):
            if not same_page(url, run.url):
                return not_this_page(run, self.first)
            run.tool_calls[self.first] += 1
            shown = self.groups[0] if len(self.groups) == 1 else {"groups": self.groups}
            return {"page": run.url, **shown, **already_judged(run)}

        evidence.__doc__ = self.specialist.tool_description + TOOL_ARGS
        return [Tool(evidence, name=self.first)]

    def request_limit(self):
        return FLEET.specialist_max_requests

    def refused(self, entry):
        return None  # why an answer for an item it was given isn't accepted, if it isn't

class LinkedPages(ShownItems):
    # soft-404's tools: the page's working links, then the pages they lead to, which the specialist reads as it
    # chooses. The link checker has already reported the broken links. It may only pass or fail a page it has read
    names = ("get_links", "read_pages")

    def __init__(self, specialist, work, run):
        super().__init__(specialist, work, run)
        self.pages = {}  # by id: a page the links lead to, with the text of the links to it
        self.read = set()  # the ids of the pages it read

    def prepare(self):
        analyzer = self.work.analyzer
        entries = linked_page_entries(analyzer.links)
        links_to = {element_id: kept["links"] for element_id, _, kept in entries}  # the links that lead to each page
        ids, options = LINK_QUESTIONS[self.specialist.name]
        for task in self.work.questions.prepare(self.specialist.name, ids, entries, **options):
            for item in task.items:
                texts = [analyzer.link_info[url]["text"] for url in links_to[item["id"]]
                         if analyzer.link_info[url]["text"]]
                self.pages[item["id"]] = {"id": item["id"], "url": item["url"],
                                          **({"link_text": list(dict.fromkeys(texts))} if texts else {})}
        return len(entries), list(self.pages)

    def tools(self):
        run, pages, analyzer = self.run, self.pages, self.work.analyzer
        limit = FLEET.pages_read_per_call

        def get_links(url: str):
            """Returns the links on the page that work and lead to a web page, by the page they lead to: each of those
            pages with an id, its URL and the text of the links to it. The link checker has already reported the
            broken links. Pages already judged on other pages of the site are left out.

            Args:
                url: The URL of the page you were asked to check.
            """
            if not same_page(url, run.url):
                return not_this_page(run, "get_links")
            run.tool_calls["get_links"] += 1
            return {"page": run.url, "pages": list(pages.values()), **already_judged(run)}

        @docstring_with(limit=limit)
        async def read_pages(ids: list[str]):
            """Reads the pages with these ids, as get_links listed them: the first {limit}, if you give more. Returns
            each one's status, title, first heading and the start of its visible text, and the URL it ended on if it
            redirected.

            Args:
                ids: The ids of the pages to read, like page-3.
            """
            ids = list(dict.fromkeys(ids))
            ids, later = ids[:limit], ids[limit:]
            run.tool_calls["read_pages"] += 1
            results = []
            for page_id in ids:
                if page_id not in pages:
                    results.append({"id": page_id, "error": "get_links listed no page with this id"})
                    continue
                page = await asyncio.to_thread(fetch_page_text, pages[page_id]["url"],
                                               CONFIG.links.workflow_page_text_chars)
                analyzer.fetched[page["url"]] = page  # the page's report counts the pages read
                if page["text"] is not None and page_id not in self.read:
                    self.read.add(page_id)
                    run.read.append(page["url"])
                results.append({"id": page_id, **compact(page, leave_out=("content_type", "truncated"))})
            if later:
                return {"pages": results, "not_read": later,
                        "note": f"only {limit} pages are read per call: call read_pages again for the ones not read"}
            return {"pages": results}

        return [Tool(get_links, name="get_links"), Tool(read_pages, name="read_pages")]

    def request_limit(self):
        # a turn for each read_pages call that reading every page takes
        return FLEET.specialist_max_requests + math.ceil(len(self.pages) / FLEET.pages_read_per_call)

    def refused(self, entry):
        if entry.verdict != "unsure" and entry.id not in self.read:
            return "you haven't read its page: read it with read_pages, or answer unsure"
        return None

TOOLKITS = {toolkit.names: toolkit for toolkit in (LinkedPages,)}  # the toolkits with tools of their own

@dataclass(frozen=True)
class Specialist:
    name: str  # the kind of question it answers, as the analyzers name it, like alt-text
    description: str  # what it does, in the orchestrator's list of specialists
    check: str  # links or accessibility: the analyzer that collects what its tools return
    skill: str  # how to judge: put in its prompt where {skill} is
    answer: str  # what it hands in, one of ANSWERS
    tools: tuple[str, ...]  # its tools' names
    toolkit: type  # what gives it those tools: ShownItems, or one of TOOLKITS
    tool_description: str | None  # for a ShownItems tool, what it returns, as the file says
    prompt: str  # its system prompt, with {skill} where the skill goes

    @property
    def submit(self):
        return ANSWERS[self.answer][0]

    @property
    def submission(self):
        return ANSWERS[self.answer][1]

def load_specialist(path):
    # a specialist's file: frontmatter that says what it is, and its system prompt as the body. Its tools are either
    # tool, one that shows it every item and is described there, or tools, the names of a toolkit's own tools
    front, prompt = read_skill(path)
    tool = front.get("tool") or {}
    tools = tuple(front.get("tools") or ())
    if tool and tools:
        raise ValueError(f"{path} has both tool and tools: give only one")
    missing = [key for key in ("name", "description", "check", "skill", "answer") if not front.get(key)]
    if tool:
        missing += [f"tool.{key}" for key in ("name", "description") if not tool.get(key)]
    elif not tools:
        missing.append("tool or tools")
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
    if tools and tools not in TOOLKITS:
        problems.append(f"no toolkit has the tools {', '.join(tools)}; the toolkits have "
                        + "; ".join(", ".join(names) for names in TOOLKITS))
    if answer not in ANSWERS:
        problems.append(f"answer must be one of {', '.join(ANSWERS)}")
    else:
        names = tools or (tool["name"],)
        problems += [f"its prompt doesn't mention {word}" for word in ("{skill}", *names, ANSWERS[answer][0])
                     if word not in prompt]
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return Specialist(name, front["description"], check, skill, answer, tools or (tool["name"],),
                      TOOLKITS[tools] if tools else ShownItems, tool.get("description"), prompt)

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
    # own, so one the browser lands on isn't judged twice. The soft-404 specialist reads the linked pages itself
    return Namespace(url=url, analyzer=CHECKS[check].name, backend="ollama", model=model, no_think=no_think,
                     max_output_tokens=max_output_tokens, output=None, checked_elsewhere=set(site_pages),
                     read_linked_pages=False)

class PageWork:
    # one check's work on one page, shared by that check's specialists and static check: the analyzer collects the
    # page once, and the questions keep every element and answer for the page's report. memory, shared across pages,
    # gives an element judged on an earlier page the answer it got there
    def __init__(self, check, args, memory=None):
        self.analyzer = CHECKS[check](args)
        self.questions = Questions(None, self.analyzer.instructions, memory, args.url)
        self.collected = None  # True once collected, False if there was nothing to judge
        self.collect_s = 0.0  # the time collecting took

    async def collect(self):
        if self.collected is None:
            start = time.perf_counter()
            self.collected = await asyncio.to_thread(self.analyzer.collect)
            self.collect_s = time.perf_counter() - start
        return self.collected

@dataclass
class Run:
    # one specialist's run on one page
    specialist: str
    url: str
    status: str = "checked"  # checked, nothing to check, nothing new, lands on another page, partly answered or failed
    lands_on: str | None = None
    error: str | None = None
    expected: list[str] = field(default_factory=list)  # the ids its tools showed it
    known: int = 0  # elements left out of what its tools showed, since they were judged on other pages
    answers: dict = field(default_factory=dict)  # by id
    first_missing: list[str] | None = None  # the ids its first submission had no accepted answer for
    refused: dict = field(default_factory=dict)  # by id: why its latest answer wasn't accepted, until one is
    refusals: int = 0  # answers not accepted, like a pass on a page it hadn't read
    invented: int = 0  # answers for ids its tools never showed
    tool_calls: Counter = field(default_factory=Counter)  # by tool
    read: list[str] = field(default_factory=list)  # the pages it chose to read, for a toolkit that reads pages
    usage: Counter = field(default_factory=Counter)
    collect_s: float = 0.0  # the time the page's tools took, if this run was the first on it

    def record(self):
        return {
            "specialist": self.specialist, "url": self.url, "status": self.status, "lands_on": self.lands_on,
            "error": self.error, "items": len(self.expected), "known": self.known,
            "answered": sum(element_id in self.answers for element_id in self.expected),
            "unanswered": [element_id for element_id in self.expected if element_id not in self.answers],
            "omitted_first": len(self.first_missing or []), "refusals": self.refusals, "invented": self.invented,
            "tool_calls": dict(self.tool_calls), "pages_read": self.read,
            **{key: self.usage[key] for key in USAGE},
            "model_s": round(self.usage["model_s"], 1), "collect_s": round(self.collect_s, 1),
        }

def system_prompt(specialist):
    return specialist.prompt.replace("{skill}", skill_body(SKILLS[specialist.skill]))

async def run_specialist(specialist, work):
    # the specialist on one page: its tools show it the elements not judged before, and it hands in an answer for each.
    # Run again on a page it left items unanswered on, it is only given those
    run = Run(specialist.name, work.analyzer.args.url)
    first = work.collected is None
    collected = await work.collect()
    run.collect_s = work.collect_s if first else 0.0
    if not collected:
        lands_on = getattr(work.analyzer, "lands_on", None)
        if lands_on:
            run.status, run.lands_on = "lands on another page", lands_on
        else:
            run.status, run.error = "failed", "the tools couldn't read the page"
        return run

    toolkit = specialist.toolkit(specialist, work, run)
    total, run.expected = toolkit.prepare()
    run.known = total - len(run.expected)
    if not run.expected:
        # nothing for the model to do: no such elements on the page, or all judged on earlier pages
        run.status = "nothing new" if total else "nothing to check"
        work.questions.record()
        return run

    def review(submitted):
        # keeps every accepted answer, from this submission and the earlier ones, and returns the ids still without one
        if not run.tool_calls[toolkit.first]:
            return ["no tool call"]
        for entry in submitted.entries():
            if entry.id not in run.expected:
                run.invented += 1
            elif entry.id not in run.answers:
                why = entry.problem() or toolkit.refused(entry)
                if why:
                    run.refused[entry.id] = why
                    run.refusals += 1
                else:
                    run.answers[entry.id] = entry.answer()
                    run.refused.pop(entry.id, None)
        missing = [element_id for element_id in run.expected if element_id not in run.answers]
        if run.first_missing is None:
            run.first_missing = missing
        return missing

    def reject(missing):
        if not run.tool_calls[toolkit.first]:
            return (f"Not accepted: you haven't called {toolkit.first} yet. Call it with {run.url}, judge every item "
                    f"it returns, then call {specialist.submit} again.")
        unanswered = [element_id for element_id in missing if element_id not in run.refused]
        refused = [f"- {element_id}: {run.refused[element_id]}" for element_id in missing if element_id in run.refused]
        parts = ["Not accepted yet."]
        if unanswered:
            parts.append(f"These ids have no answer: {', '.join(unanswered)}.")
        if refused:
            parts.append("These answers weren't accepted:\n" + "\n".join(refused) + "\n")
        parts.append(f"The answers you gave for the other ids are kept. Call {specialist.submit} again, with answers "
                     "for only these ids.")
        return " ".join(parts)

    submitted, usage = await run_agent_async(
        work.analyzer.args, system_prompt(specialist), f"Check the page {run.url}.", toolkit.tools(),
        specialist.submission, review, reject, request_limit=toolkit.request_limit(), output_name=specialist.submit,
        text_submission=True)
    run.usage = usage
    if submitted is None:
        run.error = "it stopped before it handed in all its answers"
    if not run.answers:
        run.status = "failed"
        run.error = run.error or "none of its answers could be accepted"
    elif len(run.answers) < len(run.expected):
        run.status = "partly answered"  # the orchestrator can run it again for the rest

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
            print(f"{element_id}: unanswered" + (f" ({run.refused[element_id]})" if element_id in run.refused else ""))
        elif isinstance(answer, Verdict):
            rated = f" | {answer.impact}" if getattr(answer, "impact", None) else ""
            print(f"{element_id}: {answer.verdict}{rated} | {answer.reason}")
        else:
            print(f"{element_id}: note | {answer.note}")
    print(json.dumps(run.record(), indent=2))

if __name__ == "__main__":
    main()
