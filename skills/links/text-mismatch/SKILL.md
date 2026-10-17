---
name: text-mismatch
description: Judge whether each link leads to what its text says, by comparing its text with its URL and the title
  and heading of the page it leads to. Use it on the links returned by check_page_links.
metadata:
  needs-page-text: "true"
---

Does the link lead to what its text says?
Compare the link's text with its URL and the title and heading of the page it leads to. It is a mismatch when the
text and the destination clearly don't agree, for example a link that names one product but leads to another. Text
that names no destination, like "read more", can't disagree with it.
