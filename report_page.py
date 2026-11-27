import argparse
import json
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from common import IMPACTS
from tools.axe import wcag_number

IMPACT_LABELS = {"critical": "Critical", "serious": "Serious", "moderate": "Moderate", "minor": "Minor"}
CHECKS = {"links": "Links", "accessibility": "Accessibility"}
PROBLEMS = {"error", "warning"}  # the severities of a problem found; needs-review is undecided, info is a notice
SHOWN_PAGES = 3  # a finding on more pages lists them folded
TITLES = {  # axe-core's rules are titled with its own name for them
    "broken-link": "Broken links",
    "broken-resource": "Broken images, stylesheets or scripts",
    "unreachable": "Links to servers that don't answer",
    "blocked": "Links the server refused to the link checker",
    "redirect": "Redirected links",
    "skipped": "Links not requested, because they look like actions",
    "soft-404": "Pages that answer but are really error pages (soft 404s)",
    "redirect-to-homepage": "Links that redirect to the homepage",
    "redirect-to-login": "Links that redirect to a sign-in page",
    "other": "Links that redirect somewhere other than they promise",
    "text-mismatch": "Links whose text doesn't match where they lead",
    "alt-text": "Images whose alt text doesn't say what they add",
    "link-purpose": "Links whose purpose isn't clear",
    "page-title": "Page titles that don't say what the page is about",
    "page-language": "Page language not marked correctly",
    "heading": "Headings that don't describe their section, or are out of order",
    "axe-needs-review": "Elements axe-core couldn't decide",
}
NOTICES = {"redirect": "Redirects", "blocked": "Refused to the link checker", "skipped": "Not requested"}

CSS = """
:root {
  color-scheme: light;
  --plane: #f9f9f7; --surface: #fcfcfb; --code: #f0efec; --ink: #0b0b0b; --ink-2: #52514e; --link: #1d5fb4;
  --hairline: #e1e0d9; --border: rgba(11, 11, 11, 0.10);
  --critical: #d03b3b; --serious: #ec835a; --moderate: #fab219; --minor: #898781; --review: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --plane: #0d0d0d; --surface: #1a1a19; --code: #262624; --ink: #f0efec; --ink-2: #c3c2b7; --link: #7fb2f0;
    --hairline: #2c2c2a; --border: rgba(255, 255, 255, 0.10); --review: #3987e5;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--plane); color: var(--ink); font: 15px/1.5 system-ui, sans-serif; }
header, main { max-width: 1080px; margin: 0 auto; padding: 0 24px; }
header { padding-top: 32px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 20px; margin: 40px 0 12px; }
h3 { font-size: 16px; margin: 28px 0 8px; }
a { color: var(--link); }
.meta, .count, .empty, .note { color: var(--ink-2); }
.count { font-weight: normal; }
.card, details.group, .tile, table { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; list-style: none; padding: 0; margin: 24px 0 0; }
.tile { padding: 12px 16px; min-width: 128px; }
.tile .label { display: block; color: var(--ink-2); font-size: 13px; }
.tile .value { display: block; font-size: 28px; font-weight: 600; }
.tile .sub { display: block; color: var(--ink-2); font-size: 13px; }
.by-check { margin: 8px 0 0; color: var(--ink-2); }
.narrative { max-width: 75ch; }
.mark { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px;
        vertical-align: baseline; border: 1px solid var(--border); }
.mark.critical { background: var(--critical); } .mark.serious { background: var(--serious); }
.mark.moderate { background: var(--moderate); } .mark.minor { background: var(--minor); }
.mark.unrated { background: transparent; border-color: var(--ink-2); } .mark.review { background: var(--review); }
.badge { display: inline-block; min-width: 96px; font-size: 13px; font-weight: 600; white-space: nowrap; }
details.group { margin: 8px 0; }
details.group > summary { padding: 12px 16px; cursor: pointer; display: flex; flex-wrap: wrap; gap: 4px 12px;
                          align-items: baseline; }
details.group > summary .title { font-weight: 600; flex: 1 1 320px; }
details.group[open] > summary { border-bottom: 1px solid var(--hairline); }
.group-body { padding: 4px 16px 16px; }
ol.findings { list-style: none; margin: 0; padding: 0; }
.card { margin-top: 12px; padding: 12px 16px; }
.card .top { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
.tag { font-size: 12px; border: 1px solid var(--border); border-radius: 4px; padding: 0 6px; color: var(--ink-2); }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 8px 0 0; }
dl.facts dt { color: var(--ink-2); }
dl.facts dd { margin: 0; min-width: 0; }
code, pre { font: 13px/1.45 ui-monospace, monospace; background: var(--code); border-radius: 4px; }
code { padding: 1px 4px; overflow-wrap: anywhere; }
pre { padding: 8px; margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
details.pages > summary { cursor: pointer; }
details.pages ul { margin: 4px 0 0; padding-left: 20px; }
details.section > summary { cursor: pointer; margin: 40px 0 12px; }
details.section > summary h2 { display: inline; }
table { border-collapse: separate; border-spacing: 0; width: 100%; overflow: hidden; }
th, td { text-align: left; vertical-align: top; padding: 8px 12px; border-bottom: 1px solid var(--hairline);
         overflow-wrap: anywhere; }
th { font-size: 13px; color: var(--ink-2); font-weight: 600; }
tr:last-child td { border-bottom: none; }
footer { max-width: 1080px; margin: 48px auto 32px; padding: 0 24px; color: var(--ink-2); font-size: 13px; }
"""

