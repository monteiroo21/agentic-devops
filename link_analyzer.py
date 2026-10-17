import time
from collections import Counter
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from pydantic_ai import Tool
from common import (CONFIG, AgentReport, list_skills, make_parser, parse_args, report_fields, report_path, run_agent,
                    skills_prompt, split_backed)
from tools.links import check_page_links, check_url, check_urls, fetch_page_text, for_model

LINKS = CONFIG.links

class Assessment(BaseModel):
    url: str = Field(description="The link this comment is about.")
    likely_intentional: bool = Field(
        description="True if the problem looks deliberate, e.g. a test page that demonstrates a 404.")
    note: str = Field(description="One or two sentences explaining the problem.")
    suggested_fix: str | None = Field(
        None, description="How to fix it, if you can tell. Leave this out if no fix is needed.")

SKILLS = list_skills(LINKS.skills)
FINDING_RULES = [rule for skill in SKILLS.values() for rule in skill.rules()] + ["other"]
AgentRule = Literal[tuple(FINDING_RULES)]
NEEDS_PAGE_TEXT = {rule for skill in SKILLS.values() if skill.flag("needs-page-text")
                   for rule in skill.rules()} | {"other"}

class AgentFinding(BaseModel):
    url: str = Field(description="The link with the problem, exactly as the tools returned it.")
    rule: AgentRule = Field(description="The kind of problem.")  # pyright: ignore[reportInvalidTypeForm]
    evidence: str = Field(
        description="What in the tool results shows the problem, e.g. the page title or the final URL.")
    note: str = Field(description="One or two sentences explaining the problem.")

class SubmitReport(BaseModel):
    """Hand in the final report. Call this once, after you have finished investigating."""
    narrative: str = Field(
        description="A 2-4 sentence summary of the page's link health. Only mention things the tools checked.")
    assessments: list[Assessment] = Field(
        description="One entry per broken, blocked or unreachable link. Empty if there are none.")
    agent_findings: list[AgentFinding] = Field(
        [], description="Problems you found that the status code does not show, e.g. a page that returns 200 but is "
                        "an error page, a redirect to the homepage or a login page, or link text that does not match "
                        "the page it leads to. Empty if there are none.")

class RejectedClaim(AgentFinding):
    reason: Literal["not-read", "not-homepage-redirect", "no-tool-result"]
    times_rejected: int
    read_afterwards: bool | None  # None when reading the page wasn't what was missing
    outcome: Literal["kept", "relabelled", "dropped"]


class Finding(BaseModel):
    rule: str
    severity: str  # error: broken or unreachable, warning: a problem the model found, info: blocked or redirected, needs-review: undecided
    page: str | None
    url: str
    tag: str | None
    text: str | None
    status: int | None
    error: str | None
    source: str  # checker, agent or model
    verdict: str | None = None  # the workflow model's decision on a link
    evidence: str | None = None
    note: str | None = None
    likely_intentional: bool | None = None
    suggested_fix: str | None = None

class Report(AgentReport):
    link_checks: dict[str, int]  # links per check verdict: ok, redirect, broken, blocked, unreachable
    links_found: int  # unique links found on the page
    links_checked: int  # how many of those were checked; should equal links_found
    findings: list[Finding]
    unverified: list[dict] = []  # submitted items no tool result backs, kept after the last rejection
    rejected_claims: list[RejectedClaim] = []  # each distinct claim that was sent back, and its outcome

def process_tool(tool_name, tool_input):
    if tool_name == "check_url":
        return check_url(tool_input["url"])
    elif tool_name == "check_page_links":
        return check_page_links(tool_input["url"])
    elif tool_name == "check_urls":
        return check_urls(tool_input["urls"])
    elif tool_name == "fetch_page_text":
        return fetch_page_text(tool_input["url"])
    else:
        raise ValueError(f"Unknown tool: {tool_name}")

def record_result(tool_name, tool_result, link_info, checks, fetched=None):
    if tool_name == "extract_links":
        for link in tool_result["links"]:
            link_info.setdefault(link["url"], {"page": tool_result["final_url"], "tag": link["tag"], "text": link["text"]})
    elif tool_name == "check_url":
        checks[tool_result["url"]] = tool_result
    elif tool_name == "check_urls":
        for result in tool_result:
            checks[result["url"]] = result
    elif tool_name == "check_page_links":
        record_result("extract_links", tool_result, link_info, checks)
        record_result("check_urls", [link["check"] for link in tool_result["links"]], link_info, checks)
    elif tool_name == "fetch_page_text" and fetched is not None:
        fetched[tool_result["url"]] = tool_result

