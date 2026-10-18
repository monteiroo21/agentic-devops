---
name: redirect
description: Judge whether a link that was redirected still ends on the page it promises, or somewhere else such as
  the homepage or a login page. Use it on the links whose check shows a redirect chain and a final URL.
metadata:
  rules: "redirect-to-homepage redirect-to-login"
  needs-page-text: "false"
---

Does the redirected link still end on the page it promises?
Compare the link's text and URL with its final URL and the title and heading of the page it ended on. A redirect is
fine when it ends on the page the link promises, even at a new address. It is a problem when it ends somewhere the
link did not promise, such as the homepage, a login page or a general list page instead of the specific page it
names.
