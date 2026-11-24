# Gaver

Hold styr på hvem der får hvad i gave – og hvor meget du bruger på hver person.

- **Personer**: tilføj familie og venner, gerne med fødselsdag (så får du en liste over de næste fødselsdage) og en note med ønsker og størrelser.
- **Anledninger**: *Jul 2026*, *Jul 2027*, *Fødselsdage 2026* og *Fødselsdage 2027* er oprettet fra start. Opret selv flere, fx konfirmation, bryllup eller *Jul 2028*.
- **Gaver**: hvad, pris, hvor den er fra, og status *Idé → Købt → Givet*.
- **Budget**: giv hver person et fast budget (fx 300 kr), som gælder for alle anledninger – eller et andet beløb til én bestemt anledning. Se om du er over eller under.
- **🙈 Skjul person**: kigger en person med, kan du skjule dem (på personen eller under ⚙️). Så forsvinder deres gaver, priser og budget fra hele appen – også fra totalerne – indtil du viser dem igen.
- **⚙️ Indstillinger**: del link til appen, del oversigt eller indkøbsliste som tekst, og en kort vejledning.
- **Oversigt**: hvad du har brugt i alt til den valgte anledning, og hvad hver person får.
- **Indkøb**: idéer der mangler at blive købt, og personer der mangler en gave. Listen kan deles.
- **Sammenligning**: på hver person kan du se, hvad de har fået til de forskellige anledninger (fx Jul 2026 mod Jul 2027).

Appen virker uden dækning (gemmes på telefonen) og synkroniserer med Home Assistant. Hver Home Assistant-bruger har sin egen gaveliste, så andre i husstanden kan ikke se dine gaver.

## Installation

1. HACS → ⋮ → **Brugerdefinerede repositories** → tilføj `https://github.com/amigoogfutte/Gaver` som type **Integration**.
2. Installer **Gaver** og genstart Home Assistant.
3. Indstillinger → Enheder og tjenester → **Tilføj integration** → *Gaver*.

Appen findes derefter i sidemenuen under **Gaver** og på adressen `https://<din-home-assistant>/gaver-app/index.html`
(kort adresse: `/gaver-start`). På iPhone: åbn adressen i Safari → Del → **Føj til hjemmeskærm**.
