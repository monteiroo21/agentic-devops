import argparse
import json
import re
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import httpx
import lxml.html
from common import CONFIG, slug
from tools.links import read_start

CRAWL = CONFIG.crawl
HTML_TYPES = {"text/html", "application/xhtml+xml"}
DEFAULT_PORTS = {"http": 80, "https": 443}

@dataclass
class Link:
    url: str
    text: str | None
    via: str  # link, redirect or meta refresh
    nofollow: str | None = None  # why it mustn't be followed, if it mustn't: its rel, or the page's robots meta tag

@dataclass
class Found:
    url: str
    depth: int
    found_on: str | None = None
    link_text: str | None = None
    via: str = "start"

    def record(self):
        return asdict(self)

def normalize(url):
    parts = urlsplit(url.strip())
    scheme, host = parts.scheme.lower(), parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"  # IPv6
    if parts.port and parts.port != DEFAULT_PORTS.get(scheme):
        host += f":{parts.port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))

def crawlable(url):
    try:
        return normalize(url) if urlsplit(url).scheme in ("http", "https") else None
    except ValueError:
        return None

def in_scope(url, scope):
    host = urlsplit(url).hostname or ""
    if scope["mode"] == "domain":
        return host == scope["domain"] or host.endswith("." + scope["domain"])
    return host in scope["hosts"]

