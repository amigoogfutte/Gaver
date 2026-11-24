# Gaver

Fælles gaveliste for familie og venner – hvem får hvad, hvem har købt det, og hvor meget du bruger.

- **Fælles liste**: alle der får linket deler den samme liste (de logger ind med deres egen Home Assistant-bruger).
- **✋ Det er mig**: hver bruger vælger sit eget navn på listen – én gang. Derefter kan de ikke se, hvad der er købt til dem, og heller ikke de andres idéer til dem. Serveren sender det slet ikke til deres telefon. Kun en administrator kan åbne for det igen.
- **Ønsker**: man kan skrive sine egne ønsker på sig selv. De bliver stående hos en selv, også når nogen har købt dem.
- **Antal**: ønskes der fx 3 stk, bliver gaven stående på indkøbslisten for de andre, indtil der er købt 3.
- **Anledninger**: *Jul 2026*, *Jul 2027*, *Fødselsdage 2026* og *Fødselsdage 2027* fra start – opret selv flere.
- **Budget**: dit eget budget pr. person (fast eller pr. anledning). Det kan deles med andre brugere, fx begge forældre om et barn – så tæller jeres køb sammen, og gaverne står som "Købt af Rikke og Jesper". Et budget der ikke er delt, kan kun du se.
- **Oversigt**: hvad du har brugt og hvad alle har brugt til den valgte anledning.
- **🛍️ Indkøb**: idéer og ønsker der mangler at blive købt, og personer uden gave. Kan deles som tekst.
- **🙈 Skjul**: kigger nogen med over skulderen, kan du skjule dem på din telefon.
- **🔑 Administrator**: Home Assistants administratorer er altid administrator. De kan gøre andre til administrator under ⚙️, åbne for "det er mig" igen og slette personer og anledninger.

Data ligger i `/config/gaver/faelles.json`. Appen virker uden dækning og synkroniserer, når der er forbindelse.

## Installation

1. HACS → ⋮ → **Brugerdefinerede repositories** → tilføj `https://github.com/amigoogfutte/Gaver` som type **Integration**.
2. Installer **Gaver** og genstart Home Assistant.
3. Indstillinger → Enheder og tjenester → **Tilføj integration** → *Gaver*.

Appen findes derefter i sidemenuen under **Gaver** og på adressen `https://<din-home-assistant>/gaver-app/index.html`
(kort adresse: `/gaver-start`). På iPhone: åbn adressen i Safari → Del → **Føj til hjemmeskærm**.
