Examples from other sites:

- {"text": "Opening hours", "url": "http://www.riversidelibrary.example/hours", "final_url": "https://www.riversidelibrary.example/visit/opening-hours", "title": "Opening hours | Riverside Library", "heading": "Opening hours"}
  pass: the page moved, but the link still ends on the opening hours.
- {"text": "Summer reading challenge", "url": "https://www.riversidelibrary.example/events/summer-reading-2025", "final_url": "https://www.riversidelibrary.example/", "title": "Riverside Library", "heading": "Welcome to Riverside Library"}
  fail: the link names one event but ends on the homepage.
- {"text": "Bookbinding for beginners", "url": "https://www.riversidelibrary.example/events/bookbinding", "final_url": "https://www.riversidelibrary.example/events/", "title": "Events | Riverside Library", "heading": "What's on"}
  fail: the link names one workshop but ends on the list of all events, so the workshop's page is probably gone.
- {"text": "My loans", "url": "https://www.riversidelibrary.example/account/loans", "final_url": "https://www.riversidelibrary.example/signin?next=/account/loans", "title": "Sign in | Riverside Library", "heading": "Sign in"}
  pass: a reader's loans are private, so asking them to sign in first is expected.
- {"text": "Annual report 2025 (PDF)", "url": "https://www.riversidelibrary.example/files/annual-report-2025.pdf", "final_url": "https://www.riversidelibrary.example/signin?next=/files/annual-report-2025.pdf", "title": "Sign in | Riverside Library", "heading": "Sign in"}
  fail: an annual report is a public document, but the link ends on a sign-in page.
