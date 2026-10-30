# CLAUDE.md – kanzlei-recherche

## Projektziel

`kanzlei-recherche` automatisiert eine bisher manuelle Web-Recherche: Für jeden Ort
eines Bundeslandes wird im Web nach Steuerberaterkanzleien gesucht, die gefundenen
Kanzlei-Homepages werden geöffnet, Impressum, Kontakt- und Team-Seite ausgelesen und
die Daten in eine Excel-Liste geschrieben.

### Zieldaten je Kanzlei

Kanzleiname, Rechtsform, Straße, PLZ, Ort, Bundesland, Telefon, E-Mail, Website,
Ansprechpartner (Liste aus Name, Titel, Funktion; der erste ist der
Hauptansprechpartner), Teamgröße (Zahl, wenn ablesbar; sonst Klasse
`1` / `2-5` / `6-10` / `11-20` / `21-50` / `51-100` / `>100` aus einer Zahl im Text, „über 20" →
`21-50`; sonst leer – keine Schätzung), Teamgröße-Basis (woraus abgeleitet),
Konfidenz (`hoch` / `mittel` / `niedrig`), Quell-URLs, Abrufdatum, Status.

Status-Werte (Excel-Dropdown): `neu` / `geprüft` / `kontaktiert` / `kein Interesse` / `ungültig`.
Automatisch dazu `keine Kanzlei` (Kanzlei-Erkennung) und `außerhalb Bundesland` (PLZ laut OpenPLZ in
einem anderen Land) – nicht im Dropdown, eigene Blätter; von Hand gepflegte Status überschreiben sie nie.

**Konfidenz-Regel (vorläufig, wird nach dem Pilot überprüft):**
- `hoch`: Impressum gelesen und PLZ/Ort gegen OpenPLZ verifiziert
- `mittel`: Kernfelder (Name, Straße, PLZ, Ort, Telefon oder E-Mail) vollständig, aber nicht verifiziert
- `niedrig`: Kernfelder fehlen
- mit Kanzlei-Erkennung (`extract.kanzlei_pruefung`): `hoch` nur, wenn als Kanzlei erkannt

## Verbindliches Regelwerk: `docs/pruefkatalog.md`

**`docs/pruefkatalog.md` ist ab 30.10.2026 verbindlich** für Kanzlei-Erkennung (Ausschluss 1A, Positivsignale 1B),
Feldregeln (Name, Adresse/Bundesland, Telefon, E-Mail, Ansprechpartner, Teamgröße), Domain-/Crawl-Regeln,
Qualitätsbericht (`recherche qualitaet`, Q1–Q14) und Abnahme vor jedem Volllauf. Die Regeln sind Pflicht, nicht
Vorschlag; jede Regel bekommt einen Test. Weicht diese Datei oder der Code davon ab, gilt der Katalog. Abweichungen,
Widersprüche oder technisch nicht umsetzbare Regeln **nur nach Rücksprache** – nie stillschweigend abschwächen.
Umsetzung in der Reihenfolge aus Abschnitt 8, ein Thema je Commit.

## Technische Regeln

Lokale Verarbeitung (Crawling, Ollama) ohne laufende Kosten. Die Discovery nutzt
bezahlte APIs (Stufe A: Brave), deshalb gibt es Budget und Kostenlog je Lauf.

- **Stack:** Python 3.12+, Typer-CLI, httpx (async), BeautifulSoup/trafilatura,
  Pydantic v2, SQLite, openpyxl, pytest.
- **Konfiguration:** Alles Konfigurierbare (Modell, Ollama-URL, Ortsliste,
  Ausgabepfad, Parallelität, Quellen, Drosselung, Budget, Kosten) steht in
  `config/settings.yaml`, damit derselbe Code lokal und auf einem Mietserver läuft.
  Geheimnisse (API-Keys) nur in `.env` (nie committen, Vorlage `.env.example`).

### Discovery (`src/recherche/search/`)

**Ziel ist maximale Vollständigkeit je Bundesland, nicht Schnelligkeit.** Eine Websuche
allein findet nur Kanzleien mit Website und gutem Ranking; die Discovery ist deshalb die
Vereinigung mehrerer Quellen.

**Prinzip: zuerst die günstigste Quelle vollständig ausschöpfen, dann nur die Lücken mit
der nächsten Quelle füllen.**

- **Stufe A – Websuche** (`recherche search`, `run`): je Ort alle Query-Varianten aus
  `config/queries.yaml`. Adapter per `discovery.stufe_a.adapter`:
  1. `brave` (Brave Search API) – **Standard**. Key `BRAVE_API_KEY` aus `.env`,
     bis 20 Treffer je Anfrage (`count`), Paging über `offset` (Seitenindex 0..9),
     `country=DE`, `search_lang=de`, Retry mit Backoff bei 5xx und 429.
  2. `google_cse` (Custom Search JSON API) – nur für Alt-Keys: für Neukunden gesperrt,
     Abschaltung 01.01.2027. 10 Treffer je Anfrage, Paging bis 30, Tageslimit beendet den
     Lauf sauber.
  3. `serpapi` – Platzhalter für den Pilot-Vergleich (Google-Trefferseite).
  4. `ddgs` (DuckDuckGo, ohne Key) – **nur lokale Entwicklung**.
- **Gründlichkeit je Einwohnerklasse** (`discovery.stufe_a.einwohner_regel`): erste N
  Vorlagen aus `queries.yaml` (Reihenfolge zählt!) und höchstens M Ergebnisseiten je
  Query – Standard (kalibriert am SH-Lauf, vorläufig): unter 5.000 Einwohnern **keine Websuche**
  (`varianten: 0`, Kanzleien über das amtliche Verzeichnis je PLZ; Kanzleien je 100 Anfragen dort 5–10), bis 20.000
  2/2 (28–50), darüber alle/3 (175); Stadtteil-Suchorte 1/1. **Ausnahme:** War die letzte erlaubte Seite voll und überwiegend Kanzlei-Domains
  (`weiter_wenn_voll`, > 50 % nicht auf der Blocklist), wird die nächste Seite trotzdem
  geholt. Orte ohne Einwohnerzahl werden vollständig gesucht. Gilt für `search` und
  `search-compare`, nicht für `fill`.
  Je Stufe schaltet `weiter: false` das Weiterblättern ab.
  `varianten: 0` = keine eigene Suche für diese Stufe (Kanzleien dort über Nachbarorte oder Stufe B).
  `recherche suche-auswertung [--grenzen 500,2000,20000] [--out datei.csv]` zeigt je Einwohnerstufe Orte, Queries, Anfragen (davon
  über der Seitenregel), Kosten, neue Kandidaten und deren Ergebnis (Kanzlei im Land, außerhalb, keine
  Kanzlei, nicht gelesen), Kosten je Kanzlei und Kanzleien je 100 Anfragen – zum Kalibrieren der Regel.
- **Fremdort-Filter** (`discovery.stufe_a.fremdorte`, `config/grossstaedte.yaml`): Bei Orten unter
  2.000 Einwohnern füllt Brave mit bundesweiten Kanzleien auf. Nennt Titel oder Snippet eine große Stadt
  eines anderen Bundeslandes (Bonn, Aachen, Konstanz …), aber nicht den Suchort, wird kein Kandidat
  angelegt (Zähler in der Ausgabe). Mehrdeutige Namen (Hagen, Hamm, Essen, Halle …) stehen nicht in der Liste.
