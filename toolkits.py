import json
from urllib.parse import unquote, urldefrag, urlsplit
from pydantic_ai import Tool
from analyzers.links import leads_to_page, redirect_rule
from common import CONFIG, docstring_with, with_ids, without
from tools.axe import axe_selector, check_messages, wcag_tags
from tools.links import check_url
from tools.page_content import item_for_model
from tools.styles import computed_styles

FLEET = CONFIG.fleet
ACCESSIBILITY = CONFIG.accessibility
WEB = ("http", "https")
NEEDLESS_AFTER_FAIL = {"soft-404": ("redirect", "text-mismatch"), "redirect": ("text-mismatch",)}

def element_id(item_id, skill):
    return f"{skill}:{item_id}"

def first_words(text, count):
    words = (text or "").split()
    return " ".join(words[:count]) + (" …" if len(words) > count else "") if words else None

def filled(entry):
    return {key: value for key, value in entry.items() if value is not None}

def url_key(url):
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port, unquote(parts.path).rstrip("/"), parts.query

def same_page(url, other):
    first, second = urlsplit(url), urlsplit(other)
    return ((first.hostname, first.path.rstrip("/"), first.query)
            == (second.hostname, second.path.rstrip("/"), second.query))

class Toolkit:
    tools_given = ()  # the tools it gives, as the agent's file names them
    listing = ()  # the tools that list the items, which the agent must call before it hands in its findings
    checks = ("accessibility",)  # the analyzers it needs the page collected by
    batched = True  # whether its items are split into runs of at most FLEET.batch_size

    def __init__(self, work, skills):
        self.work = work
        self.skills = {skill.name: skill for skill in skills}  # the skills the task loads
        self.order = []  # every item id, in page order
        self.needs = {}  # by item id: the skills it still needs an answer for
        self.total = 0  # the (item, skill) pairs on the page, the ones answered on earlier pages included
        self.run = None
        self.batch = []  # the ids of the items the current run checks
        self.keep = None  # keeps the current run's findings: returns how many it kept, and the refused ones with why

    @property
    def content(self):
        return self.work.analyzers["accessibility"].content

    @property
    def axe(self):
        return self.work.analyzers["accessibility"].axe

    def unavailable(self):
        if self.work.lands_on:
            return "lands on another page", None, self.work.lands_on
        if self.content is None:
            return "failed", "the tools couldn't read the page", None
        return None

    def ask(self, skill, entries, page_only=False):
        if skill not in self.skills:
            return
        found = self.skills[skill]
        about = {"category": found.category, "wcag": found.wcag_tags, "help_url": found.help_url}
        elements = [(element_id(item_id, skill), shown, {**about, **kept}) for item_id, shown, kept in entries]
        for item_id, _, _ in entries:
            if item_id not in self.order:
                self.order.append(item_id)
        self.total += len(elements)
        for task in self.work.questions.prepare(skill, "", elements, batched=not page_only):
            for item in task.items:
                self.needs.setdefault(item["id"].split(":", 1)[1], []).append(skill)

    def prepare(self):
        # the items still to judge, in page order
        self.items()
        return [item_id for item_id in self.order if item_id in self.needs]

    def items(self):
        raise NotImplementedError  # calls ask() for each skill

    def left_out(self):
        return len([item_id for item_id in self.order if item_id not in self.needs])

    def start(self, run, batch, keep):
        self.run, self.batch, self.keep = run, batch, keep

    def uncalled(self):
        # the tools that list the items, which the run hasn't called yet
        return [tool for tool in self.listing if not self.run.tool_calls[tool]]

    def task_text(self):
        return ""  # what the task tells the agent besides the page, like the items it carries

    def page_error(self, url, tool):
        if not same_page(url, self.run.url):
            return {"error": f"you were asked to check {self.run.url}: call {tool} with that URL"}
        self.run.tool_calls[tool] += 1
        return None

    def looked_up(self, ids, tool, show):
        self.run.tool_calls[tool] += 1
        results = []
        for item_id in dict.fromkeys(ids):
            if item_id in self.batch:
                results.append({"id": item_id, **show(item_id)})
            else:
                results.append({"id": item_id, "error": "no item with this id was listed"})
        return results

    def refused(self, entry):
        return None  # why a finding on an item it was given isn't accepted, if it isn't

    def needless_after_fail(self, skill):
        return ()  # the skills an item needs no answer for once it fails this one

    def tools(self):
        raise NotImplementedError  # the tools its agent's file lists

    def record_tool(self, findings_type):
        # record_findings, which every agent has besides the tools its file lists, like submit_findings. It takes the
        # findings the agent's submission takes
        def record_findings(findings: findings_type):  # pyright: ignore[reportInvalidTypeForm]
            """Records findings as you go, a few items at a time, as soon as you have judged them. An item and skill
            that already has a finding keeps it. Returns how many findings it recorded, the ones it refused with why,
            which you can give again fixed, and the items still without a finding (unanswered), each with the skills
            it still needs. When nothing is unanswered, call submit_findings.

            Args:
                findings: The findings to record, each for an item and a skill that the tools listed.
            """
            uncalled = self.uncalled()
            if uncalled:
                return {"error": f"call {' and '.join(uncalled)} with {self.run.url} first: it lists the items to "
                                 "check"}
            self.run.tool_calls["record_findings"] += 1
            kept, refused = self.keep(findings)
            unanswered = {}
            for item_id, skill in self.run.unanswered():
                unanswered.setdefault(item_id, []).append(skill)
            return filled({"recorded": kept,
                           "refused": [{"id": item_id, "skill": skill, "why": why}
                                       for (item_id, skill), why in refused] or None,
                           "unanswered": unanswered})
        return Tool(record_findings)

