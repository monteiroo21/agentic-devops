---
name: page-title
description: Judge whether the page title says what the page is about (WCAG 2.4.2). Use it on the title returned by
  get_page_content.
metadata:
  wcag: "wcag242"
  needs-selector: "false"
---

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
