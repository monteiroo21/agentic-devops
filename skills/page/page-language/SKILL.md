---
name: page-language
description: Whether the page's lang attribute matches the language of its text, and whether every passage in another
  language has a lang of its own.
category: accessibility
wcag: ["3.1.1", "3.1.2"]
help_url: "https://www.w3.org/WAI/WCAG22/Understanding/language-of-page.html"
tools: [get_page_meta, get_text_sample]
---

### When to look closer

get_page_meta gives the page's lang, as the item with the id "lang", and the parts of the page that have a lang of
their own, each with an id like part-1. Read the page's text with get_text_sample to see what language it is in and
to look for passages in another language. It returns the text a piece at a time: call it again with from_word to
read further down the page, where quotes and reviews often are.

### How to judge

Does the page's lang attribute match the language of its text, and does every passage in another language have a
lang of its own?
A lang is a language code, like "en", "pt" or "pt-PT", where the part after the dash is a region. The page fails if
it has no lang field, which means it has no lang attribute, or if its lang is not the language most of its text is
in.

The parts are the passages that have their own lang attribute. For each part, check that its lang is the language
of its text. A passage in another language that isn't one of the parts has no lang of its own, so read the page's
text for sentences in another language. If one of them isn't the text of a part, that is a problem with the page:
fail the "lang" item and say which passage it is.

Names of people, places, organisations and products don't need a lang of their own, and neither do technical terms
or words the page's language has taken in, like "café" or "croissant" in English. A sentence or a quote in another
language does. If a text is too short to tell its language, it can't be decided.

### Impact

- critical: screen readers can't read the page at all, like a page whose lang names a language with a different
  script;
- serious: the page has no lang, or the wrong one, so screen readers read all of it with the wrong pronunciation;
- moderate: a passage in another language has no lang of its own, or the wrong one, so that passage is read with the
  wrong pronunciation;
- minor: a short phrase is read with the wrong pronunciation, though it is still understandable.
Fixes look like 'set the page's lang to "en"' or 'add lang="pt" to the review's element'.
