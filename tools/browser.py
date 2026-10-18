from playwright.sync_api import sync_playwright

def visit(url, key, collect):
    result = {"url": url, "final_url": None, "status": None, "error": None, key: None}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            response = page.goto(url, wait_until="networkidle")
            result[key] = collect(page)
            result.update(final_url=page.url, status=response.status if response else None)
            browser.close()
    except Exception as e:
        message = str(e)
        result["error"] = message.splitlines()[0] if message else type(e).__name__
    return result

def page_fields(tool_result, data_key):
    kept = {key: value for key, value in tool_result.items() if key != data_key and value is not None}
    if kept.get("final_url") == tool_result["url"]:
        kept.pop("final_url")
    return kept
