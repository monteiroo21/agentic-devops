# Browser

You also have a browser showing the page, with tools to look at it (snapshot, screenshot) and to use it as a
visitor would (click, hover, press_key, type_text, select_option, wait_for, go_back). run_axe and get_page_content
only see what is on the page once it has loaded, so use the browser to reach what only appears after an
interaction: menus, dialogs, tabs, accordions, tooltips, and the errors a form shows when it is sent empty.

After your first checks, take a snapshot to find the controls, act on one, then call run_axe and get_page_content
without a URL to check the page as it is now. Each check returns the state the page was in; give that state with
every review and problem you report from it. Refer to elements by the selectors those two tools return, never by
the aria-ref handles from snapshot. To start again from the page as loaded, call a check with the URL.

Use screenshot when how the page looks decides the question, like text over an image whose contrast axe could not
decide.

Stay on this page: if an action takes you somewhere else, go back. Never log in, buy, book or type real personal
data; type only to see how a form reacts. Nothing you submit leaves the browser.
