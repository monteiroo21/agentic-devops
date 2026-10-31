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
    // the part every page of the site shares: the page's own header, menus and footer. A header or footer inside
    // an article or a section belongs to that content instead
    const inLayout = el => {
        const landmark = el.closest("header, footer, nav, [role=banner], [role=contentinfo], [role=navigation]");
        return !!landmark && !landmark.parentElement.closest("article, aside, main, section");
    };
    const seen = [];  // the headings so far, in document order
    const scopes = [];  // the part of the page each heading in seen titles
    // a heading's parent, or further up while the parent holds nothing but the heading,
    // as in <header><h2>…</h2></header>
    const scopeOf = heading => {
        let scope = heading.parentElement;
        while (scope !== document.body && clean(scope.innerText) === clean(heading.innerText)) {
            scope = scope.parentElement;
        }
        return scope;
    };
    // the last heading up to seen[last] whose scope contains the node, so a card's heading
    // doesn't carry over to the next card or to the text after the cards
    const ownerOf = (node, last) => {
        for (let i = last; i >= 0; i--) {
            if (scopes[i].contains(node)) return i;
        }
        return -1;
    };
    const headingFor = el => {
        const i = ownerOf(el, seen.length - 1);
        return i < 0 ? null : clean(seen[i].innerText);
    };
    const headings = [], images = [], links = [];
    for (const el of document.querySelectorAll("h1, h2, h3, h4, h5, h6, img, a[href]")) {
        if (!visible(el)) continue;
        if (/^h\d$/.test(el.localName)) {
            seen.push(el);
            scopes.push(scopeOf(el));
            headings.push({selector: selector(el), level: Number(el.localName[1]), text: clean(el.innerText),
                           ...(inLayout(el) ? {layout: true} : {})});
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
    // the start of the text under each heading, to judge whether the heading describes it: each piece of text
    // goes to the same heading headingFor would give it, so it stops at the end of what the heading titles
    const sectionText = seen.map(() => "");
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let current = -1;  // the last heading before the node
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        while (current + 1 < seen.length
               && seen[current + 1].compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) current++;
        const parent = node.parentElement;
        if (current < 0 || seen[current].contains(node)
            || !visible(parent) || parent.closest("script, style, noscript, template")) continue;
        const owner = ownerOf(node, current);
        if (owner < 0 || sectionText[owner].length >= 200) continue;
        sectionText[owner] = clean(sectionText[owner] + " " + node.textContent);
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