class LinkTools(Toolkit):
    tools_given = ("list_links", "get_link_context", "get_destination_summary")
    listing = ("list_links",)
    checks = ("links", "accessibility")

    def __init__(self, work, skills):
        super().__init__(work, skills)
        self.links = {}  # by id: {"url", "text", "shown": what the browser shows of it, or None, "check"}
        self.read = set()  # the ids whose destination the agent read

    def unavailable(self):
        if not self.work.collected["links"] and (self.content is None or self.work.lands_on):
            return "failed", "the tools couldn't read the page", None
        return None

    def items(self):
        page_url = self.work.url
        checker = self.work.analyzers["links"].links if self.work.collected["links"] else []
        checks = {url_key(link["url"]): link["check"] for link in checker}
        shown = [] if self.content is None or self.work.lands_on else self.content["links"]
        links, seen = [], set()
        for link in shown:
            url = urldefrag(link["href"])[0]
            check = None
            if urlsplit(url).scheme in WEB and not same_page(url, page_url):
                check = checks.get(url_key(url)) or check_url(url)
            links.append({"url": url, "text": link["text"], "shown": link, "check": check})
            seen.add(url_key(url))
        links += [{"url": link["url"], "text": link["text"] or "", "shown": None, "check": link["check"]}
                  for link in checker if link["tag"] == "a" and url_key(link["url"]) not in seen]
        self.links = dict(with_ids(links, "link"))

        purpose, redirects, soft_404s, mismatches = [], [], [], []
        for item_id, link in self.links.items():
            url, check, shown = link["url"], link["check"], link["shown"]
            if shown is not None:
                purpose.append((item_id, {**item_for_model(without(shown, "selector", "href"), "text"), "url": url},
                                {"selector": shown["selector"], "url": url}))
            if check is None:
                continue
            if check["verdict"] == "redirect":
                redirects.append((item_id, {"url": url, "text": link["text"]},
                                  {"url": url, "links": [url], "rule": redirect_rule({"url": url, "check": check},
                                                                                     {url: check})}))
            if leads_to_page({"tag": "a", "check": check}):
                # a page is the same soft-404 question whichever link leads to it
                soft_404s.append((item_id, {"url": check["final_url"]},
                                  {"url": check["final_url"], "links": [url], "rule": "soft-404"}))
                if link["text"] and (check["title"] or check["heading"]):
                    mismatches.append((item_id, {"url": url, "text": link["text"]},
                                       {"url": url, "links": [url], "rule": "text-mismatch"}))
        self.ask("soft-404", soft_404s)
        self.ask("redirect", [entry for entry in redirects if not self.known_needless(entry[0], "redirect")])
        self.ask("text-mismatch", [entry for entry in mismatches if not self.known_needless(entry[0], "text-mismatch")])
        self.ask("link-purpose", purpose)

    def row(self, item_id):
        link, check = self.links[item_id], self.links[item_id]["check"]
        row = {"id": item_id, "text": link["text"]}
        if link["shown"] and link["shown"].get("aria_label"):
            row["aria_label"] = link["shown"]["aria_label"]  # screen readers read it instead of the text
        row["url"] = link["url"]
        if check is not None:
            if check["status"] is not None:
                row["status"] = check["status"]
            elif check["error"]:
                row["error"] = check["error"][:80]
            if check["redirects"]:
                row["redirects"] = len(check["redirects"])
            if check["final_url"] and check["final_url"] != link["url"]:
                row["final_url"] = check["final_url"]
        row["skills"] = self.needs[item_id]
        return row

    def context(self, item_id):
        link = self.links[item_id]
        shown = link["shown"]
        if shown is None:
            return {"text": link["text"], "error": "the browser doesn't show this link, so it has no context"}
        context = item_for_model({key: shown.get(key) for key in ("text", "aria_label", "title", "context", "heading")},
                                 "text")
        if len(context) == 1:
            context["note"] = "it has no title, no text around it and no heading"
        return context

    def destinations(self, ids):
        # each page these links lead to once, by the URL they ended on, with the ids and texts of the links to it
        pages, errors = {}, []
        for item_id in dict.fromkeys(ids):
            if item_id not in self.batch:
                errors.append({"id": item_id, "error": "no item with this id was listed"})
                continue
            link = self.links[item_id]
            check = link["check"]
            if check is None or not leads_to_page({"tag": "a", "check": check}):
                errors.append({"id": item_id, "text": link["text"], "error": "it doesn't lead to a web page that works"})
                continue
            if item_id not in self.read:
                self.read.add(item_id)
                self.run.read.append(item_id)
            page = pages.setdefault(check["final_url"], {
                "url": check["final_url"],
                **filled({key: check[key] for key in ("status", "title", "heading", "words", "word_count")}),
                "links": []})
            page["links"].append({"id": item_id, "text": link["text"]})
        return filled({"pages": list(pages.values()) or None, "errors": errors or None})

    def refused(self, entry):
        if entry.skill in ("soft-404", "text-mismatch") and entry.verdict != "unsure" and entry.item_id not in self.read:
            return ("you haven't read the page it leads to: read it with get_destination_summary, or answer "
                    "unsure")
        return None

    def needless_after_fail(self, skill):
        return NEEDLESS_AFTER_FAIL.get(skill, ())

    def known_needless(self, item_id, skill):
        by_id = self.work.questions.answers.by_id
        return any(skill in later and getattr(by_id.get(element_id(item_id, earlier)), "verdict", None) == "fail"
                   for earlier, later in NEEDLESS_AFTER_FAIL.items())

    def tools(self):
        def list_links(url: str):
            """Lists the links on the page to check, each with an id, its text, its aria_label if it has one, its
            URL, the status its URL answered with, how many redirects it took and the URL it ended on, if it moved,
            and the skills it needs an answer for. A link with no status couldn't be requested, or isn't a web
            address, like an email link. Links already judged on other pages of the site are left out.

            Args:
                url: The URL of the page you were asked to check.
            """
            error = self.page_error(url, "list_links")
            if error:
                return error
            return filled({"page": self.run.url, "links": [self.row(item_id) for item_id in self.batch],
                           "already_judged_elsewhere": self.left_out() or None, "part": self.run.batch})

        def get_link_context(ids: list[str]):
            """Returns the context of the links with these ids, as list_links listed them: each one's text and
            aria_label again, its title attribute, the text around it (context: the sentence, paragraph, list item or
            table cell it is in) and the heading of its section. Fields a link doesn't have are left out.

            Args:
                ids: The ids of the links, like link-3.
            """
            return {"links": self.looked_up(ids, "get_link_context", self.context)}

        def get_destination_summary(ids: list[str]):
            """Reads the pages that the links with these ids lead to, as list_links listed them. Returns each page
            once, however many of the links lead to it: the URL the links ended on, its status, title, first heading,
            first words (words) and how many words its main text has (word_count), and the ids and texts of the links
            that lead to it.

            Args:
                ids: The ids of the links, like link-3.
            """
            self.run.tool_calls["get_destination_summary"] += 1
            return self.destinations(ids)

        return [Tool(list_links), Tool(get_link_context), Tool(get_destination_summary)]