def build_findings(checks, link_info, notes):
    # notes are the model's comments on the broken links, by URL: the agent's Assessments or the workflow's LinkNotes
    findings = []
    for url, check in checks.items():
        verdict = check["verdict"]
        if verdict == "ok":
            continue
        info = link_info.get(url, {})
        tag = info.get("tag")
        if verdict == "broken":
            rule = "broken-link" if tag in ("a", None) else "broken-resource"
            severity = "error"
        elif verdict == "unreachable":
            rule, severity = "unreachable", "error"
        else:  # "blocked" or "redirect"
            rule, severity = verdict, "info"
        note = notes.get(url)
        findings.append(Finding(
            rule=rule,
            severity=severity,
            page=info.get("page"),
            url=url,
            tag=tag,
            text=info.get("text"),
            status=check["status"],
            error=check["error"],
            source="checker",
            note=note.note if note else None,
            likely_intentional=note.likely_intentional if note else None,
            suggested_fix=note.suggested_fix if note else None,
        ))
    return findings

def unbacked_reason(agent_finding, checks, fetched):
    if agent_finding.rule in NEEDS_PAGE_TEXT:
        return "not-read"
    if agent_finding.rule == "redirect-to-homepage" and (agent_finding.url in checks or agent_finding.url in fetched):
        return "not-homepage-redirect"
    return "no-tool-result"

def final_url_of(url, checks, fetched):
    return (checks.get(url) or fetched.get(url) or {}).get("final_url")

def ends_on_homepage(url, checks, fetched):
    # the link was redirected and ended on the root of a site
    final_url = final_url_of(url, checks, fetched)
    return final_url is not None and final_url != url and urlparse(final_url).path in ("", "/")

def is_backed(agent_finding, checks, fetched):
    url = agent_finding.url
    if agent_finding.rule in NEEDS_PAGE_TEXT:
        final_url = checks.get(url, {}).get("final_url")
        return url in fetched or final_url in fetched
    if agent_finding.rule == "redirect-to-homepage" and not ends_on_homepage(url, checks, fetched):
        return False
    return url in fetched or url in checks

def assessment_problem(assessment, checks):
    check = checks.get(assessment.url)
    if check is None:
        return "no tool result shows this URL"
    if check["verdict"] == "ok":
        return (f"this link answered with status {check['status']} and no redirect, so the checker reported nothing "
                "for it to assess; if its page has a problem the status code does not show, put it in agent_findings")
    return None

def finding_problem(agent_finding, checks, fetched):
    if is_backed(agent_finding, checks, fetched):
        return None
    reason = unbacked_reason(agent_finding, checks, fetched)
    if reason == "not-read":
        return "you never read that page or the page it redirects to"
    if reason == "not-homepage-redirect":
        final_url = final_url_of(agent_finding.url, checks, fetched)
        return f"the tools show it ends on {final_url}, which is not a redirect to a homepage"
    return "no tool result shows this URL"

def check_report(report, checks, fetched):
    # splits the report into what the tool results back and what they don't
    assessments, unverified_assessments = split_backed(
        report.assessments, "assessment", lambda a: assessment_problem(a, checks))
    agent_findings, unverified_findings = split_backed(
        report.agent_findings, "agent_finding", lambda f: finding_problem(f, checks, fetched))
    return assessments, agent_findings, unverified_assessments + unverified_findings

def unbacked_message(unverified):
    lines = [f"- {item['kind']}" + (f" {item['rule']}" if "rule" in item else "")
             + f" for {item['url']}: {item['reason']}" for item in unverified]
    return ("Report not accepted. These items are not backed by what the tools showed you:\n"
            + "\n".join(lines)
            + "\nCheck them (use fetch_page_text to read a page), give them the rule that fits, or remove them, "
              "then call submit_report again.")

def build_agent_findings(agent_findings, checks, fetched, link_info, findings):
    # Turn the agent's backed findings into report findings
    already_errors = {f.url for f in findings if f.severity == "error"}
    report_findings = []
    for agent_finding in agent_findings:
        url = agent_finding.url
        if url in already_errors:
            continue  # already reported as broken by the checker
        info = link_info.get(url, {})
        result = checks.get(url) or fetched.get(url, {})
        report_findings.append(Finding(
            rule=agent_finding.rule,
            severity="warning",
            page=info.get("page"),
            url=url,
            tag=info.get("tag"),
            text=info.get("text"),
            status=result.get("status"),
            error=result.get("error"),
            source="agent",
            evidence=agent_finding.evidence,
            note=agent_finding.note,
        ))
    return report_findings

def rejection_outcomes(rejections, final_findings, checks, fetched):
    final_rules = {}
    for f in final_findings:
        final_rules.setdefault(f.url, set()).add(f.rule)
    rejected = []
    for (url, rule), entry in rejections.items():
        if rule in final_rules.get(url, set()):
            outcome = "kept"
        elif url in final_rules:
            outcome = "relabelled"
        else:
            outcome = "dropped"
        read_afterwards = None
        if entry["reason"] != "not-homepage-redirect":
            read_afterwards = url in fetched or final_url_of(url, checks, fetched) in fetched
        rejected.append(RejectedClaim(**entry["claim"].model_dump(), reason=entry["reason"],
                                      times_rejected=entry["times_rejected"],
                                      read_afterwards=read_afterwards, outcome=outcome))
    return rejected

