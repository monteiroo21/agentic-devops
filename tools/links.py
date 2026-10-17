import time
from urllib.parse import urldefrag, urlparse
import httpx
import lxml.html
from bs4 import BeautifulSoup
from common import CONFIG

LINKS = CONFIG.links

client = httpx.Client(
    follow_redirects=True,
    timeout=LINKS.timeout_s,
    headers={"User-Agent": LINKS.user_agent},
)

IGNORED_LINK_RELS = {"preconnect", "dns-prefetch"}
HIDDEN_FIELDS = {"verdict", "time_ms"}  # the model must judge each link itself, and timings don't help it

CHECK_URL = ("Checks whether a single URL works and returns its status code, final URL, redirect chain, content type "
             "and any connection error, and for an HTML page its title and first heading. Empty fields are left out, "
             "and the final URL is only given when it differs from the URL.")

CHECK_PAGE_LINKS = ("Finds every link on a web page and checks all of them. Returns each link's text, tag and whether "
                    "it is internal or external, with its status code, final URL, redirect chain, content type and any "
                    "connection error, and for a link to an HTML page that page's title and first heading. Empty "
                    "fields are left out, and the final URL is only given when it differs from the link's URL.")

CHECK_URLS = ("Checks whether multiple URLs work and returns their status code, final URL, redirect chain, content "
              "type and any connection error, and for HTML pages their title and first heading. Empty fields are left "
              "out, and the final URL is only given when it differs from the URL.")

FETCH_PAGE_TEXT = ("Fetches the title, first heading and a short snippet of visible text from a web page. Empty "
                   "fields are left out, and the final URL is only given when the page redirected.")

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

def read_start(response, limit=LINKS.max_page_bytes):
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

def fetch_page_text(url, max_chars=LINKS.max_page_text_chars):
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

def compact(result, leave_out=()):
    kept = {key: value for key, value in result.items()
            if key not in HIDDEN_FIELDS and key not in leave_out and value is not None and value != []}
    if kept.get("final_url") == result.get("url"):
        kept.pop("final_url", None)
    return kept

def link_for_model(link):
    # a link and its check as one object, since the check repeats the link's URL
    return {**compact(link, leave_out=("attribute", "check")), **compact(link["check"], leave_out=("url",))}

def for_model(tool_name, tool_result):
    if tool_name == "check_url":
        return compact(tool_result)
    if tool_name == "check_urls":
        return [compact(result) for result in tool_result]
    if tool_name == "check_page_links":
        links = [link_for_model(link) for link in tool_result["links"]]
        return {**compact(tool_result, leave_out=("links",)), "links": links}
    if tool_name == "fetch_page_text":
        return compact(tool_result, leave_out=() if tool_result["truncated"] else ("truncated",))
    return tool_result