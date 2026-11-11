import argparse
import time
from argparse import Namespace
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from agent import run_agent
from common import CONFIG, docstring_with, read_prompt, report_fields, report_path, trace_to_logfire
from orchestrator import CHECKS, Orchestrator, PageResult, SiteReport, filled, shortened, unique
from report_page import report_page
from specialists import AGENT_OF, AGENTS, USAGE, PageWork, page_args, run_task, task_status
from tools.axe import axe_selector
from tools.links import check_url, remember_results
from workflow import Questions, page_report

FLEET = CONFIG.fleet
ACCESSIBILITY = CONFIG.accessibility
RAN = ("checked", "nothing to check", "nothing new", "lands on another page")  # a run that needn't be repeated
LINK_PROBLEMS = ("broken", "unreachable", "blocked")  # what the link checker reports a link as that is a problem
DEFAULT_REQUEST = "Check the links and the accessibility of the site."
# a link one skill failed isn't reported again by a later one, as when they ran one after the other on a page
LINK_SKILL_ORDER = ("soft-404", "redirect", "text-mismatch")

@dataclass(frozen=True)
class StaticCheck:
    description: str  # what it does, in the orchestrator's list of static checks
    check: str  # links or accessibility: the analyzer that runs it
    source: str  # the source its findings name

STATIC_CHECKS = {
    "link-checker": StaticCheck("requests every link, image, stylesheet and script on the page, and reports the ones "
                                "that are broken, unreachable or blocked.", "links", "checker"),
    "axe": StaticCheck("runs axe-core, an automated accessibility checker, on the page, and reports the rules the page "
                       "violates, and how many elements axe-core couldn't decide, which the axe-review agent judges.",
                       "accessibility", "axe"),
}
UNITS = {**{name: static.check for name, static in STATIC_CHECKS.items()},
         **{skill: AGENT_OF[skill].skills[skill].category for skill in AGENT_OF}}
NAME_CLASHES = (set(STATIC_CHECKS) & (set(AGENTS) | set(AGENT_OF))) | (set(AGENTS) & set(AGENT_OF))
if NAME_CLASHES:
    raise ValueError(f"static checks, agents and skills each need a name of their own: {', '.join(NAME_CLASHES)}")

class TaskRequest(BaseModel):
    agent: str = Field(description="The agent's name, exactly as listed, like link-agent.")
    page: str = Field(description="The id of the page to check, like p3, as crawl_site listed it.")
    skills: list[str] = Field([], description="The skills to load, as listed for the agent. Leave it empty to load "
                                              "all of them.")

class LeftOut(BaseModel):
    name: str = Field(description='The static check, agent or skill you didn\'t run, exactly as listed, or "all" for '
                                  'every one.')
    pages: list[str] = Field(description='The ids of the pages you didn\'t run it on, like p3, or ["all"] for every '
                                         'page with status 200 and HTML.')
    reason: str = Field(description="Why you didn't run it.")

class SubmitReport(BaseModel):
    narrative: str = Field(
        description="A 3-6 sentence summary for the site's owners of what the checks found, the worst problems "
                    "first, and of what wasn't checked and why. Only mention what the tools showed you.")
    left_out: list[LeftOut] = Field(
        [], description="Every static check, agent or skill, and page with status 200 and HTML that you didn't run it "
                        "on, with why. Empty if there are none.")

class FleetReport(SiteReport):
    mode: str = "fleet"
    request: str  # what the user asked for
    left_out: list[LeftOut]  # as the orchestrator submitted it
    agents: dict[str, list[str]]  # each agent's skills
    static_check_coverage: dict[str, dict[str, int]]  # per static check: its pages by how the run ended
    agent_coverage: dict[str, dict[str, dict[str, int]]]  # per agent and skill: its pages by how it ended, its items
    static_check_runs: list[dict]  # every run of a static check on a page, in order
    agent_runs: list[dict]  # every agent run on a page, in order: a task, or a part of one

