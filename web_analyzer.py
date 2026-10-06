import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urldefrag, urlparse

import httpx
import lxml.html
from bs4 import BeautifulSoup

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from dotenv import load_dotenv

load_dotenv()

client = httpx.Client(
    follow_redirects=True,
    timeout=10.0,
    headers={"User-Agent": "Mozilla/5.0 (compatible; web-analyzer/0.1)"},
)

IGNORED_LINK_RELS = {"preconnect", "dns-prefetch"}
MAX_PAGE_BYTES = 512 * 1024  # enough for the title and first heading of almost any page

class Assessment(BaseModel):
    url: str
    likely_intentional: bool
    note: str
    suggested_fix: str | None = None

AgentRule = Literal["soft-404", "redirect-to-homepage", "redirect-to-login", "text-mismatch", "other"]
NEEDS_PAGE_TEXT = {"soft-404", "text-mismatch", "other"}

class AgentFinding(BaseModel):
    url: str
    rule: AgentRule
    evidence: str
    note: str

class SubmitReport(BaseModel):
    narrative: str
    assessments: list[Assessment]
    agent_findings: list[AgentFinding] = []

class RejectedClaim(AgentFinding):
    reason: Literal["not-read", "not-homepage-redirect", "no-tool-result"]
    times_rejected: int
    read_afterwards: bool | None  # None when reading the page wasn't what was missing
    outcome: Literal["kept", "relabelled", "dropped"]


class Finding(BaseModel):
    rule: str
    severity: str
    page: str | None
    url: str
    tag: str | None
    text: str | None
    status: int | None
    error: str | None
    assessment: Assessment | None = None
    source: str = "tools"
    evidence: str | None = None
    note: str | None = None

class Report(BaseModel):
    target: str
    model: str
    started_at: str
    duration_s: float
    llm_requests: int
    input_tokens: int
    output_tokens: int
    counts: dict[str, int]
    narrative: str | None  # None for the static baseline, which has no LLM
    findings: list[Finding]
    unverified_agent_findings: list[AgentFinding] = []
    rejected_submissions: int = 0  # times the agent submitted findings it hadn't checked
    unbacked_claims: int = 0  # total unbacked findings across those submissions
    rejected_claims: list[RejectedClaim] = []  # each distinct claim that was sent back, and its outcome
    links_found: int = 0  # unique links found on the analyzed page(s)
    links_checked: int = 0  # how many of those were checked; should equal links_found


def page_soup(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()  # not visible to a reader
    return soup

def title_and_heading(soup):
    h1 = soup.find("h1")
    title = (soup.title.get_text(strip=True) or None) if soup.title else None
    heading = (h1.get_text(" ", strip=True) or None) if h1 else None
    return title, heading

def read_start(response, limit=MAX_PAGE_BYTES):
    body = b""
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) >= limit:
            break
    return body[:limit].decode(response.encoding or "utf-8", errors="replace")

def check_url(url):
    # one GET per link, like a browser; HTML pages also give their title and first heading
    result = {"url": url, "status": None, "final_url": None, "redirects": [], "content_type": None,
              "title": None, "heading": None, "error": None, "time_ms": None, "verdict": None}
    start = time.perf_counter()
    try:
        with client.stream("GET", url) as response:
            content_type = response.headers.get("content-type", "")
            html = read_start(response) if "text/html" in content_type else None
    except httpx.TooManyRedirects:
        result.update(error="too many redirects", verdict="broken")
    except httpx.TimeoutException:
        result.update(error="timeout", verdict="unreachable")
    except Exception as e:
        # DNS failures, refused connections, SSL errors, invalid URLs, ...
        result.update(error=str(e) or type(e).__name__, verdict="unreachable")
    else:
        if response.status_code in (403, 429):
            verdict = "blocked"  # often bot protection or rate limiting, not necessarily a broken link
        elif response.status_code >= 400:
            verdict = "broken"
        elif response.status_code >= 300:
            verdict = "broken" # a redirect that didn't resolve to a 2xx page is considered broken
        elif response.history:
            verdict = "redirect"
        else:
            verdict = "ok"
        result.update(
            status=response.status_code,
            final_url=str(response.url),
            redirects=[{"url": str(r.url), "status": r.status_code} for r in response.history],
            content_type=content_type.split(";")[0] or None,
            verdict=verdict,
        )
        if html is not None:
            result["title"], result["heading"] = title_and_heading(page_soup(html))
    result["time_ms"] = round((time.perf_counter() - start) * 1000)
    return result


