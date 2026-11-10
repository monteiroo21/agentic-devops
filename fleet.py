import argparse
import time
from argparse import Namespace
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from agent import run_agent
from common import CONFIG, Verdict, docstring_with, read_prompt, report_fields, report_path, trace_to_logfire
from orchestrator import CHECKS, Orchestrator, PageResult, SiteReport, filled, shortened, unique
from report_page import report_page
from specialists import SPECIALISTS, USAGE, PageWork, page_args, run_specialist
from tools.links import remember_results
from workflow import page_report

# A Gemini model reads the user's request and hands the site's pages to the specialists, small local models that each
# check one thing. It doesn't judge anything itself: the specialists' answers are final

FLEET = CONFIG.fleet
RAN = ("checked", "nothing to check", "nothing new", "lands on another page")  # a run that needn't be repeated
DEFAULT_REQUEST = "Check the links and the accessibility of the site."

class LeftOut(BaseModel):
    specialist: str = Field(description='The specialist you didn\'t run, exactly as listed, or "all" for every one.')
    pages: list[str] = Field(description='The ids of the pages you didn\'t run it on, like p3, or ["all"] for every '
                                         'page with status 200 and HTML.')
    reason: str = Field(description="Why you didn't run it.")

class SubmitReport(BaseModel):
    narrative: str = Field(
        description="A 3-6 sentence summary for the site's owners of what the specialists found, the worst problems "
                    "first, and of what wasn't checked and why. Only mention what the tools showed you.")
    left_out: list[LeftOut] = Field(
        [], description="Every specialist and page with status 200 and HTML that you didn't run it on, with why. Empty "
                        "if there are none.")

class FleetReport(SiteReport):
    mode: str = "fleet"
    request: str  # what the user asked for
    left_out: list[LeftOut]  # as the orchestrator submitted it
    specialist_coverage: dict[str, dict[str, int]]  # per specialist: its pages by how the run ended, and its items
    specialist_runs: list[dict]  # every run of a specialist on a page, in order

