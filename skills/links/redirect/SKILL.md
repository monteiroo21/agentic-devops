---
name: redirect
description: Judge whether a link that was redirected still ends on the page it promises, or somewhere else such as
  the homepage or a login page. Use it on the links whose check shows a redirect chain and a final URL.
metadata:
  rules: "redirect-to-homepage redirect-to-login"
  needs-page-text: "false"
---

Does the redirected link still end on the page it promises?
Compare the link's text and URL with its final URL and the title and heading of the page it ended on.

A redirect is fine when it ends on the page the link promises, even at a new address: the same page over https,
with or without a slash at the end, in a new place on the site or on a new domain. A sign-in page is fine too when
the link leads to something only a signed-in user can see, like an account, an order history or a basket.

It is a problem when it ends somewhere the link did not promise:
- the homepage, instead of the specific page the link names;
- a general list, like all the tours or all the products, instead of the one item the link names, which usually
  means that item was removed;
- a sign-in page, when the link leads to something that should be public, like an article, a product or a document
  to download;
- an unrelated page.
If neither the link's text nor its URL shows what it promises, it can't be decided.
