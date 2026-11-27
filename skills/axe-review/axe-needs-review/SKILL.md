---
name: axe-needs-review
description: Whether each element that axe-core, an automated checker, could not decide meets its rule, like a
  colour contrast it couldn't measure.
category: accessibility
wcag: null
tools: [get_rule_help, get_element_html, get_computed_styles]
---

### When to look closer

The task gives each item's rule, its selector and axe-core's message, which often says why axe-core couldn't decide
it, like a background image or another element on top of the text. Call get_rule_help for a rule you don't know.
Call get_element_html when the message and the selector don't show what the element is, and get_computed_styles
when its colours, font size and weight, size or visibility would decide it, or whether there is a background image
behind it.

### How to judge

These are elements axe-core could not decide. Decide them only if what you have shows whether they meet the rule,
for example when axe-core's message gives the contrast ratio. If it doesn't, for example a colour contrast over an
image or a gradient, or under another element, which depends on the pixels behind the text, the answer is unsure
instead of a guess. Not being able to check something is never a reason to fail it.

For colour contrast, the ratio must be at least 4.5:1 for normal text, and 3:1 for large text (at least 24px, or
about 18.7px if bold), for icons and for the edges of form fields. Decide it only from a ratio axe-core gives: don't
work one out from the colours yourself. get_computed_styles can still show that the text is large, so 3:1 is
enough, or that the element is hidden, so no one reads it.

### Impact

- critical: it stops some visitors from using the page, like a form field with no name;
- serious: it makes the page much harder to use, like text with too little contrast to read;
- moderate: it gets in their way, like low contrast on a short label they can guess from its place;
- minor: they would hardly notice, like low contrast on small print.
Suggest a fix in a few words, naming what to add or change, like a label for a form field. Only suggest specific
colours if you know their contrast ratio meets the requirement.
