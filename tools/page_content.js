() => {
    const clean = s => (s || "").replace(/\s+/g, " ").trim();
    const visible = el => el.getClientRects().length > 0;
    const selector = el => {
        const parts = [];
        for (; el && el !== document.documentElement; el = el.parentElement) {
            if (el.id) { parts.unshift("#" + CSS.escape(el.id)); break; }
            const sameTag = [...el.parentElement.children].filter(c => c.localName === el.localName);
            parts.unshift(sameTag.length > 1 ? `${el.localName}:nth-of-type(${sameTag.indexOf(el) + 1})` : el.localName);
        }
        return parts.join(" > ");
    };
    // the sentence-level context WCAG 2.4.4 allows: the paragraph, list item or table cell around the element
    const context = el => {
        const block = el.parentElement.closest("p, li, td, th, dd, figure, blockquote");
        return block ? clean(block.innerText).slice(0, 300) : null;
    };
    const seen = [];  // the headings so far, in document order
    // the last heading before the element whose parent also contains it, so a card's
    // heading doesn't carry over to the next card
    const headingFor = el => {
        for (let i = seen.length - 1; i >= 0; i--) {
            if (seen[i].parentElement.contains(el)) return clean(seen[i].innerText);
        }
        return null;
    };
    const headings = [], images = [], links = [];
    for (const el of document.querySelectorAll("h1, h2, h3, h4, h5, h6, img, a[href]")) {
        if (!visible(el)) continue;
        if (/^h\d$/.test(el.localName)) {
            seen.push(el);
            headings.push({selector: selector(el), level: Number(el.localName[1]), text: clean(el.innerText)});
        } else if (el.localName === "img") {
            const link = el.closest("a[href]");
            images.push({selector: selector(el), src: el.getAttribute("src"), alt: el.getAttribute("alt"),
                         link_text: link ? clean(link.innerText) : null, context: context(el),
                         heading: headingFor(el)});
        } else {
            links.push({selector: selector(el), text: clean(el.innerText), href: el.href,
                        aria_label: el.getAttribute("aria-label"), title: el.getAttribute("title"),
                        context: context(el), heading: headingFor(el)});
        }
    }
    // the start of the text under each heading, up to the next one, to judge whether the heading describes it
    const sectionText = seen.map(() => "");
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let current = -1;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        while (current + 1 < seen.length
               && seen[current + 1].compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) current++;
        const parent = node.parentElement;
        if (current < 0 || seen[current].contains(node) || sectionText[current].length >= 200
            || !visible(parent) || parent.closest("script, style, noscript, template")) continue;
        sectionText[current] = clean(sectionText[current] + " " + node.textContent);
    }
    headings.forEach((heading, i) => { heading.section_text = sectionText[i].slice(0, 200) || null; });
    // passages marked with their own language (WCAG 3.1.2); plain text alone can't show whether a passage is marked
    const parts = [...document.body.querySelectorAll("[lang]")].filter(visible).map(el => (
        {selector: selector(el), lang: el.getAttribute("lang"), text: clean(el.innerText).slice(0, 300)}));
    return {
        title: document.title,
        lang: document.documentElement.getAttribute("lang"),
        text: clean((document.querySelector("main") || document.body).innerText),
        headings, images, links, parts,
    };
}