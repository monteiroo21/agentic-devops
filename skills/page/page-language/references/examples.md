Examples from other sites, each with the items from get_page_meta and the page's text from get_text_sample:

- items: {"id": "lang", "lang": "en"}, {"id": "part-1", "lang": "de", "text": "Führungen auf Deutsch jeden Samstag."}
  text: "Opening hours: Tuesday to Sunday, 10:00–18:00. What our visitors say: «Une collection magnifique, à ne pas manquer.» Sophie, Lyon. Guided tours in German every Saturday. Führungen auf Deutsch jeden Samstag."
  lang fail: the French quote «Une collection magnifique, à ne pas manquer.» isn't one of the parts, so it has no
  lang of its own and a screen reader would read it with English pronunciation.
  part-1 pass: the passage is German and its lang is "de".
- items: {"id": "lang", "lang": "pt-PT"}, {"id": "part-1", "lang": "en", "text": "Bienvenidos al museo."}
  text: "Bem-vindo ao Museu da Cidade. Abrimos de terça a domingo. Bienvenidos al museo."
  lang pass: the text is Portuguese and so is its lang; the Spanish sentence is a part.
  part-1 fail: the passage is Spanish, but its lang says English.
- items: {"id": "lang"}
  text: "Welcome to the City Museum. We are open from Tuesday to Sunday."
  lang fail: the page has no lang attribute, so screen readers have to guess its language.
- items: {"id": "lang", "lang": "en"}
  text: "Our curator, João Gonçalves, studied at the Universidade do Porto. The café on the ground floor serves lunch."
  lang pass: names and borrowed words like "café" don't need a lang of their own.