class PageTools(Toolkit):
    tools_given = ("get_page_meta", "get_headings", "get_text_sample")
    batched = False

    @property
    def listing(self):
        return ((("get_page_meta",) if {"page-title", "page-language"} & set(self.skills) else ())
                + (("get_headings",) if "heading" in self.skills else ()))

    def items(self):
        content = self.content
        self.ask("page-title", [("title", {"title": content["title"]}, {"selector": None})], page_only=True)
        self.ask("page-language",
                 [("lang", filled({"lang": content["lang"]}), {"selector": None})]
                 + [(item_id, without(part, "selector"), {"selector": part["selector"]})
                    for item_id, part in with_ids(content["parts"], "part")], page_only=True)
        # the headings every page shares, like the footer's, are judged once for the site, with their outline
        headings = list(with_ids(content["headings"], "h"))
        outline = [f"h{heading['level']} {heading['text']}" for _, heading in headings if heading.get("layout")]
        self.ask("heading", [(item_id, item_for_model(without(heading, "selector"), "text"),
                              {"selector": heading["selector"]})
                             for item_id, heading in headings if not heading.get("layout")], page_only=True)
        self.ask("heading", [(item_id, {**item_for_model(without(heading, "selector", "layout"), "text"),
                                        "outline": outline}, {"selector": heading["selector"]})
                             for item_id, heading in headings if heading.get("layout")])
        self.headings = headings

    def tools(self):
        content, words = self.content, FLEET.page_text_words

        def get_page_meta(url: str):
            """Returns the page's items for the page-title and page-language skills, each with an id and its skill:
            its title (id title), its lang attribute (id lang: with no lang field, the page has no lang attribute),
            and the parts of the page that have a lang of their own, each with its lang and the start of its text
            (ids like part-1).

            Args:
                url: The URL of the page you were asked to check.
            """
            error = self.page_error(url, "get_page_meta")
            if error:
                return error
            items = []
            if "page-title" in self.skills:
                items.append({"id": "title", "skill": "page-title", "title": content["title"]})
            if "page-language" in self.skills:
                items.append(filled({"id": "lang", "skill": "page-language", "lang": content["lang"]}))
                items += [{"id": item_id, "skill": "page-language", "lang": part["lang"],
                           "text": first_words(part["text"], FLEET.part_words)}
                          for item_id, part in with_ids(content["parts"], "part")]
            return {"page": self.run.url, "items": items}

        def get_headings(url: str):
            """Returns the page's headings in page order, each with an id, its level, its text and the first words
            of the text under it (section_text). A heading marked shared is in the part every page of the site
            shares, like its header or footer; shared headings already judged on other pages are left out.

            Args:
                url: The URL of the page you were asked to check.
            """
            error = self.page_error(url, "get_headings")
            if error:
                return error
            judged = "heading" in self.skills
            rows = []
            for item_id, heading in self.headings:
                shared = bool(heading.get("layout"))
                if shared and (not judged or item_id not in self.needs):
                    continue
                rows.append(filled({"id": item_id if judged else None, "level": heading["level"],
                                    "text": heading["text"],
                                    "section_text": first_words(heading.get("section_text"), FLEET.section_words),
                                    "shared": True if shared else None}))
            return {"page": self.run.url, "headings": rows}

        @docstring_with(words=words)
        def get_text_sample(url: str, from_word: int = 0):
            """Returns {words} words of the page's main text, starting at word from_word, with how many words the
            text has in all (word_count) and, if there is more, the word to start the next call at (next_word).

            Args:
                url: The URL of the page you were asked to check.
                from_word: The word to start at: 0 for the start of the text, or next_word from an earlier call.
            """
            error = self.page_error(url, "get_text_sample")
            if error:
                return error
            text = content["text"].split()
            start = max(from_word, 0)
            return filled({"from_word": start, "text": " ".join(text[start:start + words]),
                           "word_count": len(text), "next_word": start + words if start + words < len(text) else None})

        return [Tool(get_page_meta), Tool(get_headings), Tool(get_text_sample)]

