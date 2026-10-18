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
A link's text is everything inside it, so a link around a card can include a name and a price. Most links are clear
from their own text alone, like a menu link that names a section or a link that names a topic: those need no
context. Only when the text alone is vague, like "click here" or "read more", do its context and heading matter, and
then the text around the link counts as explaining it. Judge only whether a user can tell where the link goes, not
whether the topic is explained there, and not whether the destination matches the text: that is the link checker's
job.

Also look out for links with the same text that lead to different places. They fail if their context and headings
don't tell them apart.
