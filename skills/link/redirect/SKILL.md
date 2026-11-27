---
name: redirect
description: Whether a link that was redirected still ends on the page it promises, or somewhere else such as the
  homepage or a sign-in page.
category: links
wcag: null
tools: [list_links, get_destination_summary]
---

### When to look closer

list_links gives each redirected link's final URL, and that often decides it: a final URL that is the site's root,
or a sign-in page, shows where the link ended. When the final URL doesn't show what the page is, read its title and
heading with get_destination_summary.

### How to judge

Does the redirected link still end on the page it promises?
Compare the link's text and URL with its final URL and, if you read it, the title and heading of the page it ended
on.

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

### Impact

- critical: it stops visitors from reaching something they came for, like a booking or contact link that ends on
  the homepage;
- serious: content they would want is gone or locked away, like a product that ends on the list of all products, or
  a public document that ends on a sign-in page;
- moderate: they can find what they want another way, like a section link that ends on the homepage, whose menu
  leads there;
- minor: they would hardly notice, like an old page that few people would look for.
Fixes look like "link to the page's new address", "remove the link" or "make the document public".
