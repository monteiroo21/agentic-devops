---
name: alt-text
description: Whether each image's alt text says what the image adds to the page.
category: accessibility
wcag: "1.1.1"
help_url: "https://www.w3.org/WAI/WCAG22/Understanding/non-text-content.html"
tools: [list_images, get_image_context]
---

### When to look closer

list_images gives each image's file name, its alt and whether it is in a link (in_link). Some images can be decided
from that alone: an alt that is a file name or a word like "image" fails, and so does an image with no alt field.
Call get_image_context for the images that need more: every image in a link, since the link's text decides whether
its alt should be empty or repeats it, every image with an empty alt that might carry information, and any image
whose alt you can't judge without knowing what the page says around it. It returns the image's file name and alt
again, with its caption, the text around it (context), the heading of its section and, for an image in a link, the
link's text (link_text) and where it goes (link_url). Judge each image by its own alt, as that row gives it.

### How to judge

Does each image's alt text say what the image adds to the page?
You can't see the images, so you can't tell whether an alt describes its picture accurately. Judge what the alt text
says, using the filename, the caption, the context, the heading and the link's text to tell what each image is for.
An alt that fits what the image is for passes. If your only doubt is whether it matches the picture, the answer is
unsure, never fail.
- An image in a link stands for the link, so its alt says where the link goes, unless the link's own text already
  does: then its alt should be empty. An empty link_text means the image is all there is in the link.
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
- the alt is empty, but the filename or the text around the image shows that it carries information, like a chart
  the text refers to.
It passes when the alt says what the image shows or what it is for, even if it could be worded better, and when an
empty alt belongs to a decorative image or to an image in a link whose text already says where it goes. Alt text
that is long, or that starts with "Photo of", isn't a failure if the rest of it says what the image shows.

### Impact

- critical: an image that is all there is in a link has no name, so screen reader users can't tell what the link
  does, like an unnamed image link to the shop or the booking page;
- serious: an image that carries information has no alt, or a useless one, like a map, a chart or a floor plan;
- moderate: a photo that adds to the page has a file name or a single word like "photo" as its alt;
- minor: the alt repeats the text of the link it is in, so a screen reader reads the same words twice.
Fixes look like 'alt="Instagram"', 'alt=""' or "say in the alt what the chart shows".
