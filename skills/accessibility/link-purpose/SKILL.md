---
name: link-purpose
description: Judge whether a user can tell where each link goes, from its text alone or from its text together with
  the text around it (WCAG 2.4.4). Use it on the links returned by get_page_content.
metadata:
  wcag: "wcag244"
  elements: "links"
  needs-selector: "true"
---

Can a user tell where each link goes?
A link's text is everything inside it, so a link around a card can include a name and a price. If the link has an
aria_label, screen readers read it instead of the text, so judge the aria_label: one that says nothing, like "link"
or "click", fails even when the visible text is clear. The title only counts when the link has no text and no
aria_label.

Most links are clear from their own text alone, like a menu link that names a section or a link that names a topic:
those pass, whatever the text around them is about. Only when the text alone is vague, like "click here", "read
more" or "details", do its context and heading matter. The context is the sentence, paragraph, list item or table
cell the link is in, and the heading is the heading of its section. A vague link passes if they say where it goes,
and fails if they don't.

A link with no text may hold only an image, and then its name is the image's alt, which isn't part of the link's
text. Judge it by that alt if you have it, and if you don't, it can't be decided.

Judge only whether a user can tell where the link goes, not whether the topic is explained there, and not whether
the destination matches the text: that is the link checker's job.

Also look out for links with the same text that lead to different places. Their destinations are given so you know
they differ, but a user can't see them before following the links. They fail if their context and headings don't
tell them apart, and pass if each one's context or heading says which is which.
