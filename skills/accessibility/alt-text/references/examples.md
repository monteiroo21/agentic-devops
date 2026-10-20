Examples from other sites:

- {"src": "/media/uploads/DSC_0193.JPG", "alt": "DSC_0193.JPG", "context": "The new reading room opens on 3 March.", "heading": "Library news"}
  fail: the alt is the photo's file name, so a screen reader user learns nothing about the reading room.
- {"src": "/img/autumn-header.jpg", "alt": "banner", "heading": "Autumn exhibitions"}
  fail: "banner" only says what kind of image it is, not what it shows.
- {"src": "/img/collection/vase-421.jpg", "alt": "Blue and white porcelain vase with a dragon painted around it", "heading": "Ming dynasty ceramics"}
  pass: the alt says what the photo shows.
- {"src": "/img/events/storytime.jpg", "alt": "", "link_text": "Storytime for under-fives Saturdays, 10:30", "heading": "Events for children"}
  pass: the link's text already says where it goes, so the empty alt keeps the photo from adding noise.
- {"src": "/img/events/bookbinding.jpg", "alt": "Bookbinding for beginners", "link_text": "Bookbinding for beginners 12 April · €15", "heading": "Events for adults"}
  fail: the alt repeats the start of the link's text, so a screen reader reads "Bookbinding for beginners" twice.
- {"src": "/img/icons/instagram.svg", "alt": "", "link_text": "", "heading": "Follow us"}
  fail: the image is all there is in the link, so with an empty alt the link has no name.
- {"src": "/img/floor-plan-2.png", "context": "Use the floor plan to find the study rooms on the second floor.", "heading": "Floor plans"}
  fail: the image has no alt attribute, and the text shows it is a plan people need to find the rooms.
- {"src": "/img/divider-leaves.svg", "alt": "", "heading": "Opening hours"}
  pass: a divider only decorates the page, so an empty alt is right.