def impact_of(finding):
    return finding.get("impact") if finding.get("impact") in IMPACTS else None

def impact_rank(finding):
    impact = impact_of(finding)
    return IMPACTS.index(impact) if impact else len(IMPACTS)

def badge(impact):
    css = impact or "unrated"
    label = IMPACT_LABELS.get(impact, "Not rated")
    return f'<span class="badge"><span class="mark {css}" aria-hidden="true"></span>{label}</span>'

def title_of(finding):
    return finding.get("help") or TITLES.get(finding["rule"], finding["rule"])

def wcag_label(tags):
    numbers = [wcag_number(tag) for tag in tags if tag[4:].isdigit()]  # criteria like wcag111, not levels like wcag2aa
    return "WCAG " + ", ".join(numbers) if numbers else None

def noun(finding):
    if finding["check"] == "links":
        return "page" if finding["rule"] == "soft-404" else "link"
    return "element" if finding.get("selector") else "page"

def counted(findings):
    nouns = {noun(finding) for finding in findings}
    word = nouns.pop() if len(nouns) == 1 else "finding"
    return f"{len(findings)} {word}{'s' if len(findings) != 1 else ''}"

def grouped(findings):
    # by rule, the worst first, then the most common
    groups = {}
    for finding in findings:
        groups.setdefault((finding["check"], finding["rule"]), []).append(finding)
    for group in groups.values():
        group.sort(key=impact_rank)
    return sorted(groups.values(), key=lambda group: (impact_rank(group[0]), -len(group)))

def duration(seconds):
    minutes, seconds = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"

def tokens(number):
    return f"{number / 1000:.1f}K" if number >= 1000 else str(number)