class Fleet(Orchestrator):
    def __init__(self, args):
        super().__init__(args)
        self.work = {}  # by page id: the page's evidence, items and answers
        self.status = {}  # by (static check or skill, page id): how its latest run on that page ended
        self.runs = []  # every agent run's record, in order
        self.static_runs = []  # every static check run's record, in order
        self.reported = set()  # the problem links run_static_check has shown the orchestrator
        self.reported_axe = set()  # the elements in axe-core's violations it has shown the orchestrator
        self.tasks = 0

    def make_judges(self):
        return {}  # the agents replace the workflow's judges

    def worker_args(self, check, url, site_pages=()):
        return page_args(check, url, self.args.worker_model, self.args.no_think, self.args.max_output_tokens,
                         site_pages)

    def page_work(self, page_id):
        if page_id not in self.work:
            url = self.pages[page_id]["url"]
            site_pages = [self.pages[other]["url"] for other in self.to_analyze()]
            self.work[page_id] = PageWork(lambda check: self.worker_args(check, url, site_pages), self.memory)
        return self.work[page_id]

    def page_problem(self, page_id):
        # why a page can't be checked, if it can't
        if page_id not in self.pages:
            return "there is no page with this id"
        if page_id not in self.to_analyze():
            return "only pages with status 200 and HTML can be checked"
        return None

    # Static checks

    def links_for_model(self, page_id, analyzer):
        # how the page's links ended, and the problem links that earlier pages didn't already show
        links = analyzer.links
        new = [link for link in links if link["check"]["verdict"] in LINK_PROBLEMS and link["url"] not in self.reported]
        self.reported.update(link["url"] for link in new)
        return filled({
            "id": page_id, "status": "checked",
            "links": dict(Counter(link["check"]["verdict"] for link in links)),
            "problems": [[link["url"], link["check"]["verdict"],
                          link["check"]["status"] or shortened(link["check"]["error"], FLEET.max_reason_chars)]
                         for link in new],
            "problems_shown_before": sum(link["check"]["verdict"] in LINK_PROBLEMS for link in links) - len(new) or None,
        })

    def axe_for_model(self, page_id, analyzer):
        axe, new, before = analyzer.axe, {}, 0
        for rule in axe["violations"]:
            for node in rule["nodes"]:
                key = (rule["id"], axe_selector(node), node["html"][:ACCESSIBILITY.max_html_chars])
                if key in self.reported_axe:
                    before += 1
                    continue
                self.reported_axe.add(key)
                new.setdefault(rule["id"], [rule["id"], rule["impact"], 0])[2] += 1
        return filled({"id": page_id, "status": "checked", "violations": list(new.values()),
                       "elements_shown_before": before or None,
                       "needs_review": sum(len(rule["nodes"]) for rule in axe["incomplete"])})

    @docstring_with(max_pages=FLEET.pages_per_call)
    async def run_static_check_tool(self, check: str, pages: list[str]):
        """Runs a static check, a tool with no model, on pages, one by one. Up to {max_pages} pages per call, and
        only pages with status 200 and HTML. Its results are facts, and go into the report as they are.

        For each page, returns how the run ended: checked; lands on another page of the site, which is checked on its
        own; or failed, with the error. For a page it checked, the link-checker returns how many of the page's links
        ended each way, like ok, redirect or broken, and the broken, unreachable or blocked links that earlier pages
        didn't already show, each as [URL, how it ended, its status or error]. axe returns the rules the page
        violates, each as [rule, impact, how many of its elements earlier pages didn't already show], and how many
        elements axe-core couldn't decide (needs_review). A page that was already checked isn't checked again.

        Args:
            check: The static check's name, exactly as listed.
            pages: The ids of the pages to check, like p3, as crawl_site listed them.
        """
        if self.crawl is None:
            return {"error": "no pages yet: call crawl_site first"}
        if check not in STATIC_CHECKS:
            if check in AGENTS or check in AGENT_OF:
                return {"error": f"{check} isn't a static check: send an agent its tasks with delegate"}
            return {"error": f"there is no static check named {check!r}; they are {', '.join(STATIC_CHECKS)}"}
        if len(pages) > FLEET.pages_per_call:
            return {"error": f"send at most {FLEET.pages_per_call} pages per call"}
        found = STATIC_CHECKS[check]
        results, problems = [], {}
        for page_id in unique(pages):
            problem = self.page_problem(page_id)
            if problem is None and self.status.get((check, page_id)) in RAN:
                problem = f"already checked: {self.status[(check, page_id)]}"
            if problem:
                problems[page_id] = problem
                continue
            print(f"\n=====Static check {check} on {page_id}=====")
            work = self.page_work(page_id)
            first = work.collected[found.check] is None
            analyzer = work.analyzers[found.check]
            collected = await work.collect(found.check)
            if found.check == "accessibility" and work.lands_on:
                self.lands_on[page_id] = work.lands_on
                status = "lands on another page"
                result = {"id": page_id, "status": status,
                          "lands_on": self.page_ids.get(work.lands_on, work.lands_on)}
            elif collected and (found.check == "links" or analyzer.axe is not None):
                status = "checked"
                result = (self.links_for_model(page_id, analyzer) if found.check == "links"
                          else self.axe_for_model(page_id, analyzer))
            else:
                status = "failed"
                result = {"id": page_id, "status": status, "error": "the tools couldn't read the page"}
            self.status[(check, page_id)] = status
            results.append(result)
            self.static_runs.append({"check": check, "page": page_id, "status": status,
                                     "collect_s": round(work.collect_s[found.check], 1) if first else 0.0})
        self.rounds.append({"static_check": check, "pages": [result["id"] for result in results],
                            "problems": sum(len(result.get("problems", result.get("violations", [])))
                                            for result in results)})
        return filled({"pages": results, "problems": problems})

    # Agents

    def task_for_model(self, task_id, request, page_id, skills, runs, status):
        entry = {"task": task_id, "agent": request.agent, "page": page_id, "skills": skills, "status": status,
                 "error": next((run.error for run in runs if run.error), None)}
        lands_on = next((run.lands_on for run in runs if run.lands_on), None)
        if lands_on:
            entry["lands_on"] = self.page_ids.get(lands_on, lands_on)
        answers = [(pair, answer) for run in runs for pair, answer in run.answers.items()]
        if any(run.expected for run in runs):
            entry.update(
                judged=sum(len(run.expected) for run in runs),
                verdicts=dict(Counter(answer.verdict for _, answer in answers)),
                problems=[[item_id, skill, answer.verdict, shortened(answer.reason, FLEET.max_reason_chars),
                           *([answer.impact] if getattr(answer, "impact", None) else [])]
                          for (item_id, skill), answer in answers if answer.verdict != "pass"],
                unanswered=[f"{item_id} {skill}" for run in runs for item_id, skill in run.unanswered()],
            )
        if len(runs) > 1:
            entry["parts"] = len(runs)
        return filled(entry)

    async def run_one(self, request):
        # one task: an agent on a page, with the skills asked for that haven't run there yet
        agent = AGENTS.get(request.agent)
        asked = {"agent": request.agent, "page": request.page}
        if agent is None:
            if request.agent in STATIC_CHECKS:
                return {**asked, "error": f"{request.agent} is a static check: run it with run_static_check"}
            owner = AGENT_OF.get(request.agent)
            if owner:
                return {**asked, "error": f"{request.agent} is a skill of {owner.name}: send that agent the task"}
            return {**asked, "error": f"there is no agent named {request.agent!r}; they are {', '.join(AGENTS)}"}
        unknown = [skill for skill in request.skills if skill not in agent.skills]
        if unknown:
            return {**asked, "error": f"{agent.name} has no skill {', '.join(unknown)}; its skills are "
                                      f"{', '.join(agent.skills)}"}
        problem = self.page_problem(request.page)
        if problem:
            return {**asked, "error": problem}
        page_id = request.page
        skills = [skill for skill in unique(request.skills) or list(agent.skills)
                  if self.status.get((skill, page_id)) not in RAN]
        if not skills:
            return {**asked, "status": "already checked"}
        self.tasks += 1
        task_id = f"t{self.tasks}"
        print(f"\n=====Task {task_id}: {agent.name} on {page_id}, with {', '.join(skills)}=====")
        runs = await run_task(agent, self.page_work(page_id), skills, task_id)
        status = task_status(runs)
        for skill in skills:
            self.status[(skill, page_id)] = status
        self.runs += [{"page": page_id, **run.record()} for run in runs]
        lands_on = next((run.lands_on for run in runs if run.lands_on), None)
        if lands_on:
            self.lands_on[page_id] = lands_on
        return self.task_for_model(task_id, request, page_id, skills, runs, status)

    @docstring_with(max_tasks=FLEET.tasks_per_call)
    async def delegate_tool(self, tasks: list[TaskRequest]):
        """Sends tasks to agents, small models that each own a few related skills and choose which of their tools to
        call. A task sends one agent to one page, with all of its skills or the ones you name. Up to {max_tasks}
        tasks per call, run one by one, and only on pages with status 200 and HTML. Their findings are final.

        For each task, returns its id and how it ended: checked; nothing to check, when the page has nothing its skills
        check; nothing new, when all of it was judged on earlier pages; lands on another page of the site, which is
        checked on its own; partly answered, when it left some items unanswered; or failed, with the error. For a page
        it checked, returns how many items it judged, its verdicts by kind, the items it failed or was unsure about,
        each as [id, skill, verdict, its reason], with the impact it gave a fail, and the items and skills it left
        unanswered. A page with many links or images is checked in parts, each a run of its own. A skill that already
        ran on a page isn't run there again, unless its task failed or was partly answered: then it is only asked
        about what is still unanswered.

        Args:
            tasks: The tasks to run.
        """
        if self.crawl is None:
            return {"error": "no pages yet: call crawl_site first"}
        if len(tasks) > FLEET.tasks_per_call:
            return {"error": f"send at most {FLEET.tasks_per_call} tasks per call"}
        results = [await self.run_one(request) for request in tasks]
        self.rounds.append({"tasks": [filled({"task": result.get("task"), "agent": result["agent"],
                                              "page": result["page"]}) for result in results],
                            "problems": sum(len(result.get("problems", [])) for result in results)})
        return {"tasks": results}

    def tools(self):
        return [Tool(self.crawl_site_tool, name="crawl_site"),
                Tool(self.run_static_check_tool, name="run_static_check"), Tool(self.delegate_tool, name="delegate")]

    # What ran where

    def names_to_units(self, name):
        # a static check, an agent's skills, or one skill, by the name left_out gives
        if name == "all":
            return list(UNITS)
        if name in AGENTS:
            return list(AGENTS[name].skills)
        return [name] if name in UNITS else []

    def left_out_pairs(self, report):
        # by (static check or skill, page id): why the orchestrator didn't run it there
        pairs = {}
        for entry in report.left_out:
            pages = self.to_analyze() if "all" in entry.pages else entry.pages
            for unit in self.names_to_units(entry.name):
                for page_id in pages:
                    pairs[(unit, page_id)] = entry.reason
        return pairs

    def accounted_for(self, unit, page_id, left_out):
        return ((unit, page_id) in self.status or (unit, page_id) in left_out
                or (UNITS[unit] == "accessibility" and page_id in self.lands_on))

    def missing(self, report):
        if self.crawl is None:
            return [{"name": "crawl_site", "pages": []}]
        left_out = self.left_out_pairs(report)
        missing = []
        for name in STATIC_CHECKS:
            pages = [page_id for page_id in self.to_analyze() if not self.accounted_for(name, page_id, left_out)]
            if pages:
                missing.append({"name": name, "pages": pages})
        for agent in AGENTS.values():
            by_name = {}
            for page_id in self.to_analyze():
                skills = [skill for skill in agent.skills if not self.accounted_for(skill, page_id, left_out)]
                if skills:
                    name = agent.name if len(skills) == len(agent.skills) else f"{agent.name} ({', '.join(skills)})"
                    by_name.setdefault(name, []).append(page_id)
            missing += [{"name": name, "pages": pages} for name, pages in by_name.items()]
        return missing

    def reject(self, missing):
        if missing[0]["name"] == "crawl_site":
            return "Report not accepted: call crawl_site first, and check the pages the request asks for."
        listed = missing[:FLEET.max_missing_listed]
        lines = [f"- {entry['name']}: {', '.join(entry['pages'])}" for entry in listed]
        return ("Report not accepted. These static checks and agents haven't run on these pages, and they aren't in "
                "left_out:\n" + "\n".join(lines)
                + "\nRun them with run_static_check or delegate, or put them in left_out with why. Then call "
                  "submit_report again.")

    # Report

    def page_result(self, page_id, page, left_out):
        # per check, whether all, some or none of its static checks and skills ran on the page, and why not
        checks, why = {}, {}
        for check in CHECKS:
            if check == "accessibility" and page_id in self.lands_on:
                checks[check], why[check] = "lands on another page", self.lands_on[page_id]
                continue
            names = [unit for unit, unit_check in UNITS.items() if unit_check == check]
            ran = [name for name in names if self.status.get((name, page_id)) in RAN]
            partly = [name for name in names if self.status.get((name, page_id)) == "partly answered"]
            failed = [name for name in names if self.status.get((name, page_id)) == "failed"]
            reasons = {}
            for name in names:
                if name not in ran and (name, page_id) in left_out:
                    reasons.setdefault(left_out[(name, page_id)], []).append(name)
            if len(ran) == len(names):
                checks[check] = "analyzed"
            elif ran or partly:
                checks[check] = "partly analyzed"
            elif failed:
                checks[check] = "failed"
            elif reasons:
                checks[check] = "left out"
            else:
                checks[check] = "not analyzed"
            notes = [f"{', '.join(partly)} left some items unanswered"] if partly else []
            notes += [f"{', '.join(failed)} failed"] if failed else []
            notes += [reason if len(left) == len(names) else f"{', '.join(left)} left out: {reason}"
                      for reason, left in reasons.items()]
            if notes:
                why[check] = "; ".join(notes)
        return PageResult(id=page_id, url=page["url"], status=page["status"], content_type=page["content_type"],
                          checks=checks, why=why)

    def page_reports(self, started_at, start):
        # each page's static findings, as the workflow reports them: the link checker's, and axe-core's violations. They
        # only count on the pages the orchestrator ran their static check on: elsewhere, the tools only ran for an agent
        for page_id, work in self.work.items():
            for check, analyzer in work.analyzers.items():
                if work.collected[check]:
                    report = page_report(analyzer, Questions(None, None), Counter(), 0, Counter(), started_at, start)
                    not_run = {static.source for name, static in STATIC_CHECKS.items()
                               if static.check == check and self.status.get((name, page_id)) != "checked"}
                    report.findings = [finding for finding in report.findings if finding.source not in not_run]
                    self.reports[check][page_id] = report
                elif work.collected[check] is False and not (check == "accessibility" and work.lands_on):
                    self.errors[check][page_id] = "the tools couldn't read the page"

    def item_findings(self):
        # a link that soft-404 failed isn't also reported as a redirect or a text mismatch, nor one that redirect failed
        # as a text mismatch. Every verdict stays in the items
        findings = super().item_findings()
        skill_of = {item["id"]: item["criterion"] for item in self.memory.items.values()}
        failed = {skill: {finding.url for finding in findings
                          if skill_of.get(finding.item) == skill and finding.verdict == "fail"}
                  for skill in LINK_SKILL_ORDER}

        def reported_before(finding):
            skill = skill_of.get(finding.item)
            if skill not in LINK_SKILL_ORDER[1:]:
                return False
            if check_url(finding.url)["final_url"] in failed["soft-404"]:
                return True
            return skill == "text-mismatch" and finding.url in failed["redirect"]
        return [finding for finding in findings if not reported_before(finding)]

    def site_report(self, submitted, lead, started_at, start, usage):
        self.page_reports(started_at, start)
        left_out = self.left_out_pairs(submitted) if submitted else {}
        items = list(self.memory.items.values())
        findings = self.fact_findings() + self.item_findings()
        totals, by_worker = Counter(), {}
        for run in self.runs:
            used = {key: run[key] for key in (*USAGE, "model_s", "collect_s")}
            totals.update(used)
            by_worker.setdefault(run["agent"], Counter()).update(used)
        for run in self.static_runs:
            totals["collect_s"] += run["collect_s"]
            by_worker.setdefault(run["check"], Counter())["collect_s"] += run["collect_s"]
        coverage = {
            "pages": {"found": len(self.pages), "to_analyze": len(self.to_analyze())},
            "items": {"elements_judged": sum(len(item["elements"]) for item in items)},  # across pages
        }

        def pages_by_status(name, statuses):
            counts = {f"pages_{status.replace(' ', '_')}": sum(self.status.get((name, page_id)) == status
                                                                for page_id in self.to_analyze())
                      for status in statuses}
            counts["pages_left_out"] = sum((name, page_id) in left_out and (name, page_id) not in self.status
                                           for page_id in self.to_analyze())
            return counts

        static_check_coverage = {name: pages_by_status(name, ("checked", "lands on another page", "failed"))
                                 for name in STATIC_CHECKS}
        agent_coverage = {agent.name: {skill: {
            **pages_by_status(skill, (*RAN, "partly answered", "failed")),
            "items": sum(item["criterion"] == skill for item in items),
            "items_unanswered": sum(item["criterion"] == skill and item["answer"] is None for item in items),
        } for skill in agent.skills} for agent in AGENTS.values()}
        return FleetReport(
            **report_fields(lead, started_at, start, usage),
            counts=dict(Counter(finding.severity for finding in findings)),
            narrative=submitted.narrative if submitted else None,
            rejected_submissions=usage["rejected_submissions"],
            unbacked_claims=usage["unbacked_claims"],
            text_replies=usage["text_replies"],
            request=self.args.request,
            left_out=submitted.left_out if submitted else [],
            agents={agent.name: list(agent.skills) for agent in AGENTS.values()},
            worker_model=self.args.worker_model,
            worker_thinking=not self.args.no_think,
            worker_llm_requests=totals["llm_requests"],
            worker_input_tokens=totals["input_tokens"],
            worker_output_tokens=totals["output_tokens"],
            worker_truncated_replies=totals["truncated_replies"],
            worker_invalid_replies=totals["text_replies"],  # replies with no tool call
            worker_model_s=round(totals["model_s"], 1),
            worker_collect_s=round(totals["collect_s"], 1),
            worker_by_check={name: {key: round(value, 1) for key, value in used.items()}
                             for name, used in by_worker.items()},
            tools_s=round(usage["tools_s"], 1),
            crawl_s=self.crawl_s,
            crawl_started_at=self.crawl["started_at"] if self.crawl else None,
            crawl_reused=self.crawl_reused,
            coverage=coverage,
            static_check_coverage=static_check_coverage,
            agent_coverage=agent_coverage,
            decisions={},
            rounds=self.rounds,
            refused_decisions=[],
            pages=[self.page_result(page_id, page, left_out) for page_id, page in self.pages.items()],
            findings=findings,
            items=self.item_results(),
            static_check_runs=self.static_runs,
            agent_runs=self.runs,
        )