class Robots:
    # each host's robots.txt, fetched the first time a URL on that host is checked, and read as RFC 9309 says
    def __init__(self, client, agent):
        self.client = client
        self.agent = agent
        self.parsers = {}

    def parser(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.parsers:
            self.parsers[origin] = self.fetch(origin + "/robots.txt")
        return self.parsers[origin]

    def fetch(self, robots_url):
        parser = RobotFileParser(robots_url)
        try:
            response = self.client.get(robots_url, follow_redirects=True)
        except Exception:
            response = None
        if response is None or response.status_code >= 500:
            parser.disallow_all = True  # unreachable: nothing may be crawled
        elif response.status_code >= 400:
            parser.allow_all = True  # no robots.txt: everything may be crawled
        else:
            parser.parse(response.text.splitlines())
        return parser

    def allowed(self, url):
        return self.parser(url).can_fetch(self.agent, url)

    def crawl_delay(self, url):
        return self.parser(url).crawl_delay(self.agent) or 0

def link_text(element):
    text = " ".join(element.text_content().split())
    alt = " ".join(img.get("alt", "").strip() for img in element.iter("img")).strip()
    return text or element.get("aria-label") or element.get("alt") or alt or None

def page_links(doc):
    robots_meta = " ".join(doc.xpath("//meta[translate(@name, 'ROBTS', 'robts')='robots']/@content")).lower()
    page_nofollow = bool({"nofollow", "none"} & set(re.split(r"[\s,]+", robots_meta)))
    links = []
    for element, attribute, url, _ in doc.iterlinks():
        if element.tag in ("a", "area") and attribute == "href":
            rel = (element.get("rel") or "").lower().split()
            nofollow = ("nofollow in the page's robots meta tag" if page_nofollow
                        else "rel=nofollow" if "nofollow" in rel else None)
            links.append(Link(url, link_text(element), "link", nofollow))
        elif element.tag == "meta" and attribute == "content":
            links.append(Link(url, None, "meta refresh"))
    return links

def page_forms(doc, url):
    return [{
        "found_on": url,
        "method": (form.get("method") or "get").upper(),
        "action": form.get("action") or url,
        "fields": list(dict.fromkeys(field.get("name") for field in form.xpath(".//input | .//select | .//textarea")
                                     if field.get("name"))),
    } for form in doc.forms]

def fetch(client, url, max_bytes):
    entry = {"status": None, "content_type": None, "title": None, "redirect_to": None, "error": None}
    try:
        with client.stream("GET", url) as response:
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            redirect = response.has_redirect_location
            html = read_start(response, max_bytes) if content_type in HTML_TYPES and not redirect else None
    except httpx.TimeoutException:
        return entry | {"error": "timeout"}, [], []
    except Exception as e:
        # DNS failures, refused connections, SSL errors, ...
        return entry | {"error": str(e) or type(e).__name__}, [], []
    entry.update(status=response.status_code, content_type=content_type or None)
    if redirect:
        entry["redirect_to"] = urljoin(url, response.headers["location"])
        return entry, [Link(entry["redirect_to"], None, "redirect")], []
    if html is None:
        return entry, [], []  # PDFs, images, ...: nothing to follow
    try:
        doc = lxml.html.document_fromstring(html, base_url=url)
    except Exception as e:
        return entry | {"error": f"could not parse the page: {e}"}, [], []
    doc.make_links_absolute(handle_failures="ignore")  # also applies a <base href>
    entry["title"] = " ".join((doc.findtext(".//title") or "").split()) or None
    if not response.is_success:
        return entry, [], []  # an error page's links aren't part of the site
    return entry, page_links(doc), page_forms(doc, url)

class Crawler:
    # breadth-first, so when max_pages cuts a crawl short, the pages nearest the start are the ones crawled
    def __init__(self, scope, settings, cookies=None):
        self.scope = scope
        self.settings = settings
        self.skip_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in settings["skip_patterns"]]
        self.client = httpx.Client(follow_redirects=False, timeout=settings["timeout_s"],
                                   headers={"User-Agent": settings["user_agent"]}, cookies=cookies)
        self.robots = Robots(self.client, settings["robots_agent"]) if settings["respect_robots"] else None
        self.queue = deque()
        self.seen = set()
        self.pages, self.external, self.skipped, self.forms = [], {}, {}, []
        self.linked_from = Counter()  # how many crawled pages link or redirect to each URL

    def skip_reason(self, found):
        parts = urlsplit(found.url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        for pattern in self.skip_patterns:
            if pattern.search(target):
                return f"looks like an action: matches {pattern.pattern!r}"
        if found.depth > self.settings["max_depth"]:
            return f"deeper than max_depth ({self.settings['max_depth']})"
        if self.robots and not self.robots.allowed(found.url):
            return "disallowed by robots.txt"
        return None

    def add(self, found, nofollow=None):
        if found.url in self.seen or (nofollow and found.url in self.skipped):
            return
        if nofollow and in_scope(found.url, self.scope):
            self.skipped[found.url] = found.record() | {"reason": nofollow}
            return
        self.seen.add(found.url)
        self.skipped.pop(found.url, None)  # it was first found behind a nofollow link
        if not in_scope(found.url, self.scope):
            self.external[found.url] = found.record()
        elif reason := self.skip_reason(found):
            self.skipped[found.url] = found.record() | {"reason": reason}
        else:
            self.queue.append(found)

    def visit(self, current):
        if self.pages:
            time.sleep(max(self.settings["delay_s"], self.robots.crawl_delay(current.url) if self.robots else 0))
        entry, links, forms = fetch(self.client, current.url, self.settings["max_page_bytes"])
        self.pages.append(current.record() | entry)
        self.forms += forms
        print(f"[{len(self.pages)}] {entry['status'] or entry['error']} {current.url}")
        targets = set()
        for link in links:
            url = crawlable(link.url)
            if url is None or url == current.url:
                continue
            targets.add(url)
            depth = current.depth if link.via == "redirect" else current.depth + 1
            self.add(Found(url, depth, current.url, link.text, link.via), link.nofollow)
        self.linked_from.update(targets)

    def run(self, start_url):
        started_at, start = datetime.now().astimezone(), time.perf_counter()
        first = Found(normalize(start_url), depth=0)
        with self.client:
            self.add(first)
            while self.queue and len(self.pages) < self.settings["max_pages"]:
                self.visit(self.queue.popleft())
        for left in self.queue:
            self.skipped[left.url] = left.record() | {"reason": f"max_pages ({self.settings['max_pages']}) reached"}
        for entry in [*self.pages, *self.external.values(), *self.skipped.values()]:
            entry["linked_from"] = self.linked_from[entry["url"]]
        return {
            "start_url": first.url,
            "scope": self.scope,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "duration_s": round(time.perf_counter() - start, 1),
            "settings": self.settings,
            "truncated": bool(self.queue),  # max_pages was reached with pages still to crawl
            "counts": {"pages": len(self.pages), "external": len(self.external), "skipped": len(self.skipped),
                       "forms": len(self.forms)},
            "pages": self.pages,
            "external": list(self.external.values()),
            "skipped": list(self.skipped.values()),
            "forms": self.forms,
        }

def crawl_site(start_url, scope, settings, cookies=None):
    return Crawler(scope, settings, cookies).run(start_url)

def crawl_settings(max_pages=CRAWL.max_pages, max_depth=CRAWL.max_depth, delay_s=CRAWL.delay_s, cookie_names=()):
    return {"max_pages": max_pages, "max_depth": max_depth, "delay_s": delay_s, "timeout_s": CRAWL.timeout_s,
            "max_page_bytes": CRAWL.max_page_bytes, "user_agent": CRAWL.user_agent,
            "robots_agent": CRAWL.robots_agent, "respect_robots": CRAWL.respect_robots,
            "skip_patterns": CRAWL.skip_patterns, "cookies": sorted(cookie_names)}

def save_crawl(result):
    url = urlsplit(result["start_url"])
    page = slug(url.path)  # empty for the homepage
    stamp = datetime.fromisoformat(result["started_at"]).strftime("%Y%m%d-%H%M%S")
    path = CRAWL.folder / (slug(url.netloc) + (f"_{page}" if page else "") + f"_{stamp}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return path

def latest_crawl(start_url, scope, settings, max_age_hours=CRAWL.max_age_hours):
    start_url = normalize(start_url)
    oldest = datetime.now().astimezone() - timedelta(hours=max_age_hours)
    crawls = [json.loads(path.read_text())
              for path in CRAWL.folder.glob(f"{slug(urlsplit(start_url).netloc)}_*.json")]
    recent = [crawl for crawl in crawls
              if (crawl["start_url"], crawl["scope"], crawl["settings"]) == (start_url, scope, settings)
              and datetime.fromisoformat(crawl["finished_at"]) >= oldest]
    return max(recent, key=lambda crawl: datetime.fromisoformat(crawl["finished_at"]), default=None)

def load_or_crawl(start_url, scope, settings, cookies=None, max_age_hours=CRAWL.max_age_hours):
    # for a site run: a recent crawl if there is one, otherwise a new one, saved
    result = latest_crawl(start_url, scope, settings, max_age_hours)
    if result is None:
        result = crawl_site(start_url, scope, settings, cookies)
        save_crawl(result)
    return result

def make_scope(args):
    host = urlsplit(args.url).hostname
    if args.scope == "domain":
        return {"mode": "domain", "domain": (args.domain or host.removeprefix("www.")).lower()}
    if args.scope == "hosts":
        return {"mode": "hosts", "hosts": sorted({host, *(other.lower() for other in args.hosts)})}
    return {"mode": "host", "hosts": [host]}

def make_cookies(pairs, scope):
    cookies = httpx.Cookies()
    for pair in pairs:
        name, _, value = pair.partition("=")
        for host in scope.get("hosts") or [scope["domain"]]:
            cookies.set(name.strip(), value.strip(), domain=host)
    return cookies

def parse_args(parser):
    args = parser.parse_args()
    if urlsplit(args.url).scheme not in ("http", "https") or not urlsplit(args.url).hostname:
        parser.error(f"the URL must start with http:// or https://, got {args.url!r}")
    if args.domain and args.scope != "domain":
        parser.error("--domain only works with --scope domain")
    if args.hosts and args.scope != "hosts":
        parser.error("--hosts only works with --scope hosts")
    if args.scope == "domain" and not in_scope(args.url, make_scope(args)):
        parser.error(f"the start URL isn't on {make_scope(args)['domain']}")
    if args.max_pages < 1 or args.max_depth < 0 or args.delay < 0:
        parser.error("--max-pages must be at least 1, and --max-depth and --delay can't be negative")
    if any("=" not in cookie for cookie in args.cookie):
        parser.error("each --cookie must be NAME=VALUE")
    return args

def main():
    parser = argparse.ArgumentParser(description="Crawl a site over plain HTTP, without a model, and save every page "
                                                 "it finds, with the page and link that lead to it.")
    parser.add_argument("url", help="the page to start from")
    parser.add_argument("--scope", choices=["host", "domain", "hosts"], default="host",
                        help="host: the start URL's host only; domain: every host on --domain; "
                             "hosts: the start host and --hosts (default host)")
    parser.add_argument("--domain", help="with --scope domain: the domain, by default the start host without www.")
    parser.add_argument("--hosts", nargs="+", default=[], metavar="HOST", help="with --scope hosts: the other hosts")
    parser.add_argument("--max-pages", type=int, default=CRAWL.max_pages,
                        help=f"the most pages to request (default {CRAWL.max_pages})")
    parser.add_argument("--max-depth", type=int, default=CRAWL.max_depth,
                        help=f"the most link hops from the start page (default {CRAWL.max_depth})")
    parser.add_argument("--delay", type=float, default=CRAWL.delay_s,
                        help=f"seconds between two requests (default {CRAWL.delay_s})")
    parser.add_argument("--cookie", action="append", default=[], metavar="NAME=VALUE",
                        help="a cookie to send from the start, like a session cookie; repeat for more")
    args = parse_args(parser)

    scope = make_scope(args)
    settings = crawl_settings(args.max_pages, args.max_depth, args.delay,
                              [cookie.partition("=")[0].strip() for cookie in args.cookie])
    result = crawl_site(args.url, scope, settings, make_cookies(args.cookie, scope))
    path = save_crawl(result)

    counts = result["counts"]
    print(f"=====Crawled {counts['pages']} page(s): {counts['external']} external URL(s) seen, "
          f"{counts['skipped']} skipped, {counts['forms']} form(s)=====")
    pages = result["pages"]
    if len(pages) == 1 and pages[0]["redirect_to"] and result["external"]:
        print(f"=====The start URL redirects to {pages[0]['redirect_to']}, outside the scope; start there instead=====")
    if result["truncated"]:
        print("=====Stopped at --max-pages; the pages still to crawl are listed as skipped=====")
    print(f"=====Crawl saved to {path}=====")

if __name__ == "__main__":
    main()
