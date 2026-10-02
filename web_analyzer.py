import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
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

LINK_ATTRIBUTES = {"a": "href", "img": "src", "script": "src", "link": "href", "iframe": "src"}
IGNORED_LINK_RELS = {"preconnect", "dns-prefetch"}

class Assessment(BaseModel):
    url: str
    likely_intentional: bool
    note: str
    suggested_fix: str | None = None

AgentRule = Literal["soft-404", "redirect-to-homepage", "redirect-to-login", "text-mismatch", "other"]
NEEDS_PAGE_TEXT = {"soft-404", "text-mismatch"}

class AgentFinding(BaseModel):
    url: str
    rule: AgentRule
    evidence: str
    note: str

class SubmitReport(BaseModel):
    narrative: str
    assessments: list[Assessment]
    agent_findings: list[AgentFinding] = []


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


def check_url(url):
    result = {"url": url, "status": None, "final_url": None, "redirects": [],
              "error": None, "time_ms": None, "verdict": None}
    start = time.perf_counter()
    try:
        response = client.head(url)
        if response.status_code >= 400:
            with client.stream("GET", url) as response:
                pass
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
            verdict=verdict,
        )
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
    soup = BeautifulSoup(response.text, "html.parser")
    base = soup.find("base", href=True)
    base_url = urljoin(page_url, base["href"]) if base else page_url
    page_host = urlparse(page_url).netloc

    links = []
    seen = set()
    for tag in soup.find_all(list(LINK_ATTRIBUTES)):
        raw = (tag.get(LINK_ATTRIBUTES[tag.name]) or "").strip()
        if not raw or raw.startswith("#"):
            continue  # missing attribute or same-page anchor
        if tag.name == "link" and IGNORED_LINK_RELS & set(tag.get("rel", [])):
            continue
        link_url, _ = urldefrag(urljoin(base_url, raw))
        if urlparse(link_url).scheme not in ("http", "https") or link_url in seen:
            continue  # skips mailto:, tel:, javascript:, data: and duplicates
        seen.add(link_url)
        links.append({
            "url": link_url,
            "text": (tag.get_text(strip=True) or None) if tag.name == "a" else None,
            "tag": tag.name,
            "internal": urlparse(link_url).netloc == page_host,
        })
    return {"url": url, "final_url": page_url, "error": None, "links": links}

def check_urls(urls):
    return [check_url(url) for url in urls]

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

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()  # not visible to a reader
    h1 = soup.find("h1")
    # Prefer the main content, so menus and footers don't use up the text budget
    root = soup.find("main") or soup.body or soup
    text = " ".join(root.get_text(" ", strip=True).split())
    result.update(
        title=(soup.title.get_text(strip=True) or None) if soup.title else None,
        heading=(h1.get_text(" ", strip=True) or None) if h1 else None,
        text=text[:max_chars],
        truncated=len(text) > max_chars,
    )
    return result

def hide_verdict(tool_name, tool_result):
    if tool_name == "check_url":
        return {key: value for key, value in tool_result.items() if key != "verdict"}
    if tool_name == "check_urls":
        return [{key: value for key, value in result.items() if key != "verdict"} for result in tool_result]
    return tool_result

def process_tool(tool_name, tool_input):
    if tool_name == "check_url":
        return check_url(tool_input["url"])
    elif tool_name == "extract_links":
        return extract_links(tool_input["url"])
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

def unbacked_message(unbacked):
    lines = []
    for f in unbacked:
        reason = "you never read that page" if f.rule in NEEDS_PAGE_TEXT else "no tool result shows this URL"
        lines.append(f"- {f.rule} for {f.url}: {reason}")
    return ("Report not accepted. These findings are not backed by anything you looked at:\n"
            + "\n".join(lines)
            + "\nCheck them (use fetch_page_text to read a page) or remove them, then call submit_report again.")

def record_result(tool_name, tool_result, link_info, checks, fetched=None):
    if tool_name == "extract_links":
        for link in tool_result["links"]:
            link_info.setdefault(link["url"], {"page": tool_result["final_url"], "tag": link["tag"], "text": link["text"]})
    elif tool_name == "check_url":
        checks[tool_result["url"]] = tool_result
    elif tool_name == "check_urls":
        for result in tool_result:
            checks[result["url"]] = result
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

def is_backed(agent_finding, checks, fetched):
    if agent_finding.rule in NEEDS_PAGE_TEXT:
        return agent_finding.url in fetched  # it must have read the page
    return agent_finding.url in fetched or agent_finding.url in checks

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

def main():
    tools = [
        {
            "name": "check_url",
            "description": "Checks whether a single URL works and returns its status code, final URL, "
                            "redirect chain and any connection error.",
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
            "name": "extract_links",
            "description": "Extracts all links from a given web page, including their text, tag and whether they are "
                           "internal or external.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the web page to extract links from."
                    }
                },
                "required": ["url"]
            }
        },
        {
            "name": "check_urls",
            "description": "Checks whether multiple URLs work and returns their status code, final URL, "
                            "redirect chain and any connection error.",
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

        Start by extracting the links and checking all of them. Then look for anything suspicious and
        investigate it with fetch_page_text, for example:
        - an internal link that returns 200 but may be an error page ("not found", "página não encontrada")
        - a redirect that ends on the homepage or on a login page
        - a link whose text does not match the page it leads to
        You don't need to fetch every page; you can fetch several pages at once. Keep the number of tool calls to a minimum necessary,
        then call submit_report. Put the problems you found that the status code does not show in agent_findings.

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

    while True:
        if contents[-1].role == "model":
            user_message = input("\nEnter your message: ")
            contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=config
        )
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
                print(f"=====Report rejected: {len(unbacked)} unbacked finding(s)=====")
                tool_result = {"error": unbacked_message(unbacked)}

            if tool_name == "extract_links" and target is None:
                target = tool_input["url"]
            record_result(tool_name, tool_result, link_info, checks, fetched)
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
    )
    report_json = report.model_dump_json(indent=2)
    print(report_json)
    Path("reports/report.json").write_text(report_json)
    print("=====Report saved to report.json=====")


if __name__ == "__main__":
    main()
            
    
    