def parse_args():
    parser = argparse.ArgumentParser(description="Check a website with a fleet: a Gemini model reads your request, "
                                                 "runs static checks on the site's pages and sends tasks to agents, "
                                                 "small local models that each own a few related skills, and whose "
                                                 "findings are final.")
    parser.add_argument("url", help="the site's start page")
    parser.add_argument("--request", default=DEFAULT_REQUEST,
                        help=f'what to check, in your own words (default "{DEFAULT_REQUEST}")')
    parser.add_argument("--model", help=f"the orchestrator's Gemini model (default {CONFIG.models.gemini})")
    parser.add_argument("--worker-model", default=CONFIG.models.ollama,
                        help=f"the agents' Ollama model (default {CONFIG.models.ollama})")
    parser.add_argument("--no-think", action="store_true",
                        help="the agents answer without thinking first, which is much faster on a CPU")
    parser.add_argument("--max-output-tokens", type=int, default=CONFIG.ollama.max_output_tokens,
                        help="the most tokens an agent may write per turn, thinking included "
                             f"(default {CONFIG.ollama.max_output_tokens})")
    parser.add_argument("--recrawl", action="store_true",
                        help="crawl the site again, even if a recent crawl of it was saved")
    parser.add_argument("--max-pages", type=int, default=CONFIG.crawl.max_pages,
                        help=f"the most pages the crawl requests, nearest the start page first "
                             f"(default {CONFIG.crawl.max_pages})")
    args = parser.parse_args()
    if urlsplit(args.url).scheme not in ("http", "https") or not urlsplit(args.url).hostname:
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    return args

