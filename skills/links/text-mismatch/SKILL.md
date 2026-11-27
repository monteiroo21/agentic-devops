---
name: text-mismatch
description: Judge whether each link leads to what its text says, by comparing its text with its URL and the title
  and heading of the page it leads to. Use it on the links returned by check_page_links.
metadata:
  needs-page-text: "true"
---

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