class ImageTools(Toolkit):
    tools_given = ("list_images", "get_image_context")
    listing = ("list_images",)

    def items(self):
        self.images = dict(with_ids(self.content["images"], "img"))
        self.ask("alt-text", [(item_id, item_for_model(without(image, "selector"), "link_text"),
                               {"selector": image["selector"]})
                              for item_id, image in self.images.items()])

    def row(self, item_id):
        image = self.images[item_id]
        src = image["src"] or ""
        filename = "a data: URI" if src.startswith("data:") else urlsplit(src).path.rsplit("/", 1)[-1] or src or None
        return filled({"id": item_id, "filename": filename, "alt": image["alt"],
                       "in_link": image["link_text"] is not None})

    def context(self, item_id):
        # the image's file name and alt again, as list_images showed them, so its verdict can be judged from this row
        image = self.images[item_id]
        context = filled({key: image.get(key) for key in ("caption", "context", "heading", "link_text", "link_url")})
        if "context" in context and context["context"] in (image.get("link_text"), image.get("caption")):
            context.pop("context")  # the same text twice
        if not context:
            context["note"] = "it has no caption, no text around it and no heading, and isn't in a link"
        listed = self.row(item_id)
        listed.pop("id")
        listed.pop("in_link")
        return {**listed, **context}

    def tools(self):
        def list_images(url: str):
            """Lists the images on the page to check, each with an id, its file name, its alt text and whether it
            is in a link (in_link). An image with no alt field has no alt attribute at all, while one with an empty
            alt has alt="". Images already judged on other pages of the site are left out.

            Args:
                url: The URL of the page you were asked to check.
            """
            error = self.page_error(url, "list_images")
            if error:
                return error
            return filled({"page": self.run.url, "images": [self.row(item_id) for item_id in self.batch],
                           "already_judged_elsewhere": self.left_out() or None, "part": self.run.batch})

        def get_image_context(ids: list[str]):
            """Returns the context of the images with these ids, as list_images listed them: each one's file name and
            alt again (with no alt field, the image has no alt attribute), its caption, the text around it (context),
            the heading of its section and, for an image in a link, the link's text (link_text: empty when the image
            is all there is in the link) and the URL it leads to (link_url). Fields an image doesn't have are left
            out.

            Args:
                ids: The ids of the images, like img-3.
            """
            return {"images": self.looked_up(ids, "get_image_context", self.context)}

        return [Tool(list_images), Tool(get_image_context)]