class Fleet(Orchestrator):
    def __init__(self, args):
        super().__init__(args)
        self.work = {}  # by (check, page id): the check's evidence, elements and answers on that page
        self.status = {}  # by (specialist, page id): how its latest run on that page ended
        self.runs = []  # every run's record, in order

    def make_judges(self):
        return {}  # the specialists replace the workflow's judges

    def worker_args(self, check, url, site_pages=()):
        return page_args(check, url, self.args.worker_model, self.args.no_think, self.args.max_output_tokens,
                         site_pages)

    def page_work(self, check, page_id):
        if (check, page_id) not in self.work:
            site_pages = [self.pages[other]["url"] for other in self.to_analyze()]
            self.work[(check, page_id)] = PageWork(
                check, self.worker_args(check, self.pages[page_id]["url"], site_pages), self.memory)
        return self.work[(check, page_id)]

    def run_for_model(self, run, page_id):
        entry = {"id": page_id, "status": run.status, "error": run.error}
        if run.lands_on:
            entry["lands_on"] = self.page_ids.get(run.lands_on, run.lands_on)
        if run.expected:
            answers = [run.answers[element_id] for element_id in run.expected if element_id in run.answers]
            verdicts = [answer for answer in answers if isinstance(answer, Verdict)]
            entry.update(
                judged=len(run.expected),
                verdicts=dict(Counter(answer.verdict for answer in verdicts)),
                problems=[[answer.id, answer.verdict, shortened(answer.reason, FLEET.max_reason_chars)]
                          for answer in verdicts if answer.verdict != "pass"],
                notes=[[answer.id, shortened(answer.note, FLEET.max_reason_chars)]
                       for answer in answers if not isinstance(answer, Verdict)],
                unanswered=[element_id for element_id in run.expected if element_id not in run.answers],
            )
        return filled(entry)

    @docstring_with(max_pages=FLEET.pages_per_call)
    async def delegate_tool(self, specialist: str, pages: list[str]):
        """Sends pages to a specialist, a small model that checks one thing, which checks them one by one. Up to
        {max_pages} pages per call, and only pages with status 200 and HTML. Its answers are final.

        For each page, returns how the run ended: checked; nothing to check, when the page has nothing of what it
        checks; nothing new, when all of it was judged on earlier pages; lands on another page of the site, which is
        checked on its own; or failed, with the error. For a page it checked, returns how many items it judged, its
        verdicts by kind, the items it failed or was unsure about, each as [id, verdict, its reason], and its notes on
        problems a tool already confirmed, like a broken link or a rule axe-core found violated, each as [id, note].
        A page that was already checked by this specialist isn't checked again, unless its run failed.

        Args:
            specialist: The specialist's name, exactly as listed.
            pages: The ids of the pages to check, like p3, as crawl_site listed them.
        """
        if self.crawl is None:
            return {"error": "no pages yet: call crawl_site first"}
        found = SPECIALISTS.get(specialist)
        if found is None:
            return {"error": f"there is no specialist named {specialist!r}; they are {', '.join(SPECIALISTS)}"}
        if len(pages) > FLEET.pages_per_call:
            return {"error": f"send at most {FLEET.pages_per_call} pages per call"}
        analyzable = set(self.to_analyze())
        results, problems = [], {}
        for page_id in unique(pages):
            if page_id not in self.pages:
                problems[page_id] = "there is no page with this id"
            elif page_id not in analyzable:
                problems[page_id] = "only pages with status 200 and HTML can be checked"
            elif self.status.get((specialist, page_id)) in RAN:
                problems[page_id] = f"already checked: {self.status[(specialist, page_id)]}"
            else:
                print(f"\n=====Specialist {specialist} on {page_id}=====")
                run = await run_specialist(found, self.page_work(found.check, page_id))
                self.status[(specialist, page_id)] = run.status
                self.runs.append({"page": page_id, **run.record()})
                if run.lands_on:
                    self.lands_on[page_id] = run.lands_on
                results.append(self.run_for_model(run, page_id))
        self.rounds.append({"specialist": specialist, "pages": [result["id"] for result in results],
                            "problems": sum(len(result.get("problems", [])) for result in results)})
        return filled({"pages": results, "problems": problems})

    def tools(self):
        return [Tool(self.crawl_site_tool, name="crawl_site"), Tool(self.delegate_tool, name="delegate")]

    def left_out_pairs(self, report):
        # by (specialist, page id): why the orchestrator didn't run it there
        pairs = {}
        for entry in report.left_out:
            names = list(SPECIALISTS) if entry.specialist == "all" else [entry.specialist]
            pages = self.to_analyze() if "all" in entry.pages else entry.pages
            for name in names:
                for page_id in pages:
                    pairs[(name, page_id)] = entry.reason
        return pairs

    def accounted_for(self, name, page_id, left_out):
        return ((name, page_id) in self.status or (name, page_id) in left_out
                or (SPECIALISTS[name].check == "accessibility" and page_id in self.lands_on))

    def missing(self, report):
        if self.crawl is None:
            return [{"specialist": "crawl_site", "pages": []}]
        left_out = self.left_out_pairs(report)
        missing = []
        for name in SPECIALISTS:
            pages = [page_id for page_id in self.to_analyze() if not self.accounted_for(name, page_id, left_out)]
            if pages:
                missing.append({"specialist": name, "pages": pages})
        return missing

    def reject(self, missing):
        if missing[0]["specialist"] == "crawl_site":
            return "Report not accepted: call crawl_site first, and check the pages the request asks for."
        listed = missing[:FLEET.max_missing_listed]
        lines = [f"- {entry['specialist']}: {', '.join(entry['pages'])}" for entry in listed]
        return ("Report not accepted. These specialists haven't run on these pages, and they aren't in left_out:\n"
                + "\n".join(lines)
                + "\nRun them with delegate, or put them in left_out with why. Then call submit_report again.")

    # Report

    def page_result(self, page_id, page, left_out):
        # per check, whether all, some or none of its specialists ran on the page, and why not
        checks, why = {}, {}
        for check in CHECKS:
            if check == "accessibility" and page_id in self.lands_on:
                checks[check], why[check] = "lands on another page", self.lands_on[page_id]
                continue
            names = [name for name, specialist in SPECIALISTS.items() if specialist.check == check]
            ran = [name for name in names if self.status.get((name, page_id)) in RAN]
            failed = [name for name in names if self.status.get((name, page_id)) == "failed"]
            reasons = {}
            for name in names:
                if name not in ran and (name, page_id) in left_out:
                    reasons.setdefault(left_out[(name, page_id)], []).append(name)
            if len(ran) == len(names):
                checks[check] = "analyzed"
            elif ran:
                checks[check] = "partly analyzed"
            elif failed:
                checks[check] = "failed"
            elif reasons:
                checks[check] = "left out"
            else:
                checks[check] = "not analyzed"
            notes = [f"{', '.join(failed)} failed"] if failed else []
            notes += [reason if len(left) == len(names) else f"{', '.join(left)} left out: {reason}"
                      for reason, left in reasons.items()]
            if notes:
                why[check] = "; ".join(notes)
        return PageResult(id=page_id, url=page["url"], status=page["status"], content_type=page["content_type"],
                          checks=checks, why=why)

    def page_reports(self, started_at, start):
        # each page's findings, as the workflow reports them, for the site's findings
        for (check, page_id), work in self.work.items():
            if work.collected:
                self.reports[check][page_id] = page_report(work.analyzer, work.questions, Counter(), 0, Counter(),
                                                           started_at, start)
            elif not getattr(work.analyzer, "lands_on", None):
                self.errors[check][page_id] = "the tools couldn't read the page"

    def site_report(self, submitted, lead, started_at, start, usage):
        self.page_reports(started_at, start)
        left_out = self.left_out_pairs(submitted) if submitted else {}
        items = list(self.memory.items.values())
        findings = self.fact_findings() + self.item_findings()
        totals, by_specialist = Counter(), {}
        for run in self.runs:
            used = {key: run[key] for key in (*USAGE, "model_s", "collect_s")}
            totals.update(used)
            by_specialist.setdefault(run["specialist"], Counter()).update(used)
        coverage = {
            "pages": {"found": len(self.pages), "to_analyze": len(self.to_analyze())},
            "items": {"elements_judged": sum(len(item["elements"]) for item in items)},  # across pages
        }
        specialist_coverage = {name: {
            **{f"pages_{status.replace(' ', '_')}": sum(self.status.get((name, page_id)) == status
                                                        for page_id in self.to_analyze())
               for status in (*RAN, "failed")},
            "pages_left_out": sum((name, page_id) in left_out and (name, page_id) not in self.status
                                  for page_id in self.to_analyze()),
            "items": sum(item["criterion"] == name for item in items),
            "items_unanswered": sum(item["criterion"] == name and item["answer"] is None for item in items),
        } for name in SPECIALISTS}
        return FleetReport(
            **report_fields(lead, started_at, start, usage),
            counts=dict(Counter(finding.severity for finding in findings)),
            narrative=submitted.narrative if submitted else None,
            rejected_submissions=usage["rejected_submissions"],
            unbacked_claims=usage["unbacked_claims"],
            text_replies=usage["text_replies"],
            request=self.args.request,
            left_out=submitted.left_out if submitted else [],
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
                             for name, used in by_specialist.items()},
            tools_s=round(usage["tools_s"], 1),
            crawl_s=self.crawl_s,
            crawl_started_at=self.crawl["started_at"] if self.crawl else None,
            crawl_reused=self.crawl_reused,
            coverage=coverage,
            specialist_coverage=specialist_coverage,
            decisions={},
            rounds=self.rounds,
            refused_decisions=[],
            pages=[self.page_result(page_id, page, left_out) for page_id, page in self.pages.items()],
            findings=findings,
            items=self.item_results(),
            specialist_runs=self.runs,
        )

