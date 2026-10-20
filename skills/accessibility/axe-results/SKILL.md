---
name: axe-results
description: Explain the accessibility violations axe-core confirmed, and decide the elements it could not decide
  (needs_review). Use it on the results of run_axe.
---

axe-core's violations are already confirmed, so don't judge them again. Explain each one in one or two sentences:
what is wrong, and who it affects. Missing alt text and form fields with no label affect screen reader users, low
contrast affects people with low vision, and elements that can't be reached with the keyboard affect people who
don't use a mouse. Suggest a fix if you can tell from the HTML, naming what to add or change, like a label for a
form field or an alt attribute for an image. Only suggest specific colours if you know their contrast ratio meets
the requirement: 4.5:1 for normal text, and 3:1 for large text (at least 24px, or about 18.7px if bold), for icons
and for the edges of form fields.

The elements under needs_review are ones axe-core could not decide. Decide them only if the information you have
shows whether they meet the rule, for example when its messages give the contrast ratio. If it doesn't, for example
a colour contrast over an image or a gradient, or under another element, which depends on the pixels behind the
text, say that it can't be decided instead of guessing. Not being able to check something is never a reason to fail
it.
