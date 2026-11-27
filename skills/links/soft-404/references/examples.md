Examples from other sites:

- {"url": "https://www.riversidelibrary.example/events/poetry-night", "status": 200, "title": "Riverside Library", "heading": "Oops!", "text": "Oops! The page you were looking for has moved or never existed. Try the search box above."}
  fail: the page says what was asked for isn't there.
- {"url": "https://www.citymuseum.example/exhibitions/silk-road", "status": 200, "title": "The Silk Road | City Museum", "heading": "This exhibition has ended", "text": "This exhibition has ended and its page is no longer available. See what's on now."}
  fail: the page says the exhibition it was about is gone.
- {"url": "https://www.riversidelibrary.example/lost-property", "status": 200, "title": "Lost property | Riverside Library", "heading": "Lost something?", "text": "If you left something behind, ask at the front desk. We keep lost items for 30 days."}
  pass: it is a real page whose topic is lost things.
- {"url": "https://www.riversidelibrary.example/join", "status": 200, "title": "Join | Riverside Library", "heading": "Become a member", "text": "Membership is free for residents. Fill in the form to join."}
  pass: there is little text, but it is the real membership page.
- {"url": "https://shop.citymuseum.example/tote-bag", "status": 200, "title": "Tote bag | City Museum shop", "heading": "Tote bag", "text": "Tote bag €12. Out of stock. Tell me when it's back"}
  pass: the product is out of stock, but its page is still there.