class ReportPage:
    def __init__(self, report):
        self.report = report
        self.fleet = report.get("mode") == "fleet"
        self.agent_of = {skill: agent for agent, skills in report.get("agents", {}).items() for skill in skills}
        self.host = urlsplit(report["target"]).hostname
        self.checked = {check: [page["url"] for page in report["pages"]
                                if page["checks"].get(check) in ("analyzed", "partly analyzed")]
                        for check in CHECKS}
        self.items = {item["id"]: item for item in report["items"]}
        findings = report["findings"]
        self.problems = [finding for finding in findings if finding["severity"] in PROBLEMS]
        self.to_review = [finding for finding in findings if finding["severity"] == "needs-review"]
        self.notices = [finding for finding in findings if finding["severity"] == "info"]

    def link(self, url):
        parts = urlsplit(url)
        shown = url if parts.hostname != self.host else (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return f'<a href="{escape(url)}">{escape(shown)}</a>'

    def started(self):
        started = datetime.fromisoformat(self.report["started_at"])
        return f"{started.day} {started:%b %Y, %H:%M} UTC"

    def page_list(self, check, pages):
        if len(pages) <= SHOWN_PAGES:
            return ", ".join(self.link(url) for url in pages)
        checked = self.checked[check]
        label = (f"every page checked ({len(pages)})" if set(checked) <= set(pages)
                 else f"{len(pages)} of the {len(checked)} pages checked")
        listed = "".join(f"<li>{self.link(url)}</li>" for url in pages)
        return f'<details class="pages"><summary>{label}</summary><ul>{listed}</ul></details>'

    def found_by(self, finding):
        source, item = finding["source"], self.items.get(finding.get("item"), {})
        if source == "checker":
            return "The link checker"
        if source == "axe":
            return "axe-core"
        if self.fleet:
            skill = item.get("criterion", "?")
            agent = self.agent_of.get(skill, "an agent")
            if skill == "axe-needs-review":
                return f"axe-core couldn't decide it, so the {escape(agent)} agent judged it"
            return f"The {escape(agent)} agent, with its {escape(skill)} skill"
        first = ("axe-core couldn't decide it, so a worker model judged it"
                 if item.get("criterion") == "axe-needs-review" else "A worker model")
        if source == "orchestrator":
            return (f"{first}; the orchestrator changed its verdict from {escape(item.get('worker_verdict', '?'))} to "
                    f"{escape(finding.get('verdict') or '?')}")
        if item.get("decision") == "confirmed":
            return f"{first}; the orchestrator confirmed it"
        return f"{first}; the orchestrator didn't review it"

    def card(self, finding):
        facts = []
        check = finding["check"]
        if check == "links":
            what = self.link(finding["url"])
            if finding.get("text"):
                what += f" · text “{escape(finding['text'])}”"
            if finding.get("status") and finding["rule"] != "soft-404":
                what += f" · status {finding['status']}"
            if finding.get("error"):
                what += f" · {escape(finding['error'])}"
            facts.append(("Page" if finding["rule"] == "soft-404" else "Link", what))
            facts.append(("Linked from" if finding["rule"] == "soft-404" else "On",
                          self.page_list(check, finding["pages"])))
        else:
            facts.append(("On", self.page_list(check, finding["pages"])))
            if finding.get("selector"):
                facts.append(("Element", f"<code>{escape(finding['selector'])}</code>"))
            if finding.get("html"):
                facts.append(("HTML", f"<pre><code>{escape(finding['html'])}</code></pre>"))
        why = finding.get("evidence") or finding.get("note")
        if why:
            facts.append(("Why", escape(why)))
        if finding.get("suggested_fix"):
            facts.append(("Fix", escape(finding["suggested_fix"])))
        facts.append(("Found by", self.found_by(finding)))
        top = [badge(impact_of(finding))] if finding["severity"] in PROBLEMS else []
        if finding.get("likely_intentional"):
            top.append('<span class="tag">Looks deliberate</span>')
        rows = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in facts)
        top_html = f'<div class="top">{"".join(top)}</div>' if top else ""
        return f'<li class="card">{top_html}<dl class="facts">{rows}</dl></li>'

    def group(self, findings, review=False):
        first = findings[0]
        meta = [counted(findings)]
        if wcag_label(first.get("wcag") or []):
            meta.append(wcag_label(first["wcag"]))
        mark = ('<span class="badge"><span class="mark review" aria-hidden="true"></span>Needs review</span>' if review
                else badge(impact_of(first)))
        about = ""
        if first.get("help_url"):
            about = f'<p class="note"><a href="{escape(first["help_url"])}">Learn more about this rule</a></p>'
        cards = "".join(self.card(finding) for finding in findings)
        return (f'<details class="group"><summary>{mark}<span class="title">{escape(title_of(first))}</span>'
                f'<span class="meta">{" · ".join(meta)}</span></summary>'
                f'<div class="group-body">{about}<ol class="findings">{cards}</ol></div></details>')

    def header(self):
        report = self.report
        meta = (f'<a href="{escape(report["target"])}">{escape(report["target"])}</a> · {self.started()} · '
                f'took {duration(report["duration_s"])} · orchestrator {escape(report["model"])}, '
                f'{"agents" if self.fleet else "workers"} {escape(report["worker_model"])}')
        return f'<header><h1>Site check of {escape(self.host)}</h1><p class="meta">{meta}</p>{self.tiles()}</header>'

    def tiles(self):
        counts = Counter(impact_of(finding) for finding in self.problems)
        tiles = [(badge(impact), counts[impact], "problems") for impact in IMPACTS]
        if counts[None]:
            tiles.append((badge(None), counts[None], "problems"))
        tiles.append(('<span class="badge"><span class="mark review" aria-hidden="true"></span>Needs review</span>',
                      len(self.to_review), "undecided"))
        coverage = self.report["coverage"]["pages"]
        tiles.append(("Pages checked", coverage["to_analyze"], f"of {coverage['found']} found"))
        listed = "".join(f'<li class="tile"><span class="label">{label}</span><span class="value">{value}</span>'
                         f'<span class="sub">{sub}</span></li>' for label, value, sub in tiles)
        to_analyze = [page for page in self.report["pages"]
                      if page["status"] == 200 and page["content_type"] == "text/html"]
        by_check = []
        for check, label in CHECKS.items():
            line = (f"{label}: {sum(finding['check'] == check for finding in self.problems)} problems on "
                    f"{len(self.checked[check])} pages")
            # left out, failed, or never sent; a page that lands on another one is judged there
            skipped = sum(page["checks"].get(check) in ("left out", "failed", "not analyzed") for page in to_analyze)
            if skipped:
                line += f" ({skipped} of the {len(to_analyze)} pages weren't checked: see Pages)"
            by_check.append(line)
        return f'<ul class="tiles">{listed}</ul><p class="by-check">{" · ".join(by_check)}</p>'

    def summary(self):
        narrative = self.report.get("narrative")
        unfinished = ("some pages may not have been checked" if self.fleet
                      else "some items may not have been reviewed")
        text = escape(narrative) if narrative else f"The orchestrator stopped before it wrote a summary, so {unfinished}."
        return (f'<section aria-labelledby="summary"><h2 id="summary">Summary</h2><p class="narrative">{text}</p>'
                '</section>')

    def problems_section(self):
        parts = []
        for check, label in CHECKS.items():
            found = [finding for finding in self.problems if finding["check"] == check]
            groups = "".join(self.group(group) for group in grouped(found)) or '<p class="empty">No problems found.</p>'
            parts.append(f'<section aria-labelledby="{check}"><h3 id="{check}">{label} '
                         f'<span class="count">{len(found)} problems</span></h3>{groups}</section>')
        return ('<section aria-labelledby="problems"><h2 id="problems">Problems</h2>'
                '<p class="note">Grouped by rule, the worst first. Open a rule to see where each problem is, why it '
                'is one and how to fix it.</p>' + "".join(parts) + "</section>")

    def review_section(self):
        if not self.to_review:
            return ""
        groups = "".join(self.group(group, review=True) for group in grouped(self.to_review))
        who = "The agents couldn't" if self.fleet else "Neither the workers nor the orchestrator could"
        return ('<section aria-labelledby="review"><h2 id="review">Needs review</h2><p class="note">'
                f'{who} decide these from what the tools showed them, so a person should look at them.</p>'
                f'{groups}</section>')

    def notices_section(self):
        if not self.notices:
            return ""
        rows = "".join(
            f"<tr><td>{self.link(finding['url'])}</td><td>{escape(NOTICES.get(finding['rule'], finding['rule']))}"
            + (f"<br><span class=\"note\">{escape(finding.get('note') or finding.get('error') or '')}</span>"
               if finding.get("note") or finding.get("error") else "")
            + f"</td><td>{self.page_list(finding['check'], finding['pages'])}</td></tr>"
            for finding in sorted(self.notices, key=lambda finding: finding["rule"]))
        return (f'<details class="section"><summary><h2>Notices ({len(self.notices)})</h2></summary>'
                '<p class="note">Not problems in themselves: redirects, and links the checker couldn\'t or didn\'t '
                'request.</p>'
                f'<table><thead><tr><th scope="col">Link</th><th scope="col">What</th><th scope="col">On</th></tr>'
                f'</thead><tbody>{rows}</tbody></table></details>')

    def overturned_section(self):
        # the workers' false alarms, which the report would have shown without the orchestrator
        items = [item for item in self.items.values() if item["decision"] == "changed" and item["verdict"] == "pass"]
        if not items:
            return ""
        rows = "".join(
            f"<tr><td>{escape(TITLES.get(item['criterion'], item['criterion']))}</td>"
            f"<td>{self.link(item['url']) if item.get('url') else ''}"
            + (f"<code>{escape(item['selector'])}</code>" if item.get("selector") else "")
            + f"<br>on {self.page_list(item['check'], item['pages'])}</td>"
            f"<td>{escape(item.get('worker_reason') or '')}</td><td>{escape(item.get('reason') or '')}</td></tr>"
            for item in items)
        return (f'<details class="section"><summary><h2>False alarms the orchestrator removed ({len(items)})</h2>'
                '</summary>'
                '<p class="note">Items a worker failed or was unsure about, which the orchestrator changed to a pass '
                'after reading their evidence.</p><table><thead><tr><th scope="col">Criterion</th>'
                '<th scope="col">Where</th><th scope="col">The worker said</th><th scope="col">The orchestrator said'
                f'</th></tr></thead><tbody>{rows}</tbody></table></details>')

    def pages_section(self):
        def status(page, check):
            state = page["checks"].get(check, "not analyzed")
            why = page.get("why", {}).get(check)
            if state == "lands on another page" and why:
                return f"lands on {self.link(why)}"
            return escape(state) + (f'<br><span class="note">{escape(why)}</span>' if why else "")
        rows = "".join(
            f"<tr><td>{self.link(page['url'])}</td><td>{page['status'] or ''}</td>"
            + "".join(f"<td>{status(page, check)}</td>" for check in CHECKS) + "</tr>"
            for page in self.report["pages"])
        heads = "".join(f'<th scope="col">{label}</th>' for label in CHECKS.values())
        return (f'<details class="section"><summary><h2>Pages ({len(self.report["pages"])})</h2></summary><table>'
                '<thead><tr>'
                f'<th scope="col">Page</th><th scope="col">Status</th>{heads}</tr></thead><tbody>{rows}</tbody>'
                '</table></details>')

    def footer(self):
        report = self.report
        if self.fleet:
            runs = len(report.get("agent_runs", report.get("specialist_runs", [])))
            return (f'<footer>The orchestrator made {report["llm_requests"]} requests '
                    f'({tokens(report["input_tokens"])} tokens in, {tokens(report["output_tokens"])} out); the '
                    f'agents made {report["worker_llm_requests"]} over {runs} runs '
                    f'({tokens(report["worker_input_tokens"])} in, {tokens(report["worker_output_tokens"])} out), and '
                    f'judged {report["coverage"]["items"]["elements_judged"]} elements across pages. Their answers are '
                    'final: the orchestrator doesn\'t review them.</footer>')
        decisions = report["decisions"]
        changed = ", ".join(f"{kind.replace('->', ' → ')}: {number}" for kind, number in decisions.items()
                            if kind != "confirmed")
        return (f'<footer>The orchestrator made {report["llm_requests"]} requests ({tokens(report["input_tokens"])} '
                f'tokens in, {tokens(report["output_tokens"])} out); the workers made {report["worker_llm_requests"]} '
                f'({tokens(report["worker_input_tokens"])} in, {tokens(report["worker_output_tokens"])} out), and '
                f'judged {report["coverage"]["items"]["elements_judged"]} elements across pages. The orchestrator '
                f'confirmed {decisions.get("confirmed", 0)} of their verdicts'
                + (f" and changed others ({changed})" if changed else "") + ".</footer>")

    def html(self):
        return (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f'<title>Site check of {escape(self.host)}, {self.started()}</title>\n<style>{CSS}</style>\n'
                f'</head>\n<body>\n{self.header()}\n<main>\n{self.summary()}\n{self.problems_section()}\n'
                f'{self.review_section()}\n{self.notices_section()}\n{self.overturned_section()}\n'
                f'{self.pages_section()}\n</main>\n{self.footer()}\n</body>\n</html>\n')

def report_page(report):
    return ReportPage(report).html()

def main():
    parser = argparse.ArgumentParser(description="Lay orchestrator reports out as pages, each saved next to its "
                                                 "report with .html instead of .json.")
    parser.add_argument("reports", nargs="+", type=Path, help="the orchestrator reports, as JSON")
    for path in parser.parse_args().reports:
        page = path.with_suffix(".html")
        page.write_text(report_page(json.loads(path.read_text())))
        print(f"=====Saved {page}=====")

if __name__ == "__main__":
    main()
