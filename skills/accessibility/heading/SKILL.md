---
name: heading
description: Judge whether each heading describes the section under it, and whether the heading levels show how the
  page is organised (WCAG 1.3.1 and 2.4.6). Use it on the headings returned by get_page_content.
metadata:
  wcag: "wcag131 wcag246"
  elements: "headings"
  needs-selector: "false"
---

Does each heading describe the content under it, and does its level fit where it sits in the page's outline?
The headings come in page order, each with its level and the start of the text under it (section_text). Judge each
one against that text and against the headings around it.

It fails when:
- it names a different topic than the text under it, like a heading copied from another section;
- it says nothing about its section, like "Section 2", "Title" or "Lorem ipsum";
- it isn't a heading at all: a sentence, a note, small print or a price marked up as a heading to change how it
  looks. Headings name sections, they don't hold the content itself;
- its level puts it in the wrong place in the outline, like the subsections of a section given a higher level than
  the section itself.
It passes when:
- it names what its section is about, even briefly or in general words, like "Help" over links to the help pages,
  or an organisation's name over links about it, like About and Careers;
- it heads a card or a list item and names it, even if all that is under it is a price, a date or a link;
- it has no text of its own under it but is followed by lower-level headings: those subheadings are its content.
Skipped heading levels alone, like an h2 followed by an h4, aren't a WCAG failure, and neither is a page with no h1.
You only see the text that is marked up as headings, so don't guess about other text that might be a heading.