def parse_args():
    parser = argparse.ArgumentParser(description="Check a website with a fleet: a Gemini model reads your request and "
                                                 "hands the site's pages to specialists, small local models that each "
                                                 "check one thing, and whose answers are final.")
    parser.add_argument("url", help="the site's start page")
    parser.add_argument("--request", default=DEFAULT_REQUEST,
                        help=f'what to check, in your own words (default "{DEFAULT_REQUEST}")')
    parser.add_argument("--model", help=f"the orchestrator's Gemini model (default {CONFIG.models.gemini})")
    parser.add_argument("--worker-model", default=CONFIG.models.ollama,
                        help=f"the specialists' Ollama model (default {CONFIG.models.ollama})")
    parser.add_argument("--no-think", action="store_true",
                        help="the specialists answer without thinking first, which is much faster on a CPU")
    parser.add_argument("--max-output-tokens", type=int, default=CONFIG.ollama.max_output_tokens,
                        help="the most tokens a specialist may write per turn, thinking included "
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
    # the orchestrator's prompt, with the specialists it can use listed in it
    roster = "\n".join(f"- {name} ({specialist.check}): {specialist.description}"
                       for name, specialist in SPECIALISTS.items())
    return read_prompt(FLEET.agents / "coordinator.md", roster=roster)

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