def main():
    args = parse_args(make_parser("Find link problems on a web page."))

    rules = ", ".join(FINDING_RULES[:-1])
    read_first = [rule for rule in FINDING_RULES if rule in NEEDS_PAGE_TEXT]
    read_first = ", ".join(read_first[:-1]) + " or " + read_first[-1]
    system_prompt = f"""
        You are a web link analyzer. Your goal is to find every link problem on the page the user gives you,
        including problems that the status code alone does not reveal.

        Start by calling check_page_links on the URL: it finds every link on the page and checks all of them.

        Comment on the links it found broken, unreachable or blocked in assessments, with the broken-links skill
        below. A status of 200 only means the server answered. It does not mean the page shows what the link
        promises: an error page, an empty page or the wrong page can all return 200. So judge every link to a page,
        internal or external, with each of the other skills. Read every page you cannot confirm from its link text,
        URL, title and heading alone with fetch_page_text. You can fetch several pages at once.

        Put the problems you found that the status code does not show in agent_findings, using the most specific
        rule that fits: {rules}, or other for a problem none of them covers. Before reporting a {read_first}
        finding, read that page with fetch_page_text.

        Only report what the tools showed you. You do not check accessibility, so do not comment on it.

        # Skills

    """ + skills_prompt(SKILLS)

    # What the tools found, filled in during the run
    link_info = {}
    checks = {}
    fetched = {}
    rejections = {}  # each distinct claim that was sent back, with why and how often

    def run_tool(tool_name, tool_input):
        tool_result = process_tool(tool_name, tool_input)
        record_result(tool_name, tool_result, link_info, checks, fetched)
        return for_model(tool_name, tool_result)

    def check_url_tool(url: str):
        """Checks whether a single URL works and returns its status code, final URL, redirect chain, content type and
        any connection error, and for an HTML page its title and first heading. Empty fields are left out, and the
        final URL is only given when it differs from the URL.

        Args:
            url: The URL to check.
        """
        return run_tool("check_url", {"url": url})

    def check_page_links_tool(url: str):
        """Finds every link on a web page and checks all of them. Returns each link's text, tag and whether it is
        internal or external, with its status code, final URL, redirect chain, content type and any connection
        error, and for a link to an HTML page that page's title and first heading. Empty fields are left out, and the
        final URL is only given when it differs from the link's URL.

        Args:
            url: The URL of the web page whose links to check.
        """
        return run_tool("check_page_links", {"url": url})

    def check_urls_tool(urls: list[str]):
        """Checks whether multiple URLs work and returns their status code, final URL, redirect chain, content type
        and any connection error, and for HTML pages their title and first heading. Empty fields are left out, and
        the final URL is only given when it differs from the URL.

        Args:
            urls: The URLs to check.
        """
        return run_tool("check_urls", {"urls": urls})

    def fetch_page_text_tool(url: str):
        """Fetches the title, first heading and a short snippet of visible text from a web page. Empty fields are
        left out, and the final URL is only given when the page redirected.

        Args:
            url: The URL of the web page to fetch text from.
        """
        return run_tool("fetch_page_text", {"url": url})

    tools = [Tool(check_url_tool, name="check_url"), Tool(check_page_links_tool, name="check_page_links"),
             Tool(check_urls_tool, name="check_urls"), Tool(fetch_page_text_tool, name="fetch_page_text")]

    def review(report):
        *_, unverified = check_report(report, checks, fetched)
        return unverified

    def reject(unverified):
        for item in unverified:
            if item["kind"] == "agent_finding":
                claim = AgentFinding.model_validate(item)
                entry = rejections.setdefault((claim.url, claim.rule), {
                    "claim": claim, "reason": unbacked_reason(claim, checks, fetched), "times_rejected": 0})
                entry["times_rejected"] += 1
        return unbacked_message(unverified)

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    submitted, usage = run_agent(args, system_prompt, f"Check the links on {args.url}", tools, SubmitReport,
                                 review, reject)

    if submitted is None:
        return

    assessments, agent_findings, unverified = check_report(submitted, checks, fetched)
    findings = build_findings(checks, link_info, {a.url: a for a in assessments})
    findings += build_agent_findings(agent_findings, checks, fetched, link_info, findings)
    report = Report(
        **report_fields(args, started_at, start, usage),
        counts=dict(Counter(f.severity for f in findings)),
        narrative=submitted.narrative,
        rejected_submissions=usage["rejected_submissions"],
        unbacked_claims=usage["unbacked_claims"],
        text_replies=usage["text_replies"],
        link_checks=dict(Counter(check["verdict"] for check in checks.values())),
        links_found=len(link_info),
        links_checked=sum(1 for url in link_info if url in checks),
        findings=findings,
        unverified=unverified,
        rejected_claims=rejection_outcomes(rejections, submitted.agent_findings, checks, fetched),
    )
    path = report_path("links", args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
