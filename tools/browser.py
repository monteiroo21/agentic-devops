from collections.abc import Callable
from dataclasses import dataclass
from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai_harness.playwright import PlaywrightBrowserSession
from common import CONFIG

BROWSER = CONFIG.browser
SUBMIT_GUARD_JS = 'window.addEventListener("submit", event => event.preventDefault());'
ANIMATIONS_DONE_JS = """() => document.getAnimations().every(animation =>
    animation.playState !== "running" || animation.effect?.getComputedTiming().iterations === Infinity)"""
DESCRIBE_JS = 'element => element.outerHTML.replace(/\\s+/g, " ").slice(0, 150)'
READ_ONLY = {"snapshot", "screenshot", "wait_for"}

async def block_writes(route):
    if route.request.method in ("GET", "HEAD"):
        await route.fallback()
    else:
        await route.abort()

async def guard(page):
    # on the context, so a tab the page opens is guarded too
    await page.context.route("**/*", block_writes)
    await page.context.add_init_script(SUBMIT_GUARD_JS)

async def settle(page):
    try:
        await page.wait_for_function(ANIMATIONS_DONE_JS, timeout=BROWSER.settle_timeout_ms)
    except PlaywrightTimeout:
        pass  # check the page as it is

class GuardedSession(PlaywrightBrowserSession):
    # the agent's browser, guarded as soon as its page opens, before any tool can act on it
    async def __aenter__(self):
        self.guarded = False
        return await super().__aenter__()

    async def ensure_page(self):
        page = await super().ensure_page()
        if not self.guarded:
            await guard(page)
            self.guarded = True
        return page

async def describe_element(page, selector):
    if page is None or not selector:
        return selector
    try:
        return await page.locator(selector).first.evaluate(DESCRIBE_JS, timeout=1000)
    except Exception:
        return selector  # pixel coordinates, or nothing matches

@dataclass
class ActionLog(WrapperToolset):
    session: PlaywrightBrowserSession = None
    on_action: Callable = None

    async def call_tool(self, name, tool_args, ctx, tool):
        if name in READ_ONLY:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        target = await describe_element(self.session.page, tool_args.get("selector"))
        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        if not (isinstance(result, str) and result.startswith("Error")):
            details = ", ".join(f"{key}={value!r}" for key, value in tool_args.items()
                                if key not in ("selector", "timeout_ms"))
            self.on_action(" ".join(part for part in (name, target, details and f"({details})") if part))
        return result

async def visit(url, key, collect):
    result = {"url": url, "final_url": None, "status": None, "error": None, key: None}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            response = await page.goto(url, wait_until="networkidle")
            result[key] = await collect(page)
            result.update(final_url=page.url, status=response.status if response else None)
            await browser.close()
    except Exception as e:
        message = str(e)
        result["error"] = message.splitlines()[0] if message else type(e).__name__
    return result

async def screenshot(url, selector=None, margin=BROWSER.screenshot_margin_px):
    # the element with a little of the page around it, or the top of the page when there is no element
    async def shoot(page):
        await settle(page)
        if not selector:
            return await page.screenshot()
        element = page.locator(selector).first
        await element.scroll_into_view_if_needed(timeout=BROWSER.settle_timeout_ms)
        box = await element.bounding_box()
        if box is None:
            raise ValueError(f"the element {selector} isn't visible")
        viewport = page.viewport_size
        x, y = max(box["x"] - margin, 0), max(box["y"] - margin, 0)
        return await page.screenshot(clip={"x": x, "y": y,
                                           "width": min(box["width"] + 2 * margin, viewport["width"] - x),
                                           "height": min(box["height"] + 2 * margin, viewport["height"] - y)})
    return await visit(url, "png", shoot)

def page_fields(tool_result, data_key):
    kept = {key: value for key, value in tool_result.items() if key != data_key and value is not None}
    if kept.get("final_url") == tool_result["url"]:
        kept.pop("final_url")
    return kept
