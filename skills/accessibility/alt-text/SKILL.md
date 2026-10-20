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
You can't see the images, so judge what the alt text says, not whether it matches the picture. Use the src, the
link_text, the context and the heading to tell what each image is for:
- An image in a link stands for the link, so its alt says where the link goes, unless the link's own text already
  does: then its alt should be empty. An image with no link_text field isn't in a link, and an empty link_text
  means the image is all there is in the link.
- An image that shows something the text doesn't say, like a photo, a map or a chart, needs alt text that says
  what it shows.
- An image that only decorates the page needs an empty alt.

It fails when:
- the alt is a file name, like "IMG_2041.jpg", or a word that only says it is an image, like "image", "photo",
  "picture" or "banner";
- the image has no alt field, which means it has no alt attribute at all, so a screen reader may read out its file
  name instead;
- the image is all there is in a link and its alt is empty, so the link has no name;
- the alt repeats the text of the link it is in, so a screen reader reads the same words twice;
- the alt is empty, but the src or the text around the image shows that it carries information, like a chart the
  text refers to.
It passes when the alt says what the image shows or what it is for, even if it could be worded better, and when an
empty alt belongs to a decorative image or to an image in a link whose text already says where it goes. Alt text
that is long, or that starts with "Photo of", isn't a failure if the rest of it says what the image shows.