- **Zusatz-Suchorte großer Städte**: kreisfreie Städte ab 100.000 Einwohnern (Kiel,
  Lübeck) bekommen je PLZ-Bezirk einen Zusatz-Suchort mit Verweis auf die Gemeinde
  (`stadtteil_vorlagen` in `queries.yaml`). OpenPLZ führt je PLZ meist nur den Postort
  („Kiel"), echte Stadtteilnamen gibt es nur, wo OpenPLZ einen eigenen Ortsnamen hat.
  Coverage und Lückenfüller arbeiten weiter je Gemeinde.
  **Automatisch bei voller Trefferliste**: Erreicht eine Query einer kreisfreien Stadt (ab
  `orte.stadtteile.ab_einwohner`, ohne Einwohnerzahl genügt die volle Liste) das Limit (60 bzw.
  Seitenregel × 20), werden ihre PLZ-Bezirke aus OpenPLZ als Zusatz-Suchorte nachgesucht – auch ohne
  Stadtteil-Einträge in der Ortsliste und auch für schon erledigte Queries (Wiederaufnahme, Pilot-
  Bestand). `recherche orte` legt die Zusatz-Suchorte nur mit Einwohnerzahlen (GV-ISys) an.
- **Danach `recherche coverage`** (nach der Extraktion zählen nur Kanzleien mit Status Kanzlei und
  Bundesland laut PLZ = Lauf-Bundesland, Kreis aus der extrahierten PLZ; „keine Kanzlei", „außerhalb",
  „ohne geprüfte PLZ", „außerhalb laut Suchtreffer (nicht gecrawlt)" und „offen" (nicht gelesen) stehen getrennt;
  vorher zählen die Kandidaten). `recherche offen [--out datei.csv]` schlüsselt die offenen nach Ursache auf
  (nie gecrawlt, Timeout/Verbindung, HTTP 403/404/5xx, DNS, robots-Verbot, Portal, Seite ohne Text, kein
  Impressum, nicht extrahiert …), mit dem Weg (`crawl --wiederholen`, Playwright, `extract`, nein) und dem
  Länder-Hinweis der Suchtreffer (PLZ außerhalb / im Land / keine; große Stadt eines anderen Landes; Suchort
  unter 5.000 EW):
  gefundene Kanzleien je Kreis gegen
  `config/referenz_kammern.yaml` (Einheit `kanzleien` oder `mitglieder` × `faktor`, Land
  gesamt und optional je Kreis; Rest nach Einwohnern verteilt), Anteil je Quelle und
  „nur in einer Quelle", Kreise und Orte mit der größten Lücke.
- **Stufe B – Lückenfüller** (`recherche fill --quelle <name>`), **nur für Orte unter der
  Referenz** (`--luecken`) oder ausdrücklich genannte Orte (`--orte`); flächendeckend nur
  mit `--alle`. Reihenfolge: `places` (Google Places Text Search (New) über ein
  adaptives Rechteck-Raster) → `gelbeseiten`, `dasoertliche`, `elf880` →
  `stb_verzeichnis` (amtliches Verzeichnis PLZ für PLZ, **standardmäßig aus**, sehr
  langsam). Ob und welche Stufe-B-Quellen gebaut werden, entscheidet sich nach den
  Coverage-Zahlen aus Stufe A. Nutzungsbedingungen (Speichern von Places-Inhalten,
  automatisierte Abfragen der Verzeichnisse) vor dem Bau klären.
  - **`places`** (`search/places.py`, gebaut, aber **stillgelegt**: Name und Adresse gehören zur
    kostenpflichtigen Pro-Stufe, kostenlos ist nur „IDs Only" – Entscheidung nach dem Pilot): Gebiet des Ortes aus dem viewport einer Text Search
    („24103 Kiel, Deutschland"), dann je Rasterzelle POST `places:searchText` („Steuerberater",
    includedType `accounting`, locationRestriction = Zelle, bis 3 Seiten à 20); volle Zellen werden
    geviertelt (`zelle_km` 5 → `min_zelle_km` 0,5). FieldMask **nur** id, displayName,
    formattedAddress, addressComponents, nationalPhoneNumber, websiteUri – keine Bewertungen,
    Öffnungszeiten, Fotos. Gespeichert als Fund (Quelle „places", quell_id = place_id, gefunden_am =
    Abrufdatum). Jede Anfrage (auch die Gebietsabfrage) zählt fürs Budget und den Kostenlog
    (`kosten_je_1000` 35 = Enterprise-SKU wegen Telefon/Website). `fill` meldet die Ausbeute:
    „X Treffer, davon Y neu, Z bereits bekannt (Telefon/Adresse/Domain), W verworfen".
    `search-compare` nimmt Stufe-B-Quellen nur mit `--adapter` mit.
  - **Verzeichnisse** (`search/verzeichnisse.py`, Parser aus den Seiten in `tests/fixtures/verzeichnisse/`;
    echte Seiten nur lokal unter `echt/`, committet sind fiktive unter `beispiele/`). Ergebnisseiten aus
    `url_vorlage` ({rubrik} {ort} {ort_klein} {plz} {seite} {start}; `formular` → POST), bis `max_seiten`,
    gedrosselt pause_s + Zufall (Verzeichnisse 10–15 s, amtlich 30–35 s), robots.txt beachtet, Antworten gecacht
    (POST mit Formularfeldern im Schlüssel). Branchenverzeichnisse nie `--alle` (`nur_luecken`). Alle vier
    `aktiv: true` (Branchenverzeichnisse nur im Ergänzungsmodus).
    - `stb_verzeichnis` (amtlich, BStBK, **Grundliste**, `nur_luecken: false` → auch `fill --alle`; gleiche PLZ
    mehrerer Gemeinden kommen aus dem Cache): POST je PLZ des Ortes (`je_plz`), alle Treffer auf
      einer Seite; „Treffer: X von Y" mit X < Y wird gemeldet. Karten je Berufsträger und je Gesellschaft werden
      zu Kanzleien gruppiert (`stbv_kanzleien`): Gesellschaften sind Kanzleien; Berufsträger gehören zur Firma
      in ihren Adresszeilen (vor der Straße; Zeilen „Beratungsstelle/Zweigniederlassung …" entfallen, „c/o X" →
      X), bei gleicher Adresse zur Gesellschaft mit gleichem Namen, Namensanfang oder eindeutig gleichem
      erstem Wort; ohne Firmenzeile Einzelkanzlei „Steuerberaterin Vorname Nachname". Umbrochene Namen
      („Steuerberatungs-" / „gesellschaft") werden zusammengesetzt, Ergänzungsstriche („Treuhand- und")
      bleiben. Die Berufsträger stehen im Snippet. Abgleich über die Dubletten-Logik → „X Treffer, Y neu,
      Z bekannt". **Website-Nachsuche** (`search/nachsuche.py`): neue Einträge ohne Website → Brave
      „{Name} {Ort}", eine Seite; erster Treffer mit Kanzlei-Domain, die zum Namen passt, wird Website
      (Kandidat → crawl/extract); sonst bleibt die amtliche Anschrift („Website vorhanden" = nein). Jeder
      Eintrag einmal.
    - `gelbeseiten`, `dasoertliche`, `elf880` (11880.com) **nur Ergänzung** (`nur_ergaenzen`): Treffer werden
      nur bekannten Kandidaten **ohne Website** zugeordnet (leere Felder Telefon, Adresse, Website; Fund
      gespeichert; extract neu, bei neuer Website auch crawl). Unbekannte Einträge werden nicht angelegt,
      Kandidaten mit Website nicht angefasst (deren Website-Daten gelten) – Ausgabe „ergänzt / bekannt mit
      Website / nicht im Bestand". Ortswahl `fill --unvollstaendig`: Orte mit Kandidaten ohne Website, denen
      Telefon oder Straße fehlt. gelbeseiten: eigene Kacheln (Telefon/Website base64 in `data-prg`/
      `data-webseitelink`), nur die ersten 50 (weitere per AJAX-POST, nicht gebaut); dasoertliche (25 je Seite,
      `recFrom={start}`) und 11880 (50 je Seite, `?page={seite}`): schema.org (JSON-LD, Microdata).
- **Interface** (`search/base.py`): `QueryAdapter.search(query, max_results)` (Stufe A)
  und `OrtsAdapter.search_ort(ort)` (Stufe B). Alle liefern `SearchHit` mit Name,
  Straße, PLZ, Ort, Telefon, Website, Quelle, Quell-ID (+ Snippet, Fundstelle). Jede
  Quelle ist in `settings.yaml` unter `discovery.quellen.<name>.aktiv` einzeln
  schaltbar.
- **Kosten**: Jeder Adapter zählt tatsächlich gesendete Anfragen (Cache-Treffer zählen
  nicht). `max_anfragen_je_lauf` (Standard 1000) ist das Budget je Lauf und Adapter;
  `kosten_je_1000` steht beim jeweiligen Adapter. Anfragen und Kosten landen je Query im
  Lauf-Protokoll und werden am Ende ausgegeben.
- **Limits**: `LimitErreicht` (Kontingent/Tageslimit, Budget, ungültiger Key) beendet
  den Lauf sauber; ein erneuter Aufruf macht dort weiter. Brave: HTTP 402, `X-RateLimit-Remaining`
  mit Monatsrest 0 oder ein Guthaben-/Limit-Fehlertext → sofort „Brave-Guthaben erschöpft – im
  Dashboard aufladen, dann erneut starten" (meldet eine erfolgreiche Antwort Rest 0, endet der Lauf vor
  der nächsten Anfrage). Jeder Request hat einen **harten Gesamt-Timeout** (`timeout_s` + 2 s, Watchdog-
  Thread – httpx' Timeout gilt nur je Lesevorgang), höchstens `retries` (3) Wiederholungen, danach
  Query als Fehler und weiter; Retry-Meldungen erscheinen sofort im Terminal. Fortschritt je Query:
  „Ort 812/1127 · … · gesamt N Anfragen ≈ X". Vor dem Start: offene Queries × Seiten × Preis gegen
  `discovery.quellen.<adapter>.guthaben` (von Hand gepflegtes Restguthaben); liegt die Schätzung
  darüber, fragt `search`/`run` nach (`--ja` ohne Rückfrage). Jede API-Antwort wird in
  `api_cache` gespeichert, damit eine Wiederaufnahme nichts doppelt bezahlt.
- **Blocklist** (`config/domain_blocklist.txt`) und **Dubletten-Logik**
  (`search/dedupe.py`) liegen außerhalb der Adapter (`search/pipeline.py`) und gelten
  für alle gleich:
  - Blocklist: Websuche-Treffer auf Portalen werden verworfen; bei Verzeichnis-/Places-
    Treffern wird nur die Portal-Website entfernt, die Kanzlei bleibt.
  - Zusammenführen: Domain gleich → Telefon gleich → Adresse gleich und Name passt
    (sicher); **gleiche Adresse, anderer Name → nicht zusammenführen, nur Verdacht**
    (Bürogemeinschaft); PLZ gleich und Name ähnlich → zusammenführen, aber „unsicher".
    Verdacht und unsicher landen im Blatt „Dubletten".
  - Beim Zusammenführen werden nur leere Felder ergänzt, nie überschrieben. Jeder Fund
    bleibt mit Quelle in der Tabelle `funde` erhalten.
- `recherche search-compare --orte …` vergleicht **alle aktiven Websuche-Adapter** (oder
  `--adapter a,b,…`): Kanzleien je Adapter und Ort, Schnittmenge, „nur in diesem
  Adapter", Paar-Matrix. Treffer werden über die Dubletten-Logik zusammengefasst.

### Ortsliste (`orte.py`, `recherche orte`)

- Gemeinden aus OpenPLZ (`/de/FederalStates/<Land>/Localities`, alle Seiten, gecacht),
  zusammengefasst je amtlichem Gemeindeschlüssel (AGS); Name ohne Zusatz („Kiel,
  Landeshauptstadt" → „Kiel"), niedrigste PLZ in `plz`, übrige in `plz_weitere`.
- Kreis und Einwohner aus dem GV-ISys von Destatis (XLSX oder CSV, Pfad oder URL in
  `orte.gv_isys.quelle`, Spaltennamen einstellbar; Satzart 40 = Kreis, 60 = Gemeinde),
  Zuordnung über den AGS. Gleiche Namen: in verschiedenen Kreisen mit dem Kreis („Hasselberg
  (Schleswig-Flensburg)"), im selben Kreis mit dem Namenszusatz („Garding (Stadt)", „Garding (Kirchspiel)"). Kreisnamen im Format der Referenzdatei („Kreis Dithmarschen",
  „Kreisfreie Stadt Kiel").
- Ausgabe `config/orte_<BL>.csv` (`ort;plz;kreis;einwohner;ags;plz_weitere;gemeinde;
  stadtteil`). **Eine bestehende Datei wird nie ungefragt überschrieben** (`--force`,
  `--out`, `--dry-run`); vorher Vergleich alt/neu und Kostenabschätzung je Einwohnerstufe.

### Crawling (`crawl.py`)

- Für jeden Kandidaten **mit Website**: Start bei `https://<domain>/` (Rückfall `http://`,
  dann Treffer-URL), Links sammeln, Impressum-/Kontakt-/Team-Seite über Pfad und Linktext
  erkennen (`/impressum`, `/kontakt`, `/team`, `/ueber-uns`, `/kanzlei`, `/wir`,
  „Unser Team", „Mitarbeiter", „Ansprechpartner", „Berufsträger", „Partner", „Über uns",
  „Berater", „Unsere Steuerberater", „Steuerberater" (nur als ganzer Linktext), „Beraterinnen",
  „Köpfe", „Menschen", „Wir über uns" („berater" nur im Linktext – im Pfad träfe es
  `/steuerberater-kiel/`); Abzüge für
  Datenschutz, Karriere, Jobs, Kooperationspartner, Honorar, Preise, Empfehlungen, Leistungen;
  Linktexte über fünf Wörter sind Fließtext und zählen nicht, nur der Pfad). Das Impressum wird auch
  über den Linktext erkannt, wenn der Pfad fremd ist (`/information/license`). Die zweitbeste
  Team-Kandidatin wird als `team2` gelesen (z. B. `/berufstraeger` und `/ueber-uns`) –
  höchstens 6 Abrufe je Domain. Ohne Impressum-Link wird das Impressum **vor** Kontakt- und
  Teamseite geraten (`/impressum`, `/impressum/`, `/impressum.html`; mit `crawl --wiederholen`
  zusätzlich `/impressum.php`, `/imprint`, `/kontakt/impressum`, `/rechtliches/` …); als
  Impressum gilt eine Seite mit „Impressum", „Imprint", „Anbieterkennzeichnung" oder „Angaben
  gemäß § 5" (Soft-404 wird erkannt). Linktexte auch „Rechtliches", „Rechtliche Hinweise",
  „Imprint". Der Grund im Blatt nennt die probierten Pfade.
- Max. 1 Request/Sekunde je Domain – **auch Weiterleitungen und robots.txt** (Weiter-
  leitungen werden selbst verfolgt) –, viele Domains parallel (`crawl.parallel_domains`),
  Timeout 15 s, 2 Retries mit Backoff, User-Agent aus `settings.yaml`.
- robots.txt nach RFC 9309 (`robots.py`, nicht `urllib.robotparser`): für uns gilt die Gruppe,
  deren User-Agent unser Produkt-Token genau trifft, sonst `*`; längste Regel gewinnt, bei
  Gleichstand Allow; `*`/`$`; leeres Disallow sperrt nichts; Weiterleitungen werden gefolgt.
  Sperren nur für GPTBot/ClaudeBot/CCBot oder nur für Pfade (`/wp-admin/`) betreffen die
  Startseite nicht. 4xx → alles erlaubt. Bei 5xx, Timeout oder Verbindungsfehler werden http/https
  und mit/ohne www probiert; bleibt sie unerreichbar, wird die Seite trotzdem gelesen (RFC 9309 sieht
  die Sperre dann nur vorübergehend vor; so entschieden – echte Verbote bleiben respektiert) – Hinweis „robots.txt nicht abrufbar (HTTP 500), Seite
  gelesen" in der Crawl-Ausgabe und in „Abweichung". Fehler werden gecacht, mit `--wiederholen` neu
  versucht. **DNS-Fehler** (Domain existiert nicht) → „Domain nicht erreichbar (DNS)", nie als
  robots-Sperre; diese Domains überspringt `crawl --wiederholen`. Anfragen mit vollständigem
  Kopfsatz (Accept, Accept-Language, Accept-Encoding), User-Agent bleibt unserer.
- `recherche robots-check --domain X`: Diagnose live ohne Cache – robots.txt aller Varianten roh
  (Status, Fehlermeldung, Weiterleitungskette), geltende Gruppe und entscheidende Regel, Startseite
  (Textlänge, erkannte Unterseiten, Frames/Weiterleitungen); `--rendern` zusätzlich mit Playwright.
- `recherche blocklist-vorschlag [--mindestens N] [--out datei.csv]`: Domains aus „Nicht
  gefunden" mit Grund robots.txt/HTTP 403 und gelesene Domains mit Status „keine Kanzlei", nach
  Stammdomain zusammengefasst und nach Zahl der Suchorte sortiert (ab 5 Suchorten als „Portal"
  markiert), mit Treffern, Name und Snippet – Vorschläge
  zum Prüfen; übernommen wird von Hand in `config/domain_blocklist.txt`.
- Jede URL wird **einmal** geladen und als **HTML (gzip) + bereinigter Text** in SQLite
  gecacht (auch 404). Der Text entsteht aus dem ganzen `<body>` mit Zeilenumbrüchen –
  **Navigation und Fußzeile bleiben**, weil dort oft Adresse und Telefon stehen.
  Cookie-/Consent-Banner werden entfernt; **versteckte Elemente bleiben** (Akkordeons
  im Impressum und auf Teamseiten enthalten oft die Namen).
- **Unsichtbare Zeichen** (`textnorm.py`) vor allen Regeln und vor dem Prompt entfernen:
  bedingte Trennstriche (U+00AD, auch als `&shy;`), Nullbreite-Zeichen (U+200B–U+200D,
  U+2060, U+FEFF); geschützte und schmale Leerzeichen → normales Leerzeichen; NFC. Gilt
  für Textknoten und Attribute nach dem Parsen (Linktexte, Seitentext, trafilatura), für
  Regel-Eingaben (auch gecachten Text) und beim Laden der Fixtures.
- Playwright nur als Rückfall für fast leere Seiten (< `crawl.min_text_zeichen`), für eine
  Startseite mit HTTP 403 (Bot-Schutz) und für
  Teamseiten mit großem HTML, aber kaum Text (≥ `playwright_team_min_html`, Text unter
  `playwright_team_min_anteil` = 2 % des HTML; Namen per JavaScript) – nur mit
  `crawl.playwright: true` (`setup.sh --mit-playwright`). Das gerenderte HTML ersetzt das
  ursprüngliche nur, wenn es mehr Text ergibt. Mit `crawl --wiederholen` werden auch gecachte
  403-Startseiten und Leerseiten aus einem Lauf ohne Playwright gerendert (Cache wird ersetzt).
  Fast leere Startseiten: zuerst **Frames/iFrames, Meta-Refresh und JavaScript-Weiterleitungen**
  (`location.href = "…"`) derselben Site folgen (je bis 3); der Frame mit dem meisten Text wird
  Startseite, Links aus allen Frames zählen. Playwright wartet auf `domcontentloaded` +
  `crawl.playwright_warten_s` (2 s; „networkidle" lief in Timeouts) und hängt die Inhalte aller
  Frames an. Der Grund „Seite ohne Text" nennt Textlänge und Playwright-Ergebnis. Das Ergebnis (gerendert, fehlgeschlagen
  mit Grund, nicht mehr Text, nötig aber aus) steht in `meta.json` der Fixture und in der
  Ausgabe von `fixture-add`.
- Fehler (nicht erreichbar, HTTP-Fehler, robots.txt, kein Impressum, JavaScript-Seite,
  Weiterleitung auf Portal) → Blatt „Nicht gefunden" **mit den Discovery-Daten** (Name,
  Adresse, Telefon), damit von Hand nachgepflegt werden kann.

### Extraktion (`extract/`)

- Austauschbar über ein Interface (`extract/base.py`), damit später weitere Anbieter
  ergänzt werden können.
- Stufe 1 regelbasiert (`rules.py`): Straße, PLZ, Ort, Telefon (über
  `normalize_telefon`), E-Mail aus dem Impressum (Rückfall Kontakt-, Startseite, zuletzt
  Meta-Beschreibungen). Adressen/Telefone in Fremd-Abschnitten (Kammer, Aufsicht,
  Versicherung, Hoster, Webdesign) werden ignoriert: `config/impressum_ausschluss.yaml`.
  - Bekannte Fremdadressen der Ausschlussliste gelten nur dann als fremd, wenn sie im
    Fremd-Abschnitt stehen, der Name der Institution davor steht oder das Impressum eine
    andere Adresse nennt. Steht dort nur diese Adresse (Kanzlei im Haus der Kammer), wird
    sie übernommen, mit Hinweis in „Abweichung".
  - Telefon: Festnetz vor Mobil (015/016/017), Impressum vor den anderen Seiten. Neben
    „Tel/Telefon/Fon" zählen „Telefonnummer", „Rufnummer", „Zentrale", „Büro", ☎ und im
    Impressum jede unbeschriftete Nummernzeile mit Vorwahl (nicht hinter „Fax"). Liefert der
    bereinigte Text kein Festnetz, wird es aus dem rohen HTML genommen (Text und tel:-Links,
    nicht Attribute/Skripte), bei mehreren Standorten die Nummer nächst der Kanzlei-PLZ.
    Mobil nur als Rückfall. Gültig ist nur eine Rufnummer mit Ortsnetz 2–9, Mobil 15–17 oder
    Service 18 (Zahlenkolonnen und SVG-Pfade sind keine Nummern).
  - E-Mail: Impressum-Text vor Navigation/Fußzeile vor mailto-Links.
- Stufe 2 LLM über Ollama (`ollama.py`, URL und Modell aus `settings.yaml`) auf allen
  Seiten: Name, Rechtsform, Personen (Name, Titel, Funktion), genannte
  Mitarbeiterzahl/Größenklasse; Adresse/Telefon/E-Mail nur als Rückfall.
  - JSON-Schema **immer** über den Ollama-Parameter `format` erzwingen.
  - Thinking-Modus **immer** abschalten (`"think": false`), bei allen Modellen.
  - `num_ctx` immer setzen (Ollama-Standard schneidet längere Seiten ab).
  - Modelle: lokal `qwen3.5:9b`, Fallback `llama3.1:8b`; GPU-Server `qwen3.6:27b`,
    Alternative `gemma4:26b`. Umschalten nach dem Fixture-Vergleich (`fixture-eval`).
- Prompt (`prompt.py`): gezielt reduzieren statt hinten abschneiden – Navigation/Fußzeile
  einmal (Abschnitt RAHMEN: Zeilen auf ≥ ¾ der Seiten, mind. 2), Cookie-Zeilen und
  Textbausteine (Datenschutz, Haftung, Urheberrecht, Streitschlichtung) raus,
  Impressum-Schlüsselstellen (vertretungsberechtigt, Partner, Geschäftsführer, Inhaber,
  Berufsbezeichnung; ±4 Zeilen) immer vorn, dann die regelbasiert erkannten PERSONEN – als
  Vorschlag, den das Modell vollständig in `personen` übernehmen soll (sonst lässt es die
  Personen als „schon bekannt" weg).
  Budget `llm.max_text_chars: auto` = (num_ctx − 2700 Token) × 3,0 Zeichen/Token, verteilt
  nach Anteilen je Seite. Wird gekürzt oder ist der Kontext fast voll, steht ein Hinweis im
  Log und im Lauf-Protokoll. `recherche fixture-prompt --domain X [--live] [--namen]` zeigt
  jede Stufe, versteckte Quellen der Teamseiten (noscript, Skripte, JSON-LD, Attribute),
  die gezählten Berufsträger mit Herkunft (LLM, Teamseite, Impressum-Berufsbezeichnung),
  belegte Felder und Abbruchgrund der Antwort und vergleicht geschätzte mit echten
  Prompt-Token. Mit `--namen` zusätzlich: das Protokoll je Impressum-Schlüsselwort (warum
  eine Zeile Namen lieferte oder verworfen wurde), alle Personen nach der Zusammenführung
  mit Funktion, Herkunft und Berufsträger-Status, und Personen, die nur in Navigation/
  Fußzeile stehen und deshalb nicht zählen. Das Playwright-Ergebnis aus `meta.json` steht
  bei den Teamseiten. Außerdem: Kanzleiname LLM → Ergebnis, Rechtsform Regel/LLM,
  Telefonkandidaten je Seite und alle Telefonnummern im rohen HTML (tel:-Link, Attribut,
  Skript, Text), der Impressum-Kopf und die Zeilen ab jedem Namen der Teamseite.
- Personen (`personen.py`, `config/personen_funktionen.yaml`): Kacheln/Listen auf beiden
  Teamseiten. Ein Name gilt nur mit einer Funktion aus der Liste **in derselben Zeile**
  („Name – Funktion", „Name (Funktion)"), **in der Folgezeile** (auch mehrere
  Funktionszeilen untereinander: „Gesellschafter" / „Steuerberater"; reine Titelzeilen
  („Dipl.-Volkswirt") und bis zu zwei Kontaktzeilen dazwischen unterbrechen nicht und Titel
  wandern in den Titel; Aufzählungen mit Kommas dürfen bis 220 Zeichen lang sein; eine Zeile,
  die mit einem Namen beginnt, ist die nächste Person; Firmen-/Adresszeilen mit PLZ oder
  PartG/mbB sind nie Funktion), als **Abschnittsüberschrift** („Partner" über mehreren
  Kacheln, gilt bis zur nächsten Überschrift oder zu anderem Text) oder direkt darüber (Kachel mit Rolle als
  Überschrift); Zeilen aus dem RAHMEN sind nur Namen, wenn darunter eine eigene Funktion
  steht, die nicht selbst im Rahmen steht („Andreas Schreiner" auch in der Fußzeile); kurze
  Rahmenzeilen („Steuerberater" im Logo) zählen als Funktion unter einem Namen. Eine reine
  Namensliste unter einer Überschrift wird durch spätere Detailblöcke derselben Personen
  ersetzt; Menüwörter („Berufsträger", „Firmenphilosophie",
  „Historie" …) und Institutionen („…kammer", „…gericht") sind nie Namen; „BD-Partner"
  (Bindestrich davor) ist keine Funktion und kein Impressum-Schlüsselwort. Initialen („W. Drews") und Anrede
  („Frau …") sind erlaubt; `*kauffrau` in der Liste trifft auch Komposita
  („Einzelhandelskauffrau"). Titel und Berufskürzel vor dem Namen („Diplom-Betriebswirt",
  „StB Dipl.-Kfm.", „WP/StB") gehören zum Titel; ein Berufskürzel dort genügt als Funktion.
  Kundenstimmen mit fremder Firma („Geschäftsführer der Muster GmbH") sind keine Personen
  der Kanzlei; ein Eintrag ohne Vornamen („Steuerberater Schreiner") entfällt neben
  derselben Person mit Vornamen. Zusätzlich Personen aus JSON-LD (schema.org `Person` mit `jobTitle`)
  und aus Bild-Attributen („Name, Funktion …" in alt/title); dieselbe Person aus mehreren
  Quellen → Funktionen vereinigt. `fixture-prompt --namen` zeigt die Zeilen ab jedem Namen.
  Mitarbeiterzahl-Regel auf allen Seiten inkl. Startseite („28 Expert:innen", „Team von 28",
  „15-köpfig"; Gender-Schreibweisen normalisiert; „über N" → Klasse über N).
- Fehlende Felder bleiben `null` – **nie raten**. Konfidenz berechnet der Code, nicht das LLM.
- Zusammenführung (`merge.py`):
  - Adresse (als Einheit) und Telefon: Discovery (Stufe B) > Regeln > LLM.
  - Name: Discovery (Stufe B) > LLM; **Websuche-Titel gelten nicht als Discovery-Name**.
    Bei Partnerschaften wird der LLM-Name um die Berufsbezeichnungen ergänzt, wenn das
    Impressum sie direkt dahinter führt (gleiche oder nächste Zeile, nur Berufswörter):
    „… PartG mbB Steuerberater" (§ 2 PartGG). Bei Einzelkanzleien nicht – dort ist
    „Steuerberater" unter dem Namen die Berufsbezeichnung des Inhabers.
  - Discovery-Daten werden nie überschrieben; Unterschiede (normalisiert verglichen)
    stehen in der Spalte „Abweichung". Kandidatenfelder werden nur ergänzt, wenn leer.
  - Personen: **Grundlage sind die strukturell erkannten Personen** (Teamseiten, JSON-LD,
    Impressum); das LLM ergänzt Titel, Funktionen und weitere Personen. Zusammengeführt
    wird nur bei gleichem Nachnamen **und** gleichem Vornamen bzw. passender Initiale
    („W. Drews" = „Wiebke Drews", aber ≠ „Sven Drews"; „Drews" allein nie) oder wenn ein
    Vorname Teil des anderen ist („Alexander Hamdorf" = „Johann-Alexander Hamdorf"); beide
    Reihenfolgen gelten („Schreiner, Andreas", „Schreiner Andreas" = „Andreas Schreiner"),
    ausgegeben wird „Vorname Nachname". Funktionswörter, Artikel und Titel vor dem Namen wandern in
    Funktion bzw. Titel („Geschäftsführer Andreas Schröder", „den Geschäftsführer Diplom-Kaufmann Andreas
    Paasch"). Ein Eintrag, dessen Funktion eine andere Person nennt („Forderungsmanagement Flensburg
    (Rechtsanwältin Ina Reese)"), ist eine Themenseite, keine Person. E-Mail-Adressen
    in einer Funktion bleiben nur, wenn der lokale Teil zum Namen passt.
  - Ansprechpartner = nur **Berufsträger und Leitung**; Mehrfachnennungen werden
    vereinigt (Funktionen und Titel). Reihenfolge: Vertretungsberechtigte in der
    Reihenfolge des Impressum-Abschnitts „vertretungsberechtigt/vertreten durch", dann alle
    übrigen in der Reihenfolge der Teamseite (erste Nennung; Leitung und Berufsträger
    gleichrangig), zuletzt Personen nur aus der LLM-Liste. Ehemalige („Partner bis 01.01.2025",
    „ehemaliger Partner") zählen nicht als Leitung.
  - „Berufsträger (Anzahl)" zählt StB/WP/RA/vBP, Berufstitel nur mit Wortgrenze;
    Fach­angestellte, Fachwirt, Fachassistent, Fachgehilfe und Anwärter nie.
    Vertretungsberechtigte zählen, wenn im selben Impressum-Abschnitt genau **eine**
    Berufsbezeichnung steht – oder, bei einer Partnerschaft, wenn ihr Name genau einen
    Beruf nennt („Brandenburg und Partner mbB Steuerberater"; § 2 PartGG).
    Einzelkanzlei, Partnerschaft oder Name mit Berufsbezug („Steuerberatungsgesellschaft", „Steuerbüro")
    mit genau einem Ansprechpartner ohne erkannte Berufsangabe → 1 Berufsträger, Hinweis „Berufsträger
    abgeleitet" in „Abweichung".
  - Kein Kanzleiname gefunden, aber Ansprechpartner und ≥ 1 Berufsträger → Name aus dem
    Hauptansprechpartner: ein Berufsträger „Steuerberaterin Angelika Just" (Berufsbezeichnung aus
    Funktion/Titel), mehrere „Steuerkanzlei Köhn"; Hinweis „Name abgeleitet" in „Abweichung".
  - Vertretungsberechtigte im Impressum: „vertretungsberechtigt" (auch der Tippfehler
    „vertretungberechtigt" und Gender-Klammern wie „(r)"), „vertreten durch",
    „Geschäftsführer", „Inhaber" (Namen dürfen direkt folgen); „Partner", „Gesellschafter",
    „Vorstand" mit Doppelpunkt, mit Namen in derselben Zeile („Die Gesellschafter Yvonne
    Baur und …") oder als reine Überschrift („Die Partner"). Bei starken Schlüsselwörtern
    gilt auch eine kurze Überschrift ohne Doppelpunkt als Abschnittsanfang („Vertretungs-
    berechtigte Gesellschafter der … mbB"), außer sie sieht wie Navigation aus (mehrere
    Menüwörter). Namen in derselben Zeile (getrennt durch Komma, „/", „und", auch „Name
    (Funktion)") oder in den Folgezeilen; sie enden an der nächsten Leerzeile, einer Zeile
    ohne Namen, einer Zeile, die mit einem Schlüsselwort beginnt, oder einer Kammer-/
    Versicherungszeile. Eine Zeile, die mit dem Schlüsselwort beginnt („Geschäftsführer: …"),
    zählt auch im Fremd-Abschnitt direkt hinter den Kammer-Adressen; ein Satz dort
    („… Versicherung AG, vertreten durch den Vorstand …") nicht. Eine
    Berufsbezeichnung direkt am Namen („Yvonne Baur, Steuerberaterin") gilt für diese Person.
    Paragrafenangaben („gemäß § 18 Abs. 2 MStV", „im Sinne des DDG") stören die
    Überschriften-Erkennung nicht. „Verantwortlich gemäß § 5 TMG/§ 18 MStV" zählt nachrangig (nach den
    Vertretungsberechtigten). Rahmenzeilen zählen nie.
  - Name: Steht im Impressum-Kopf eine Zeile mit allen Wörtern des LLM-Namens und
    höchstens drei weiteren (Rechtsform, Berufswörter, Namen), hat sie Vorrang vor Logo und
    Startseite („900 GRAD STEUERBERATUNG" → „900 GRAD Steuerberatungsgesellschaft PartmbB");
    auch über zwei kurze Zeilen („Steuerberater" / „Andreas Schreiner"), nie mit Menüwörtern.
    Vorrang hat eine Zeile irgendwo im Impressum (auch im Kammer-/Haftpflicht-Abschnitt,
    auch hinter „Firma:"), die den Namen enthält und als Zusatz eine Rechtsform.
  - Rechtsform per Regel **vor dem LLM**: aus dem Kanzleinamen, dann aus dem Impressum-Kopf
    (ohne Fremd-Abschnitte). „mbB", „PartmbB", „und Partner mbB" → PartG mbB. Ohne
    Gesellschaftsform: Einzelperson mit Berufstitel im Namen („Steuerberater Andreas
    Schreiner", „Steuerbüro Thomas Schütt", auch „Steuerberater Schreiner") oder genau ein
    Vertreter ohne Registernummer (HRB/PR …; „keine Eintragung im Handelsregister" zählt nicht)
    → Einzelkanzlei. Einzelkanzlei mit einem Berufsträger und höchstens einer Person
    auf den Teamseiten: Teamgröße 1; „Steuerberater Schreiner" + Person „Andreas Schreiner" →
    „Steuerberater Andreas Schreiner". Partnerschaftsregister/„PR 427" → PartG, mit „mbB" oder
    „beschränkter Berufshaftung" irgendwo im Impressum → PartG mbB. Kurzformen (AG, KG, UG,
    OHG, GbR) nur in der Namenszeile („AG Kiel" ist das Amtsgericht); eine LLM-Rechtsform nur
    mit Beleg in Name oder Impressum. Kanonisch nach `config/rechtsformen.yaml`; Ort laut OpenPLZ
    („Hamburg-Rissen" → „Hamburg").
  - Adresszusatz („/ Haus 8 Süd", eigene Zeile mit Haus, Gebäude, Eingang, Etage, OG,
    Hinterhof, c/o; auch in Klammern, mehrteilig oder nach einem Zeilenumbruch hinter
    „/") steht im eigenen Feld; die PLZ-Zeile darf bis zu zwei Zeilen unter der Straße
    stehen. Findet die Regel nur PLZ/Ort, wird die Straße des LLM zur selben PLZ genommen
    (ebenfalls in Straße und Zusatz zerlegt).
  - Teamgröße: genannte Zahl (LLM, dann Regel) > **alle** Personen der Teamseiten (≥ 2; Untergrenze, eine
    größere Klasse aus dem Text geht vor) > Klasse aus dem Text.
    Die Zahl ist nie kleiner als die Berufsträger: sonst Klasse ab der Berufsträgerzahl („mindestens 7
    Berufsträger", danrevision: Teamseite nur eines Standorts).
  - Größenklassen (`groesse.py`): Obergrenzen in `extract.groessenklassen` (Standard 1, 5, 10, 20, 50,
    100). Das LLM-Schema erzwingt die aktuellen Klassen. Gespeicherte Zeilen werden beim Laden neu
    eingeordnet: aus der Zahl; ohne Zahl aus der Text-Basis („über 35" → 21-50) oder, wenn der alte
    Bereich in genau eine neue Klasse fällt, umgerechnet; sonst leer mit Hinweis in „Abweichung".
    LLM-Aufzeichnungen mit alter Klasse gelten nur, wenn eindeutig umrechenbar; `fixture-eval`
    markiert Sollwerte der alten Einteilung, `recherche fixture-klassen-umstellen [--probe]` stellt die
    erwartet.json unter `echt/` um (mit Zahl → aus der Zahl; ohne Zahl → nur eindeutig umrechenbare
    alte Klassen, sonst leer mit Vorschlag aus dem Seitentext; Sicherung `erwartet.json.bak`).
- **Kanzlei-Erkennung** (`extract/kanzlei.py`, `extract.kanzlei_pruefung`, **bis Pilot-Ende aus**):
  Magazine, Jobbörsen, Branchenbücher, Hochschulen haben oft ein sauberes Verlags-Impressum.
  Kanzlei ist, wer **mindestens eins** erfüllt (ODER): Steuerberaterkammer im Impressum (§ 5 DDG,
  wichtigster Anker); Berufsträger erkannt (Impressum oder Teamseite); Rechtsform aus
  `extract.kanzlei_rechtsformen` (PartG mbB, PartG, Einzelkanzlei); Kanzleiname mit Steuer…, StB,
  Kanzlei, Treuhand, Revision, Wirtschaftsprüf… (kein Domain-Name wie „11880-steuerberater.com",
  kein Portal-Titel wie „Steuerberater in Kiel"); Titel/Snippet eines Suchtreffers mit
  „Steuerberat…", „Steuerkanzlei", „StB". Name, Rechtsform und Snippet zählen **nicht**, wenn die
  Stammdomain (alle Subdomains und Unterseiten aller Kandidaten zusammen; Baukasten-Hoster wie
  wixsite.com je Subdomain) in ≥ 5 Suchorten vorkommt, und nicht ohne echten Kanzleinamen. Sonst Status `keine Kanzlei`, Grund vorn in „Abweichung"
  („Keine Kanzlei: …"), Konfidenz höchstens `mittel`. Nichts wird gelöscht. Nur Kandidaten mit
  Website; ein von Hand gepflegter Status bleibt. `recherche kanzlei-pruefung` wendet die
  Prüfung ohne LLM auf bereits extrahierte Kanzleien an (auch zurück zu `neu`). Big-Four-
  Gesellschaften („… Wirtschaftsprüfungsgesellschaft", Berufsträger) gelten als Kanzlei – die
  gehören auf die Blocklist.
  **Vorab keine Kanzlei**, auch wenn ein Merkmal zutrifft: Domain auf der Blocklist (Kammern, Verbände,
  Portale – auch bei schon extrahierten Zeilen); „Lohnsteuerhilfe…"/„e. V." im Namen (Grund
  „Lohnsteuerhilfeverein"); reine Rechtsanwaltskanzlei (Anwalt/Notar in Name oder Impressum, aber nirgends
  Steuerberater, Steuerberaterkammer, WP/vBP – auch nicht bei den Ansprechpartnern; gemischte StB + RA bleiben);
  „Name abgeleitet aus Ansprechpartner" ohne Steuerberaterkammer und ohne Berufsbezeichnung im Impressum
  (Gemeinden, Ortsvereine, Praxen). Name abgeleitet ohne Kammer → Konfidenz immer `niedrig`.
- **Bundesland-Filter**: Liegt die PLZ einer Kanzlei laut OpenPLZ nicht im Bundesland des Laufs, wird
  der Status `außerhalb Bundesland` (auch ohne Website; `keine Kanzlei` geht vor). Ebenso **ohne gültige deutsche
  PLZ** (keine, nicht fünfstellig oder laut OpenPLZ unbekannt; nicht bei unerreichbarem OpenPLZ) – nie im Hauptblatt,
  „Außerhalb: keine deutsche PLZ" in „Abweichung" – und bei **ausländischer Domain** (.at, .ch, .li, .tirol, .wien,
  .swiss, .lu): „Außerhalb: ausländische Domain (.at)", in `extract` ohne LLM. Nichts wird gelöscht –
  bei einem bundesweiten Lauf sind sie für ihr eigenes Land richtig. `recherche kanzlei-pruefung`
  wendet den Filter auf bestehende Zeilen an. Schon **vor dem Crawl**: Nennen die Suchtreffer
  (Kandidaten-/Fund-PLZ, „01067 Dresden" in Titel oder Snippet) nur PLZ außerhalb, wird nicht gecrawlt
  („außerhalb Schleswig-Holstein laut Suchtreffer (01067 Dresden – Sachsen)" im Blatt „Nicht
  gefunden"); eine PLZ im Land oder keine erkennbare PLZ → crawlen.
- **Vorprüfung ohne LLM**: Vor dem LLM-Aufruf wird die Adresse per Regel (Discovery, Impressum)
  zusammengeführt und die PLZ geprüft. Findet die strenge Adressregel nichts (PLZ und Ort in getrennten
  Zeilen, Fußzeilen mit Trennzeichen – im Gesamtlauf SH der Normalfall), zählen alle PLZ mit Ort aus
  Impressum, Kontakt- und Startseite ohne Fremd-Abschnitte (Kammer, Versicherung, Hoster) und ohne
  Register-/Kontonummern: liegen alle bekannten außerhalb, entsteht die Zeile ohne LLM (Status
  `außerhalb Bundesland`, PLZ/Ort/Bundesland aus dem Text, Hinweis „ohne LLM: …", Konsole „…, ohne LLM").
  Eine PLZ im Land → LLM entscheidet. `extract -v` protokolliert die Vorprüfung je Kandidat. Ein
  unerwarteter Fehler eines Kandidaten markiert nur diesen als Fehler, der Lauf geht weiter.
- `recherche extract --alle [--ja]`: alle Kanzleien neu extrahieren (nach Regeländerungen), mit
  Rückfrage und Zeitschätzung aus dem Lauf-Protokoll (sonst 15 s je Kanzlei); fortsetzbar, gepflegte
  Status bleiben. Erst nach einem Gesamtlauf ausführen.
- Kandidaten **ohne Website** kommen mit ihren Discovery-Daten ins Blatt „Kanzleien"
  („Website vorhanden" = nein).

### Fixtures für die Extraktion (`fixtures.py`)

- `tests/fixtures/kanzleiseiten/<beispiele|echt>/<domain>/`: `start/impressum/kontakt/
  team/team2.html`, `meta.json`, `erwartet.json` (von Hand, `_status: fertig`),
  `llm/<modell>.json` (Aufzeichnungen; in den Beispielen von Hand geschrieben:
  `llm/handgeschrieben_<modell>.json`). JSON wird mit `utf-8-sig` gelesen (PowerShell-BOM).
- **Echte Kanzleiseiten nur lokal** unter `echt/` (`.gitignore`); committet werden nur
  die fiktiven Beispiele. Tests überspringen fehlende echte Fixtures.
- `recherche fixture-add --domain X` holt die Seiten (robots.txt, Drosselung) und legt
  die Vorlage an. `recherche fixture-eval [--live] [--modell M]` misst die Trefferquote
  je Feld, mittlere Laufzeit und Token je Kanzlei; `--live` fragt Ollama und zeichnet
  auf – nur für `echt/`, außer mit `--nur beispiele`. `--offline` prüft PLZ/Ort ohne OpenPLZ.
  Name und Ansprechpartner werden offline nur über Aufzeichnungen bewertet.
- Ziel: 13 von 15 echten Fixtures korrekt für Name, Adresse, Telefon und ersten
  Ansprechpartner.

### Nachverarbeitung (`normalize.py`)

- Telefon ins `+49`-Format, PLZ/Ort-Check gegen OpenPLZ-API (kostenlos, eine Anfrage
  je PLZ, gecacht), Bundesland aus PLZ. OpenPLZ nicht erreichbar → „nicht verifiziert".
  Ort in OpenPLZ-Schreibweise („AHRENSBURG" → „Ahrensburg"; „Wyk/Insel Föhr" → „Wyk auf Föhr", wenn
  genau ein Ort zur PLZ mit demselben ersten Wort). Telefonnummern werden beim Laden neu normalisiert
  (ältere Zeilen); `kanzlei-pruefung` frischt Ort, Bundesland und Verifizierung aus dem OpenPLZ-Cache auf.
- Dubletten über Domain, Telefon, Adresse und PLZ+Name-Token (`search/dedupe.py`).

### Excel (`export.py`)

- Blatt „Kanzleien": Autofilter, feste Kopfzeile, Dropdown für Status; Spalten u. a.
  „Website vorhanden", „Berufsträger (Anzahl)", „Abweichung". Eine erneute Extraktion
  setzt einen gepflegten Status nicht zurück.
- Blatt „Nicht gefunden": Domains ohne lesbares Impressum oder mit Fehler, mit Grund und
  Discovery-Daten.
- Blatt „Keine Kanzlei" (nur wenn es solche Zeilen gibt): alle Spalten wie „Kanzleien", Grund
  vorn – zum Zurückholen von Hand. Diese Zeilen fehlen im Blatt „Kanzleien".
- Blatt „Außerhalb Bundesland" (nur wenn es solche Zeilen gibt): alle Spalten wie „Kanzleien".
- Blatt „Dubletten". `kanzlei-pruefung` trägt dort zusätzlich **einen Verdacht je Gruppe** ein, wenn
  gleiches Telefon **und** gleicher Hauptansprechpartner mehrere Stammdomains verbinden (Behalten =
  Zentrale, Dublette = je fremder Stammdomain eine Domain; egido.de / egido-flensburg.de …). Standorte
  derselben Stammdomain (Subdomains) sind keine Dubletten. Die Einträge werden bei jedem Lauf ersetzt.
- Spalte „Zentrale/Standort" (hinter dem Kanzleinamen, `extract/standorte.py`): Zeilen derselben
  Stammdomain (Subdomains je Niederlassung) oder mit gleichem Telefon + Ansprechpartner sind eine
  Kanzlei mit mehreren Standorten; Zentrale ist die Zeile auf der gemeinsamen Stammdomain
  („Zentrale (21 Standorte)" / „Standort von SHBB …"). Daneben Spalte „Zentrale" mit der Hauptdomain
  für alle Zeilen der Gruppe. Nur beim Export berechnet, kein Schema.
  **Weitere Domains** derselben Kanzlei (gleicher Name, Telefon, Hauptansprechpartner und Adresse;
  Knüppel & Partner mit 14 Orts-Domains) sind keine Standorte: In der Versandliste steht nur die
  Zentrale („+ 13 weitere Domains: …"); im Blatt „Dubletten" ein Eintrag „weitere domains /
  zusammengeführt". Standorte mit eigener Adresse (SHBB) bleiben eigene Zeilen.
- Blatt „Lauf-Protokoll": Suchbegriff, Trefferzahl, neue Kandidaten, Anfragen, Kosten, Laufzeit.

### CLI (`cli.py`)

`recherche search | fill | coverage | crawl | extract | export | run | search-compare | orte |
blocklist-vorschlag | kanzlei-pruefung | robots-check | suche-auswertung | offen | fixture-add | fixture-eval | fixture-prompt |
fixture-klassen-umstellen`,
jeweils mit `--bundesland` und `--db`. **Jeder Schritt muss nach Abbruch
wiederaufnehmbar sein**: Fortschritt steht in SQLite (Status je Domain und
Schritt, erledigte Queries im Lauf-Protokoll); ein erneuter Aufruf macht nur das,
was noch offen ist.

### Speicher (`storage.py`)

SQLite-Tabellen: `kandidaten` (eigene ID, Domain optional), `funde` (jeder Treffer je
Quelle), `api_cache` (auch robots.txt und OpenPLZ), `seiten` (HTML gzip + Text),
`kandidat_seiten` (Seitentyp → URL je Kandidat), `kanzleien` (je Kandidat),
`nicht_gefunden`, `dubletten`, `lauf_protokoll` (inkl. Anfragen und Kosten).
Schemaversion über `PRAGMA user_version` (aktuell 4, `adresszusatz`); ältere Datenbanken werden beim
Öffnen schrittweise migriert (jeder Schritt in einer Transaktion).

## Arbeitsweise

- **Plan vor Umsetzung:** Vor jeder größeren Änderung Plan mit Dateiliste und
  offenen Fragen vorlegen und abstimmen.
- **Kleine Commits** mit klaren Nachrichten, ein Thema je Commit.
- **pytest muss grün sein** vor jedem Commit (`pytest` im Ordner `kanzlei-recherche/`).
- Tests hängen nicht an den lokal gepflegten Dateien in `config/` (Ortsliste, Referenz, Budget, num_ctx):
  eigene Test-Konfigurationen oder Werte aus der geladenen Konfiguration.
- **Tests nur gegen Fixtures** (`tests/fixtures/`): keine Netzwerkzugriffe (in `conftest.py` gesperrt – ein echter
  httpx-Abruf schlägt fehl), kein
  Ollama, keine Suchmaschine in Tests. Externe Clients werden injiziert und in
  Tests durch Fakes mit gespeicherten Beispielantworten ersetzt.
- **Live-Läufe nur über `cli.py run`** (bzw. die einzelnen CLI-Befehle), nie aus
  Tests heraus.
- `data/` (Datenbank, Exporte) und `.env` gehören nicht ins Git.
- Testdaten sind fiktiv (erfundene Namen, `.example`-Domains, fiktive Durchwahlen),
  nur Orte/PLZ sind echt.

## Stand

- Stufe 1 (Gerüst): Modelle, SQLite-Speicher, Excel-Export, CLI.
- Discovery-Sitzung: Ergebnisformat und Dubletten-Logik über Adresse/Telefon, Brave- und
  Google-CSE-Adapter, `coverage`, `fill`, `search-compare` für alle aktiven Adapter.
  Platzhalter: `serpapi`, `places` (stillgelegt); Verzeichnisse mit Parsern (aus Kiel-Seiten), aktiv.
- Stufe 2 (Lesen und Extrahieren): `crawl`, `extract` (Regeln + Ollama), Zusammenführung
  mit Abweichung und Konfidenz, OpenPLZ-Prüfung, Schema v3, `fixture-add`,
  `fixture-eval` mit 6 fiktiven Beispielen. Die 15 echten Fixtures folgen (lokal).
- Nach dem ersten echten Fixture-Lauf: Prompt-Aufbau ohne Abschneiden, `fixture-prompt`,
  Personen- und Mitarbeiterzahl-Regeln, Adresszusatz (Schema v4), kanonische
  Rechtsform und Ort, Berufsträger- und Ansprechpartner-Reihenfolge.
- Fixture-Runde mit 12 echten Kanzleien (lokal, `echt/`): Kernfelder Name, Adresse, Telefon,
  erster Ansprechpartner 12/12; Rechtsform 12/12; rund 15 s je Kanzlei mit qwen3.5:9b.
  Modellvergleich auf RTX 5060: qwen3.5:9b am schnellsten und genauesten (gemma4:26b 43 s,
  qwen3.6:27b 126 s, beide nicht genauer) – **qwen3.5:9b bleibt Standard**. Die harten Fehler
  waren Regelfehler, keine Modellfehler; Regeln haben bei Name, Personen, Rechtsform und
  Telefon Vorrang vor dem LLM.
- **Pilot SH** (80 Startorte): 1.169 Kandidaten, 907 Websites gelesen; bei echten SH-Kanzleien Adresse
  ~97 %, Telefon ~98 %, E-Mail ~95 %, Ansprechpartner ~90 %, Teamgröße 56 %; Stichprobe 15/15 korrekt.
  Probleme: 15–25 % Portale/Jobbörsen (→ Kanzlei-Erkennung, blocklist-vorschlag), 184 Zeilen außerhalb
  SH (→ Bundesland-Filter), robots.txt-Fehler als Sperre gewertet (→ RFC-9309-Parser, DNS getrennt,
  Lesen trotz nicht abrufbarer robots.txt). Schema bleibt v4.
- `orte`: vollständige Gemeindeliste (OpenPLZ + GV-ISys), Einwohnerregel mit
  Seitenbegrenzung, Zusatz-Suchorte für Kiel/Lübeck. Die erzeugte Liste ersetzt die
  Startliste erst nach `recherche orte --force`.