def extract_links(url):
    try:
        response = client.get(url)
    except Exception as e:
        return {"url": url, "final_url": None, "error": str(e) or type(e).__name__, "links": []}
    if response.status_code >= 400:
        return {"url": url, "final_url": str(response.url), "error": f"HTTP {response.status_code}", "links": []}
    if "text/html" not in response.headers.get("content-type", ""):
        return {"url": url, "final_url": str(response.url), "error": "not an HTML page", "links": []}

    page_url = str(response.url)  # after redirects, so relative links resolve correctly
    try:
        doc = lxml.html.fromstring(response.content, base_url=page_url)
    except Exception as e:
        return {"url": url, "final_url": page_url, "error": f"could not parse the page: {e}", "links": []}
    doc.make_links_absolute(handle_failures="ignore")
    page_host = urlparse(page_url).netloc

    links = []
    seen = set()
    for element, attribute, link, _ in doc.iterlinks():
        if element.tag == "link" and IGNORED_LINK_RELS & set((element.get("rel") or "").lower().split()):
            continue
        link_url, _ = urldefrag(link.strip())
        if urlparse(link_url).scheme not in ("http", "https") or link_url in seen or link_url == page_url:
            continue  # skips mailto:, tel:, javascript:, data:, duplicates and links to this same page
        seen.add(link_url)
        text = " ".join(element.text_content().split()) if element.tag == "a" and attribute == "href" else ""
        links.append({
            "url": link_url,
            "text": text or None,
            "tag": element.tag,
            "attribute": attribute,  # None for links inside CSS in a <style> block
            "internal": urlparse(link_url).netloc == page_host,
        })
    return {"url": url, "final_url": page_url, "error": None, "links": links}

def check_urls(urls):
    return [check_url(url) for url in urls]

def check_page_links(url):
    page = extract_links(url)
    for link, result in zip(page["links"], check_urls([link["url"] for link in page["links"]])):
        link["check"] = result
    return page

def fetch_page_text(url, max_chars=2000):
    # returns the page's title and a short piece of its visible text
    result = {"url": url, "final_url": None, "status": None, "content_type": None,
              "title": None, "heading": None, "text": None, "truncated": False, "error": None}
    try:
        response = client.get(url)
    except Exception as e:
        result["error"] = str(e) or type(e).__name__
        return result
    content_type = response.headers.get("content-type", "")
    result.update(final_url=str(response.url), status=response.status_code,
                  content_type=content_type.split(";")[0] or None)
    if "text/html" not in content_type:
        return result  # PDFs, images, ...: no text to read

    soup = page_soup(response.text)
    title, heading = title_and_heading(soup)
    # Prefer the main content, so menus and footers don't use up the text budget
    root = soup.find("main") or soup.body or soup
    text = " ".join(root.get_text(" ", strip=True).split())
    result.update(
        title=title,
        heading=heading,
        text=text[:max_chars],
        truncated=len(text) > max_chars,
    )
    return result

def without_verdict(result):
    return {key: value for key, value in result.items() if key != "verdict"}

def hide_verdict(tool_name, tool_result):
    if tool_name == "check_url":
        return without_verdict(tool_result)
    if tool_name == "check_urls":
        return [without_verdict(result) for result in tool_result]
    if tool_name == "check_page_links":
        return {**tool_result, "links": [{**link, "check": without_verdict(link["check"])} for link in tool_result["links"]]}
    return tool_result

def process_tool(tool_name, tool_input):
    if tool_name == "check_url":
        return check_url(tool_input["url"])
    elif tool_name == "check_page_links":
        return check_page_links(tool_input["url"])
    elif tool_name == "check_urls":
        return check_urls(tool_input["urls"])
    elif tool_name == "fetch_page_text":
        return fetch_page_text(tool_input["url"])
    elif tool_name == "submit_report":
        try:
            return SubmitReport.model_validate(tool_input)
        except ValidationError as e:
            return {"error": f"Invalid report, fix it and call submit_report again: {e}"}
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

def build_findings(checks, link_info, assessments):
    assessment_by_url = {a.url: a for a in assessments}
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
        findings.append(Finding(
            rule=rule,
            severity=severity,
            page=info.get("page"),
            url=url,
            tag=tag,
            text=info.get("text"),
            status=check["status"],
            error=check["error"],
            assessment=assessment_by_url.get(url),
        ))
    return findings

def unbacked_reason(agent_finding, checks, fetched):
    if agent_finding.rule in NEEDS_PAGE_TEXT:
        return "not-read"
    if agent_finding.rule == "redirect-to-homepage" and (agent_finding.url in checks or agent_finding.url in fetched):
        return "not-homepage-redirect"
    return "no-tool-result"

