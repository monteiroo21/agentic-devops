Examples from other sites:

- {"text": "Opening hours", "heading": "Visit"}
  pass: the text alone names the page it leads to.
- {"text": "Membership", "context": "Our café is open until 18:00 on weekdays. Membership", "heading": "Visit"}
  pass: the text alone is clear, so it doesn't matter that the text around it is about something else.
- {"text": "Read more", "context": "The library will close early on 24 December. Read more", "heading": "News"}
  pass: the sentence it is in says the link is about the early closing.
- {"text": "here", "context": "To renew a book, sign in to your account here.", "heading": "Borrowing"}
  pass: "here" is vague, but its sentence says it leads to signing in to renew a book.
- {"text": "Learn more", "context": "Something exciting is coming to the library this winter. Learn more", "heading": "News"}
  fail: neither the text nor its sentence says where the link goes.
- {"text": "Our partner museums", "aria_label": "click"}
  fail: screen readers read the aria_label instead of the text, and "click" doesn't say where the link goes.
- {"text": "", "heading": "Follow us"}
  unsure: the link has no text, so it probably holds an image whose alt is its name, and that alt isn't shown here.

Links with the same text that lead to different places:

- {"text": "Book now", "links": [{"destination": "https://www.citymuseum.example/tours/roman-city", "heading": "The Roman city"}, {"destination": "https://www.citymuseum.example/tours/night-walk", "heading": "Night walk"}]}
  pass: each one's heading names the tour it books.
- {"text": "Download", "links": [{"destination": "https://www.citymuseum.example/files/guide-en.pdf", "heading": "Visitor guide"}, {"destination": "https://www.citymuseum.example/files/guide-pt.pdf", "heading": "Visitor guide"}]}
  fail: they share a heading and have no context, so only the destination, which a user can't see, shows that one
  guide is in English and the other in Portuguese.
