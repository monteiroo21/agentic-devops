---
name: link-purpose
description: Whether a user can tell where each link goes, from its text alone or from its text together with the
  text around it.
category: accessibility
wcag: "2.4.4"
help_url: "https://www.w3.org/WAI/WCAG22/Understanding/link-purpose-in-context.html"
tools: [list_links, get_link_context]
---

### When to look closer

list_links gives each link's text, and its aria_label if it has one. A link whose text or aria_label names where it
goes passes on its own, without its context. Call get_link_context for the others: links whose text is vague, like
"read more", "here" or "details", links with no text, and links that share their text with another link that has a
different URL. It returns the link's text and aria_label again, with its title, the text around it (context) and
the heading of its section.

### How to judge

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
the destination matches the text: that is the text-mismatch skill's job.

Also look out for links with the same text that lead to different URLs. A user can't see the URLs before following
the links. They fail if their context and headings don't tell them apart, and pass if each one's context or heading
says which is which.

### Impact

- critical: screen reader users can't tell where a link they need goes, like a link to the booking or checkout page
  whose aria_label says "link";
- serious: they can't tell where a link to main content goes, like a "Learn more" with nothing around it that says
  what about;
- moderate: links with the same text are hard to tell apart, though the page's layout gives a hint;
- minor: they would hardly notice, like a vague link to a page few people look for.
Fixes look like "name the page in the link's text, like 'Read more about the early closing'" or "remove the
aria-label, or make it say where the link goes".
