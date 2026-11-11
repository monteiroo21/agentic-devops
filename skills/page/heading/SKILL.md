---
name: heading
description: Whether each heading describes the section under it, and whether the heading levels show how the page is
  organised.
category: accessibility
wcag: ["1.3.1", "2.4.6"]
help_url: "https://www.w3.org/WAI/WCAG22/Understanding/headings-and-labels.html"
tools: [get_headings]
---

### When to look closer

get_headings returns every heading in page order, each with its level and the first words of the text under it
(section_text), which is all this skill needs: compare each heading with its section_text and with the headings
around it. A heading marked shared is in the part every page of the site shares, like its header or footer: judge
its level against the other shared headings, not the page's own.

### How to judge

Does each heading describe the content under it, and does its level fit where it sits in the page's outline?
Judge each one against the text under it and against the headings around it.

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

### Impact

- critical: screen reader users can't find their way around the page at all, like a page whose every section heading
  names the wrong thing;
- serious: a heading names a different topic than its section, so users who jump from heading to heading are
  misled, like a heading copied from another section;
- moderate: text that isn't a heading is marked up as one, like small print, or a level puts a section in the wrong
  place in the outline;
- minor: a heading says little about a short section, like "Section 2" over a few lines.
Fixes look like "rename the heading to name its section", "mark the small print up as a paragraph" or "make the h4
an h3".