def unbacked_message(unbacked, checks, fetched):
    lines = []
    for f in unbacked:
        reason = unbacked_reason(f, checks, fetched)
        if reason == "not-read":
            text = "you never read that page or the page it redirects to"
        elif reason == "not-homepage-redirect":
            text = f"the tools show it ends on {final_url_of(f.url, checks, fetched)}, which is not a redirect to a homepage"
        else:
            text = "no tool result shows this URL"
        lines.append(f"- {f.rule} for {f.url}: {text}")
    return ("Report not accepted. These findings are not backed by what the tools showed you:\n"
            + "\n".join(lines)
            + "\nCheck them (use fetch_page_text to read a page), give them the rule that fits, or remove them, "
              "then call submit_report again.")

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

def build_agent_findings(agent_findings, checks, fetched, link_info, findings):
    # Turn the agent's own findings into report findings, but only if a tool result backs them up
    already_errors = {f.url for f in findings if f.severity == "error"}
    verified, unverified = [], []
    for agent_finding in agent_findings:
        url = agent_finding.url
        if not is_backed(agent_finding, checks, fetched):
            unverified.append(agent_finding)
            continue
        if url in already_errors:
            continue  # already reported as broken by the tools
        info = link_info.get(url, {})
        result = checks.get(url) or fetched.get(url, {})
        verified.append(Finding(
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
    return verified, unverified

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
    tools = [
        {
            "name": "check_url",
            "description": "Checks whether a single URL works and returns its status code, final URL, "
                            "redirect chain, content type and any connection error, and for an HTML page "
                            "its title and first heading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to check."
                    }
                },
                "required": ["url"]
            }
        },
        {
            "name": "check_page_links",
            "description": "Finds every link on a web page and checks all of them. Returns each link's text, tag, "
                           "attribute and whether it is internal or external, with its status code, final URL, "
                           "redirect chain, content type and any connection error, and for a link to an HTML page "
                           "that page's title and first heading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the web page whose links to check."
                    }
                },
                "required": ["url"]
            }
        },
        {
            "name": "check_urls",
            "description": "Checks whether multiple URLs work and returns their status code, final URL, "
                            "redirect chain, content type and any connection error, and for HTML pages "
                            "their title and first heading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                        "description": "The URLs to check."
                    }
                },
                "required": ["urls"]
            }
        },
        {
            "name": "fetch_page_text",
            "description": "Fetches the title, first heading and a short snippet of visible text from a web page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the web page to fetch text from."
                    }
                },
                "required": ["url"]
            }
        },
        {
            "name": "submit_report",
            "description": "Hand in the final report. Call this once, after you have finished investigating.",
            "parameters": {
                "type": "object",
                "properties": {
                    "narrative": {
                        "type": "string",
                        "description": "A 2-4 sentence summary of the page's link health. Only mention things the tools checked."
                    },
                    "assessments": {
                        "type": "array",
                        "description": "One entry per broken, blocked or unreachable link. Empty if there are none.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "The link this comment is about."},
                                "likely_intentional": {"type": "boolean", "description": "True if the problem looks deliberate, e.g. a test page that demonstrates a 404."},
                                "note": {"type": "string", "description": "One or two sentences explaining the problem."},
                                "suggested_fix": {"type": "string", "description": "How to fix it, if you can tell. Leave this out if no fix is needed."}
                            },
                            "required": ["url", "likely_intentional", "note"]
                        }
                    },
                    "agent_findings": {
                        "type": "array",
                        "description": "Problems you found that the status code does not show, e.g. a page that returns 200 "
                                       "but is an error page, a redirect to the homepage or a login page, or link text that "
                                       "does not match the page it leads to. Empty if there are none.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "The link with the problem, exactly as the tools returned it."},
                                "rule": {"type": "string", "enum": list(get_args(AgentRule)), "description": "The kind of problem."},
                                "evidence": {"type": "string", "description": "What in the tool results shows the problem, e.g. the page title or the final URL."},
                                "note": {"type": "string", "description": "One or two sentences explaining the problem."}
                            },
                            "required": ["url", "rule", "evidence", "note"]
                        }
                    }
                },
                "required": ["narrative", "assessments", "agent_findings"]
            }
        }
    ]
    
    system_prompt = """
        You are a web link analyzer. Your goal is to find every link problem on the page the user gives you,
        including problems that the status code alone does not reveal.

        Start by calling check_page_links on the URL: it finds every link on the page and checks all of them.

        A status of 200 only means the server answered. It does not mean the page shows what the link promises:
        an error page, an empty page or the wrong page can all return 200. So for every link to a page, internal
        or external, ask whether its text, its URL and the page it ends up on agree. Look out for:
        - a page that returns 200 but is an error page, or no longer has what the link promises
        - a redirect that ends somewhere the link did not promise, such as the homepage, a login page
        - a link whose text does not match its URL or the page it leads to
        Read every page you cannot confirm from its link text, URL, title and heading alone with fetch_page_text.
        You can fetch several pages at once.

        Put the problems you found that the status code does not show in agent_findings,
        using the most specific rule that fits. Before reporting a soft-404, text-mismatch or other
        finding, read that page with fetch_page_text.

        Only report what the tools showed you. You do not check accessibility, so do not comment on it.
    """
    
    client = genai.Client(
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions())
    )
    MODEL_NAME = "gemini-3.1-flash-lite"
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[types.Tool(function_declarations=tools)],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="ANY")
        ),
        thinking_config=types.ThinkingConfig(include_thoughts=True, thinking_level="HIGH"),
    )

    user_message = input("\nEnter your message: ")
    contents = [types.Content(role="user", parts=[types.Part(text=user_message)])]
    
    # What the tools found, filled in during the run
    link_info = {}
    checks = {}
    fetched = {}
    target = None
    submitted = None

    # Metrics for the report
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    llm_requests = 0
    input_tokens = 0
    output_tokens = 0

    MAX_REJECTIONS = 2
    rejected_submissions = 0
    unbacked_claims = 0
    rejections = {}

    while True:
        if contents[-1].role == "model":
            user_message = input("\nEnter your message: ")
            contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=config
        )
        
        for part in response.candidates[0].content.parts or []:
            if part.thought and part.text:
                print(f"\n=====Gemini's thoughts=====\n{part.text}\n")
        
        llm_requests += 1
        usage = response.usage_metadata
        if usage:
            input_tokens += usage.prompt_token_count or 0
            output_tokens += (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)

        contents.append(response.candidates[0].content)

        if not response.function_calls:
            print("=====Gemini has no more function calls=====")
            break

        # Answer every tool call in this response, not just the last one
        response_parts = []
        for function_call in sorted(response.function_calls, key=lambda call: call.name == "submit_report"):
            tool_name = function_call.name
            tool_input = function_call.args
            print(f"=====Gemini wants to use the {tool_name} tool=====")

            tool_result = process_tool(tool_name, tool_input)

            if isinstance(tool_result, SubmitReport):
                unbacked = [f for f in tool_result.agent_findings if not is_backed(f, checks, fetched)]
                if not unbacked or rejected_submissions >= MAX_REJECTIONS:
                    submitted = tool_result
                    continue
                # Send the report back: the agent must look before it claims
                rejected_submissions += 1
                unbacked_claims += len(unbacked)
                for f in unbacked:
                    entry = rejections.setdefault((f.url, f.rule), {
                        "claim": f, "reason": unbacked_reason(f, checks, fetched), "times_rejected": 0})
                    entry["times_rejected"] += 1
                print(f"=====Report rejected: {len(unbacked)} unbacked finding(s)=====")
                tool_result = {"error": unbacked_message(unbacked, checks, fetched)}

            if tool_name == "check_page_links" and target is None:
                target = tool_input["url"]
            record_result(tool_name, tool_result, link_info, checks, fetched)
            print(f"=====Result of {tool_name}=====\n{hide_verdict(tool_name, tool_result)}\n")
            response_parts.append(
                types.Part.from_function_response(name=tool_name, response={"result": hide_verdict(tool_name, tool_result)})
            )

        if submitted is not None:
            print("=====Gemini has submitted the final report=====")
            break

        contents.append(types.Content(role="user", parts=response_parts))

    if submitted is None:
        print("=====No valid report was submitted=====")
        return

    findings = build_findings(checks, link_info, submitted.assessments)
    agent_findings, unverified = build_agent_findings(submitted.agent_findings, checks, fetched, link_info, findings)
    report = Report(
        target=target or user_message,
        model=MODEL_NAME,
        started_at=started_at,
        duration_s=round(time.perf_counter() - start, 1),
        llm_requests=llm_requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        counts=dict(Counter(check["verdict"] for check in checks.values())),
        narrative=submitted.narrative,
        findings=findings + agent_findings,
        unverified_agent_findings=unverified,
        rejected_submissions=rejected_submissions,
        unbacked_claims=unbacked_claims,
        rejected_claims=rejection_outcomes(rejections, submitted.agent_findings, checks, fetched),
        links_found=len(link_info),
        links_checked=sum(1 for url in link_info if url in checks),
    )
    report_json = report.model_dump_json(indent=2)
    Path("reports/report.json").write_text(report_json)
    print("=====Report saved to report.json=====")


if __name__ == "__main__":
    main()
            
    
    