class AxeReviewTools(Toolkit):
    tools_given = ("get_rule_help", "get_element_html", "get_computed_styles")

    def unavailable(self):
        if self.work.lands_on:
            return "lands on another page", None, self.work.lands_on
        if self.axe is None:
            return "failed", "axe-core couldn't run on the page", None
        return None

    def items(self):
        html_chars = ACCESSIBILITY.max_html_chars
        self.nodes = dict(with_ids([(rule, node) for rule in self.axe["incomplete"] for node in rule["nodes"]],
                                   "review"))
        # the rule's own WCAG criteria and help page, from axe-core, rather than the skill's
        self.ask("axe-needs-review", [
            (item_id, {"rule": rule["id"], "html": node["html"][:html_chars], "problems": check_messages(node)},
             {"rule": rule["id"], "wcag": wcag_tags(rule), "selector": axe_selector(node),
              "html": node["html"][:html_chars], "summary": node.get("failureSummary"), "help": rule["help"],
              "help_url": rule.get("helpUrl")})
            for item_id, (rule, node) in self.nodes.items()])

    def task_text(self):
        items = [{"id": item_id, "rule": self.nodes[item_id][0]["id"], "selector": axe_selector(self.nodes[item_id][1]),
                  "message": " ".join(check_messages(self.nodes[item_id][1]))} for item_id in self.batch]
        return ("\n\nThe items axe-core couldn't decide:\n"
                + "\n".join(json.dumps(item, ensure_ascii=False) for item in items))

    def tools(self):
        rules = {rule["id"]: rule for rule in self.axe["incomplete"]}

        def get_rule_help(rule: str):
            """Returns axe-core's explanation of a rule, as the task names it: what the rule checks, and why it
            matters.

            Args:
                rule: The rule's id, like color-contrast.
            """
            self.run.tool_calls["get_rule_help"] += 1
            if rule not in rules:
                return {"error": f"no item in the task has the rule {rule!r}; their rules are {', '.join(rules)}"}
            return {"rule": rule, "help": rules[rule]["help"], "description": rules[rule]["description"]}

        def get_element_html(ids: list[str]):
            """Returns the HTML of the elements with these ids, as the task lists them.

            Args:
                ids: The ids of the items, like review-3.
            """
            return {"elements": self.looked_up(ids, "get_element_html", lambda item_id: {
                "html": self.nodes[item_id][1]["html"][:ACCESSIBILITY.max_html_chars]})}

        async def get_computed_styles(ids: list[str]):
            """Returns how the elements with these ids, as the task lists them, are shown on the page: their text
            colour, the background colour behind them and whether there is a background image behind them, their
            font size and weight, opacity, visibility and display, and their width and height in pixels.

            Args:
                ids: The ids of the items, like review-3.
            """
            wanted = [item_id for item_id in dict.fromkeys(ids) if item_id in self.batch]
            selectors = {item_id: axe_selector(self.nodes[item_id][1]) for item_id in wanted}
            found = await computed_styles(self.run.url, list(dict.fromkeys(selectors.values()))) if wanted else None

            def styles(item_id):
                if found["styles"] is None:
                    return {"error": f"the page couldn't be read: {found['error'] or 'no styles came back'}"}
                return found["styles"].get(selectors[item_id]) or {"error": "nothing on the page matches its selector"}
            return {"elements": self.looked_up(ids, "get_computed_styles", styles)}

        return [Tool(get_rule_help), Tool(get_element_html), Tool(get_computed_styles)]

TOOLKITS = {"link-agent": LinkTools, "page-agent": PageTools, "image-agent": ImageTools, "axe-review": AxeReviewTools}
