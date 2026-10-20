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
A lang is a language code, like "en", "pt" or "pt-PT", where the part after the dash is a region. The page fails if
it has no lang field, which means it has no lang attribute, or if its lang is not the language most of its text is
in.

The parts are the passages that have their own lang attribute. For each part, check that its lang is the language
of its text. A passage in another language that isn't one of the parts has no lang of its own, so read the page's
text for sentences in another language. If one of them isn't the text of a part, that is a problem with the page:
fail it and say which passage it is.

Names of people, places, organisations and products don't need a lang of their own, and neither do technical terms
or words the page's language has taken in, like "café" or "croissant" in English. A sentence or a quote in another
language does. If a text is too short to tell its language, it can't be decided.
