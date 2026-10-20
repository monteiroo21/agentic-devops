from pathlib import Path
from common import CONFIG
from tools.browser import page_fields, visit

ACCESSIBILITY = CONFIG.accessibility
# Runs in the page: what a reader sees, for the judgements axe can't make
PAGE_CONTENT_JS = (Path(__file__).parent / "page_content.js").read_text()

async def content_on(page):
    return await page.evaluate(PAGE_CONTENT_JS)

async def get_page_content(url):
    return await visit(url, "content", content_on)

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
