An example from another site, with its whole outline in page order, as get_headings returns it:

{"id": "h-1", "level": 1, "text": "Visit the library", "section_text": "Everything you need to know before you come."}
{"id": "h-2", "level": 2, "text": "Opening hours", "section_text": "Monday to Friday 9:00–20:00, Saturday 10:00–14:00."}
{"id": "h-3", "level": 2, "text": "Events", "section_text": "Free workshops and talks every month."}
{"id": "h-4", "level": 3, "text": "Bookbinding for beginners", "section_text": "12 April · €15"}
{"id": "h-5", "level": 3, "text": "Family storytime", "section_text": "Saturdays, 10:30 · free"}
{"id": "h-6", "level": 6, "text": "Places are limited, so book early."}
{"id": "h-7", "level": 2, "text": "Getting here", "section_text": "Our membership card costs €5 a year and lets you borrow up to ten books"}
{"id": "h-8", "level": 2, "text": "Help", "section_text": "FAQ Membership Accessibility Contact us", "shared": true}

h-1 pass: it says what the page is about.
h-2 pass: it names the opening times under it.
h-3 pass: it names the section, and the events under it are its subsections.
h-4 pass: it names the event on its card; a date and a price are all a card needs under its heading.
h-5 pass: same as h-4.
h-6 fail: it is a sentence of small print, not the name of a section, marked up as a heading.
h-7 fail: it promises directions, but the text under it is about membership, so it was probably copied from another
  section.
h-8 pass: "Help" is general, but it names what the links under it have in common.
