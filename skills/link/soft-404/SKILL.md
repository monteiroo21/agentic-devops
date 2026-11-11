---
name: soft-404
description: Whether a page that a link leads to is really an error page, even though the server answered it with a
  working status.
category: links
wcag: null
tools: [list_links, get_destination_summary]
---

### When to look closer

A link's text and URL don't show whether the page behind it is an error page: only that page's title, heading and
text do. Read the destination of every link you answer soft-404 for with get_destination_summary. You can only pass
or fail a link whose destination you read; if you didn't read it, the answer is unsure.

### How to judge

Is the page an error page, even though the server answered it with a working status?
A status of 200 only means the server answered: an error page or an empty page can return it too. Judge the page by
its title, its heading, its first words and how many words it has (word_count).

It is a soft 404 when the page:
- says it can't find what was asked for, like "page not found", "404" or "we couldn't find that";
- says that what it was about is gone, like "no longer available" or "has been removed";
- shows an error instead of content, like "something went wrong";
- has nothing of its own: an empty page, a placeholder that only sends you somewhere else, or a domain parked for
  sale. A very low word_count can show this, but a short page isn't a soft 404 for that alone.
It is not a soft 404 when the page shows real content, even if there is little of it, like a contact page with a
form or a sign-in page. A page whose topic is something missing, like an article about lost luggage or a help page
about error messages, isn't one either. Nor is a product that is out of stock but still has its own page, with its
details: only a page that says the thing itself is gone is a soft 404.

### Impact

- critical: it stops visitors from reaching something they came for, like an error page in place of the booking,
  shop or contact page;
- serious: content they would want is gone, like a product or an article whose page says it is no longer there;
- moderate: they can find what they want another way, like a placeholder page that only sends them on to the
  homepage;
- minor: they would hardly notice, like an old page that few people would look for.
Fixes look like "link to the product's new page", "remove the link" or "answer the page with status 404 or 410".
