Examples from other sites, each with the link from list_links and its destination from get_destination_summary:

- link {"text": "Poetry night", "url": "https://www.riversidelibrary.example/events/poetry-night"}
  destination {"status": 200, "title": "Riverside Library", "heading": "Oops!", "words": "Oops! The page you were looking for has moved or never existed. Try the search box above.", "word_count": 16}
  fail: the page says what was asked for isn't there.
- link {"text": "The Silk Road", "url": "https://www.citymuseum.example/exhibitions/silk-road"}
  destination {"status": 200, "title": "The Silk Road | City Museum", "heading": "This exhibition has ended", "words": "This exhibition has ended and its page is no longer available. See what's on now.", "word_count": 15}
  fail: the page says the exhibition it was about is gone.
- link {"text": "Lost property", "url": "https://www.riversidelibrary.example/lost-property"}
  destination {"status": 200, "title": "Lost property | Riverside Library", "heading": "Lost something?", "words": "If you left something behind, ask at the front desk. We keep lost items for 30 days.", "word_count": 64}
  pass: it is a real page whose topic is lost things.
- link {"text": "Membership", "url": "https://www.riversidelibrary.example/join"}
  destination {"status": 200, "title": "Join | Riverside Library", "heading": "Become a member", "words": "Membership is free for residents. Fill in the form to join.", "word_count": 11}
  pass: there are few words, but it is the real membership page.
- link {"text": "Tote bag €12", "url": "https://shop.citymuseum.example/tote-bag"}
  destination {"status": 200, "title": "Tote bag | City Museum shop", "heading": "Tote bag", "words": "Tote bag €12. Out of stock. Tell me when it's back", "word_count": 48}
  pass: the product is out of stock, but its page is still there.
