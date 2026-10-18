---
name: soft-404
description: Judge whether a page that answered with a working status is really an error page, or says that what it
  was about is no longer there. Use it on the pages the links lead to, read with fetch_page_text.
metadata:
  needs-page-text: "true"
---

Is the page an error page, even though the server answered it with a working status?
A status of 200 only means the server answered: an error page or an empty page can return it too. A page is a soft
404 if it is an error page or says that what it was about is no longer there, for example "page not found", "no
longer available" or an error message. A page that shows real content is not.
