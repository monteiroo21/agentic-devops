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
The headings come in page order, each with the start of the text under it (section_text). A heading followed by
lower-level headings needs no text of its own under it: those subheadings are its content. A heading over a list of
links passes if it names what the links have in common. Skipped heading levels alone aren't a WCAG failure.
