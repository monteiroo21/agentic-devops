---
name: axe-results
description: Explain the accessibility violations axe-core confirmed, and decide the elements it could not decide
  (needs_review). Use it on the results of run_axe.
---

axe-core's violations are already confirmed, so don't judge them again. Explain each one and who it affects in one
or two sentences, and suggest a fix if you can tell. Only suggest specific colours if you know their contrast ratio
meets the requirement.

The elements under needs_review are ones axe-core could not decide. Decide them only if the information you have
shows whether they meet the rule. If it doesn't, for example a colour contrast over an image, which depends on the
pixels behind the text, say that it can't be decided instead of guessing. Not being able to check something is
never a reason to fail it.
