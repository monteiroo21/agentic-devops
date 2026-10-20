---
name: soft-404
description: Judge whether a page that answered with a working status is really an error page, or says that what it
  was about is no longer there. Use it on the pages the links lead to, read with fetch_page_text.
metadata:
  needs-page-text: "true"
---

Is the page an error page, even though the server answered it with a working status?
A status of 200 only means the server answered: an error page or an empty page can return it too. Judge the page by
its title, its heading and the start of its text.

It is a soft 404 when the page:
- says it can't find what was asked for, like "page not found", "404" or "we couldn't find that";
- says that what it was about is gone, like "no longer available" or "has been removed";
- shows an error instead of content, like "something went wrong";
- has nothing of its own: an empty page, a placeholder that only sends you somewhere else, or a domain parked for
  sale.
It is not a soft 404 when the page shows real content, even if there is little of it, like a contact page with a
form or a sign-in page. A page whose topic is something missing, like an article about lost luggage or a help page
about error messages, isn't one either. Nor is a product that is out of stock but still has its own page, with its
details: only a page that says the thing itself is gone is a soft 404.
