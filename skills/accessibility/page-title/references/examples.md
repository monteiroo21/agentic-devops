Examples from other sites:

- {"title": "Opening hours | Riverside Library", "headings": ["Opening hours", "Holidays"], "text": "We are open Monday to Friday from 9:00 to 20:00."}
  pass: the title names the page's topic and the site.
- {"title": "Riverside Library", "headings": ["Welcome to Riverside Library", "What's on", "Visit us"], "text": "Books, events and free Wi-Fi in the heart of the city."}
  pass: this is the home page, so the site's name says what it is about.
- {"title": "Riverside Library", "headings": ["Bookbinding for beginners", "What you'll learn"], "text": "A two-hour workshop on sewing your own notebook."}
  fail: the title is only the site's name, but the page is about one workshop.
- {"title": "New Page 3", "headings": ["Become a member"], "text": "Membership is free for everyone who lives in the city."}
  fail: the title is a placeholder that says nothing about membership.
- {"title": "Events | Riverside Library", "headings": ["Holiday opening hours"], "text": "Over the holidays we close at 14:00."}
  fail: the title names the events page, but this page is about holiday opening hours.
