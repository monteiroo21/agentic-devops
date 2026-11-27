Examples from other sites, each with the page and its parts:

- page: {"lang": "en", "text": "Opening hours: Tuesday to Sunday, 10:00–18:00. What our visitors say: «Une collection magnifique, à ne pas manquer.» Sophie, Lyon. Guided tours in German every Saturday. Führungen auf Deutsch jeden Samstag."}
  part: {"lang": "de", "text": "Führungen auf Deutsch jeden Samstag."}
  page fail: the French quote «Une collection magnifique, à ne pas manquer.» isn't one of the parts, so it has no
  lang of its own and a screen reader would read it with English pronunciation.
  part pass: the passage is German and its lang is "de".
- page: {"lang": "pt-PT", "text": "Bem-vindo ao Museu da Cidade. Abrimos de terça a domingo. Bienvenidos al museo."}
  part: {"lang": "en", "text": "Bienvenidos al museo."}
  page pass: the text is Portuguese and so is its lang; the Spanish sentence is a part.
  part fail: the passage is Spanish, but its lang says English.
- page: {"text": "Welcome to the City Museum. We are open from Tuesday to Sunday."}
  page fail: the page has no lang attribute, so screen readers have to guess its language.
- page: {"lang": "en", "text": "Our curator, João Gonçalves, studied at the Universidade do Porto. The café on the ground floor serves lunch."}
  page pass: names and borrowed words like "café" don't need a lang of their own.
