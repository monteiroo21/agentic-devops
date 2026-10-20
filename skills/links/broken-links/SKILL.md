---
name: broken-links
description: Explain the links the link checker found broken, unreachable or blocked, and say whether each problem
  looks deliberate. Use it on the links that check_page_links reported with an error status, a timeout or a failed
  connection.
---

The link checker has already confirmed these problems, so don't judge them again. Explain each one in one or two
sentences, from its status or error:
- 404 or 410: there is no page at that address, because it was removed or moved, or because the URL is mistyped;
- 500 or another 5xx: the server failed, which may be temporary;
- too many redirects: the redirects go round in a loop and never reach a page;
- a timeout or a connection error: the server didn't answer, or the site no longer exists;
- 403 or 429, which the checker reports as blocked: the server refused the checker, often to keep bots out. The page
  may well work in a browser, so say that instead of calling it broken.
A broken image, stylesheet or script breaks part of the page it is on, not a page the user can go to.

Say a problem looks deliberate only when the link shows it, for example a link whose text or URL says it is an
example of a broken link, or a test page that demonstrates a 404. A link to a page a site would normally have, like
a product, an article or a contact page, isn't deliberate just because it is broken.

Suggest a fix if you can tell: correct a mistyped URL, link to the page's new address, remove a link to a page that
is gone, or fix the redirect loop.
