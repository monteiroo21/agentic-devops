---
name: page-title
description: Whether the page's title says what the page is about.
category: accessibility
wcag: "2.4.2"
help_url: "https://www.w3.org/WAI/WCAG22/Understanding/page-titled.html"
tools: [get_page_meta, get_headings, get_text_sample]
---

### When to look closer

get_page_meta gives the page's title. Compare it with the page's first headings from get_headings. If they don't
show what the page is about, read the start of its text with get_text_sample.

### How to judge

Does the page title say what the page is about?
Compare it with the page's first headings and the start of its text. A good title names the page's own topic, often
followed by the site's name, like "Opening hours | City Library". It doesn't have to repeat the main heading word
for word.

It fails when:
- it is missing, empty or a placeholder left by an editor or a template, like "New page" or "{{ title }}";
- it is a file name or an address, like "index.html" or "www.example.com";
- it names a different page, like a title copied from another page of the site;
- it is only the site's name, on a page about one thing, like a product, an article or an event. On the home page,
  where the headings and text are about the whole site, the site's name alone is enough.

### Impact

- critical: visitors can't tell the page apart from others at all, like every page of a checkout titled the same
  placeholder;
- serious: the title is missing, a placeholder or names another page, so screen reader users, and anyone with many
  tabs open, can't tell what the page is;
- moderate: the title is only the site's name, on a page about one thing;
- minor: the title is right but hard to scan, like a file name next to the page's topic.
Fixes look like "title it 'Opening hours | City Library'".
