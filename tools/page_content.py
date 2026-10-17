from pathlib import Path
from common import CONFIG
from tools.browser import page_fields, visit

ACCESSIBILITY = CONFIG.accessibility
# Runs in the page: what a reader sees, for the judgements axe can't make
PAGE_CONTENT_JS = (Path(__file__).parent / "page_content.js").read_text()

GET_PAGE_CONTENT = ("Loads a web page in a headless browser and returns what a reader sees: its title, the lang "
                    "attribute of the page, the start of its visible main text, its headings in order with the start "
                    "of the text under each, its visible images and links, and the parts of the page that have their "
                    "own lang attribute, with their text. Each image comes with its selector, src, alt text, the text "
                    "of the link it is in and the text around it; each link with its selector, text, URL, aria-label "
                    "and title attributes and the text around it. Both also give the heading of the section they are "
                    "in. A passage in another language that is not listed in parts has no lang attribute of its own. "
                    f"Up to {ACCESSIBILITY.max_page_items} images, links and parts are listed. Empty fields are left "
                    "out: an image with no alt attribute has no alt field, while a decorative image has an empty one. "
                    "The final URL is only given when the page redirected, and an error only when the page could not "
                    "be loaded.")

def get_page_content(url):
    return visit(url, "content", lambda page: page.evaluate(PAGE_CONTENT_JS))

def item_for_model(item, own_text):
    kept = {key: value for key, value in item.items() if value is not None}
    if kept.get("context") == item.get(own_text):
        kept.pop("context", None)
    return kept

def content_for_model(tool_result):
    kept = page_fields(tool_result, "content")
    content = tool_result["content"]
    if content is None:
        return kept
    text = content["text"]
    max_chars, max_items = ACCESSIBILITY.max_text_chars, ACCESSIBILITY.max_page_items
    return {
        **kept,
        **{key: content[key] for key in ("title", "lang") if content[key] is not None},
        "text": text[:max_chars],
        **({"truncated": True} if len(text) > max_chars else {}),
        "headings": [item_for_model(heading, "text") for heading in content["headings"]],
        "images_total": len(content["images"]),
        "images": [item_for_model(image, "link_text") for image in content["images"][:max_items]],
        "links_total": len(content["links"]),
        "links": [item_for_model(link, "text") for link in content["links"][:max_items]],
        "parts_total": len(content["parts"]),
        "parts": content["parts"][:max_items],
    }
