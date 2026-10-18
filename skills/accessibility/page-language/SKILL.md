---
name: page-language
description: Judge whether the page's lang attribute matches the language of its text, and whether every passage in
  another language has a lang of its own (WCAG 3.1.1 and 3.1.2). Use it on the lang, text and parts returned by
  get_page_content.
metadata:
  wcag: "wcag311 wcag312"
  elements: "parts"
  needs-selector: "false"
---

Does the page's lang attribute match the language of its text, and does every passage in another language have a
lang of its own?
The parts are the passages that have their own lang attribute, so a passage in another language that isn't listed
as one of the parts has none. For each part, check that its lang matches the language of its text. Names of people
and places don't need their own lang. A page or part with no lang field has no lang attribute.
