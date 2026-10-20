Examples from other sites:

- {"text": "Membership", "url": "https://www.riversidelibrary.example/join", "title": "Join the library | Riverside Library", "heading": "Become a member"}
  pass: the text and the page name the same thing in different words.
- {"text": "The Silent River by Ana Costa €18", "url": "https://www.riversidelibrary.example/shop/the-silent-river", "title": "Winter Light by Rui Mendes | Riverside Library shop", "heading": "Winter Light"}
  fail: the link names one book, but the page is about another.
- {"text": "Holiday opening hours", "url": "https://www.riversidelibrary.example/visit/holidays", "title": "Fines and fees | Riverside Library", "heading": "Fines and fees"}
  fail: the link promises opening hours, but the page is about fines.
- {"text": "Read more", "url": "https://www.riversidelibrary.example/news/new-wing", "title": "Riverside Library", "heading": "Welcome to Riverside Library"}
  pass: "Read more" names no destination, so it can't disagree with one.
- {"text": "What's on", "url": "https://www.riversidelibrary.example/whats-on", "title": "Riverside Library"}
  pass: the URL agrees with the text, and a title that is only the site's name doesn't say otherwise.
