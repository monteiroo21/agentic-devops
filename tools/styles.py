from tools.browser import visit

# Runs in the page: each element's computed colours, font, size and visibility. An element's own background is often
# transparent, so the colour behind it is the first background up its ancestors that isn't, and a background image on
# the way there means the colour behind the text can't be known from styles alone
STYLES_JS = """selectors => selectors.map(selector => {
    let element;
    try { element = document.querySelector(selector); } catch { return null; }
    if (!element) return null;
    const style = getComputedStyle(element);
    let background = null, backgroundImage = false;
    for (let node = element; node; node = node.parentElement) {
        const own = getComputedStyle(node);
        if (own.backgroundImage !== "none") backgroundImage = true;
        if (own.backgroundColor !== "rgba(0, 0, 0, 0)" && own.backgroundColor !== "transparent") {
            background = own.backgroundColor;
            break;
        }
    }
    const box = element.getBoundingClientRect();
    return {
        color: style.color, background_color: background, background_image: backgroundImage,
        font_size: style.fontSize, font_weight: style.fontWeight, opacity: style.opacity,
        visibility: style.visibility, display: style.display,
        width: Math.round(box.width), height: Math.round(box.height),
    };
})"""

async def computed_styles(url, selectors):
    # one browser visit for all the selectors: by selector, its styles, or None if nothing on the page matches it
    async def collect(page):
        return await page.evaluate(STYLES_JS, list(selectors))
    result = await visit(url, "styles", collect)
    styles = result["styles"]
    return {"error": result["error"],
            "styles": dict(zip(selectors, styles)) if styles is not None else None}