def coordinator_prompt():
    # the orchestrator's prompt, with the static checks and agents it can use listed in it
    static_checks = "\n".join(f"- {name} ({static.check}): {static.description}"
                              for name, static in STATIC_CHECKS.items())
    roster = "\n".join(f"- {agent.name}: {agent.description} Its skills:\n"
                       + "\n".join(f"  - {skill.name} ({skill.category}): {skill.description}"
                                   for skill in agent.skills.values())
                       for agent in AGENTS.values())
    return read_prompt(FLEET.folder / "coordinator.md", static_checks=static_checks, roster=roster)

def main():
    trace_to_logfire()
    args = parse_args()
    remember_results()  # each URL is checked once, however many pages link to it
    fleet = Fleet(args)
    lead = Namespace(url=args.url, backend="gemini", model=args.model, no_think=False,
                     max_output_tokens=CONFIG.ollama.max_output_tokens)
    system_prompt = coordinator_prompt()

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    submitted, usage = run_agent(lead, system_prompt, f"{args.request}\n\nThe site's start page: {args.url}",
                                 fleet.tools(), SubmitReport, fleet.missing, fleet.reject,
                                 request_limit=FLEET.max_requests)

    report = fleet.site_report(submitted, lead, started_at, start, usage)
    path = report_path("fleet", lead)
    path.write_text(report.model_dump_json(indent=2))
    page = path.with_suffix(".html")
    page.write_text(report_page(report.model_dump(mode="json")))
    print(f"=====Report saved to {path}, and as a page to {page}=====")

if __name__ == "__main__":
    main()
