---
name: text-mismatch
description: Whether each link leads to what its text says, by comparing its text with its URL and the title and
  heading of the page it leads to.
category: links
wcag: null
tools: [list_links, get_destination_summary]
---

### When to look closer

Only the destination's title and heading show what the page a link leads to is about. Read the destination of every
link you answer text-mismatch for with get_destination_summary. You can only pass or fail a link whose destination
you read; if you didn't read it, the answer is unsure.

### How to judge

Does the link lead to what its text says?
Compare the link's text with its URL and the title and heading of the page it leads to. A link around a card can
hold a name, a price and more, so compare the name.

It is a mismatch when the text and the destination clearly name different things:
- a different product, place, event or person, like a link that names one book but leads to another;
- a different topic, like a link to delivery times that leads to the returns policy;
- a different date, year or edition than the one the text names.
It is not a mismatch when they name the same thing in different words, like "Contact" and "Get in touch", or when
the destination says more than the link, like "Exhibitions" and "Current and upcoming exhibitions". Text that names
no destination, like "read more", can't disagree with it. A title or heading that is only the site's name doesn't
show what the page is about, so judge by the URL then, and if that doesn't settle it either, it can't be decided.

### Impact

- critical: it stops visitors from reaching something they came for, like a booking or contact link that leads to
  another page;
- serious: it sends them to the wrong thing, like a link that names one product or article and leads to another;
- moderate: it misleads them, but what they want is easy to find, like a footer link that leads to a related help
  page instead of the one it names;
- minor: they would hardly notice, like a link to last year's edition of a page few people look for.
Fixes look like "link to the returns policy", or "change the link's text to name the page it leads to".
