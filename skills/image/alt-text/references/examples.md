Examples from other sites, each with the image from list_images and, where it was needed, its context from
get_image_context:

- {"filename": "DSC_0193.JPG", "alt": "DSC_0193.JPG", "in_link": false}
  fail: the alt is the photo's file name, so a screen reader user learns nothing from it.
- {"filename": "autumn-header.jpg", "alt": "banner", "in_link": false}
  fail: "banner" only says what kind of image it is, not what it shows.
- {"filename": "vase-421.jpg", "alt": "Blue and white porcelain vase with a dragon painted around it", "in_link": false, "heading": "Ming dynasty ceramics"}
  pass: the alt says what the photo shows, and fits its section.
- {"filename": "storytime.jpg", "alt": "", "in_link": true, "link_text": "Storytime for under-fives Saturdays, 10:30", "heading": "Events for children"}
  pass: the link's text already says where it goes, so the empty alt keeps the photo from adding noise.
- {"filename": "bookbinding.jpg", "alt": "Bookbinding for beginners", "in_link": true, "link_text": "Bookbinding for beginners 12 April · €15", "heading": "Events for adults"}
  fail: the alt repeats the start of the link's text, so a screen reader reads "Bookbinding for beginners" twice.
- {"filename": "instagram.svg", "alt": "", "in_link": true, "link_text": "", "heading": "Follow us"}
  fail: the image is all there is in the link, so with an empty alt the link has no name.
- {"filename": "floor-plan-2.png", "in_link": false, "context": "Use the floor plan to find the study rooms on the second floor.", "heading": "Floor plans"}
  fail: the image has no alt attribute, and the text shows it is a plan people need to find the rooms.
- {"filename": "divider-leaves.svg", "alt": "", "in_link": false, "heading": "Opening hours"}
  pass: a divider only decorates the page, so an empty alt is right.
- {"filename": "reading-room.jpg", "alt": "Children reading in the garden", "in_link": false, "caption": "The new reading room opens on 3 March."}
  unsure: the alt is a real description, but the caption is about the reading room, and only the picture would show
  which one is right.
