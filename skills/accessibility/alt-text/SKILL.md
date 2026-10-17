---
name: alt-text
description: Judge whether each image's alt text says what the image adds to the page (WCAG 1.1.1).
  Use it on the images returned by get_page_content.
metadata:
  wcag: "wcag111"
  elements: "images"
  needs-selector: "true"
---

Does each image's alt text say what the image adds to the page?
Look out for file names, "image" or "photo", and alt text that repeats the link text. An empty alt is right for
a decorative image, and for an image inside a link whose text already says where it goes. An image with no alt
field has no alt attribute at all, which fails.