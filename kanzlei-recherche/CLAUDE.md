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
`1-5` / `6-15` / `16-50` / `>50`), Teamgröße-Basis (woraus abgeleitet),
Konfidenz (`hoch` / `mittel` / `niedrig`), Quell-URLs, Abrufdatum, Status.

Status-Werte (Excel-Dropdown): `neu` / `geprüft` / `kontaktiert` / `kein Interesse` / `ungültig`.

**Konfidenz-Regel (vorläufig, wird nach dem Pilot überprüft):**
- `hoch`: Impressum gelesen und PLZ/Ort gegen OpenPLZ verifiziert
- `mittel`: Kernfelder (Name, Straße, PLZ, Ort, Telefon oder E-Mail) vollständig, aber nicht verifiziert
- `niedrig`: Kernfelder fehlen

## Technische Regeln

Keine laufenden Kosten, kein API-Key für den Standardbetrieb.

- **Stack:** Python 3.12+, Typer-CLI, httpx (async), BeautifulSoup/trafilatura,
  Pydantic v2, SQLite, openpyxl, pytest.
- **Konfiguration:** Alles Konfigurierbare (Modell, Ollama-URL, Ortsliste,
  Ausgabepfad, Parallelität, Suchadapter, Drosselung) steht in
  `config/settings.yaml`, damit derselbe Code lokal und auf einem Mietserver läuft.
  Geheimnisse (API-Keys) nur in `.env` (nie committen, Vorlage `.env.example`).

### Suche (`src/recherche/search/`)

- Austauschbare Adapter über ein gemeinsames Interface (`search/base.py`,
  `SearchAdapter`). Der aktive Adapter wird per `search.adapter` in
  `settings.yaml` gewählt.
- Alle Adapter liefern dasselbe Ergebnisformat `SearchHit`
  (`url`, `titel`, `snippet`, `quelle`).
- Adapter:
  1. `ddgs` (DuckDuckGo, ohne Key) – Standard für lokale Tests und den Pilot.
     Drosselung (Pause zwischen Anfragen + Zufallsanteil) und Retry mit
     exponentiellem Backoff, weil DuckDuckGo bei vielen Anfragen bremst.
  2. `google` (Custom Search JSON API) – API-Key und Search-Engine-ID aus `.env`
     (`GOOGLE_CSE_API_KEY`, `GOOGLE_CSE_ID`), 10 Treffer je Anfrage, Paging bis
     maximal 30 Treffer je Query. **Bis zur Suche-Sitzung nur Platzhalter.**
  3. `brave` (Brave Search API) – API-Key aus `.env` (`BRAVE_API_KEY`), bis 20
     Treffer je Anfrage. **Bis zur Suche-Sitzung nur Platzhalter.**
- Je Ort mehrere Query-Varianten aus `config/queries.yaml`.
- Blocklist (`config/domain_blocklist.txt`: Portale, Verzeichnisse, Social Media)
  und Dubletten-Logik (jede Domain genau einmal als Kandidat) liegen
  **außerhalb der Adapter** (`search/pipeline.py`), damit sie für alle gleich gelten.
- `recherche search-compare --orte Kiel,Flensburg,…` lässt alle konfigurierten
  Adapter für die genannten Orte laufen und zeigt: Kanzlei-Domains je Adapter,
  Überschneidung, nur-in-A / nur-in-B. Entscheidungsgrundlage nach dem Pilot, ob
  DuckDuckGo für den Gesamtlauf reicht.

### Crawling (`crawl.py`)

- Startseite laden, Links sammeln, Impressum-/Kontakt-/Team-Seite über Linktext und
  Pfad erkennen (`/impressum`, `/kontakt`, `/team`, `/ueber-uns`, `/kanzlei`,
  „Unser Team", „Mitarbeiter").
- Max. 1 Request/Sekunde je Domain, viele Domains parallel, Timeout 15 s,
  2 Retries, robots.txt beachten.
- Alle Rohseiten als Text in SQLite cachen – eine URL wird nie zweimal geladen.

### Extraktion (`extract/`)

- Austauschbar über ein Interface (`extract/base.py`), damit später weitere Anbieter
  ergänzt werden können.
- Stufe 1 regelbasiert (`rules.py`): PLZ, Ort, Straße, Telefon, E-Mail aus dem
  Impressum (Regex).
- Stufe 2 LLM über Ollama (`ollama.py`, lokal `http://localhost:11434`): Name,
  Rechtsform, Ansprechpartner, Teamgröße.
  - JSON-Schema **immer** über den Ollama-Parameter `format` erzwingen.
  - Thinking-Modus **immer** abschalten (`"think": false`), bei allen Modellen.
  - Modellname aus `settings.yaml`. Lokal: aktuelles Qwen-3-Modell der 8B-Klasse,
    Fallback `llama3.1:8b`. GPU-Server: Qwen 3.6 27B (dicht), Alternative
    Gemma 4 26B-A4B. Umschalten nach dem Fixture-Vergleich.
- Fehlende Felder bleiben `null` – **nie raten**.

### Nachverarbeitung (`normalize.py`)

- Telefon ins `+49`-Format, PLZ/Ort-Check gegen OpenPLZ-API (kostenlos),
  Bundesland aus PLZ.
- Dubletten über Domain, Telefon und PLZ+Name-Token zusammenführen.

### Excel (`export.py`)

- Blatt „Kanzleien": Autofilter, feste Kopfzeile, Dropdown für Status.
- Blatt „Nicht gefunden": Domains ohne lesbares Impressum oder mit Fehler, mit Grund.
- Blatt „Dubletten".
- Blatt „Lauf-Protokoll": Suchbegriff, Trefferzahl, Laufzeit.

### CLI (`cli.py`)

`recherche search | crawl | extract | export | run | search-compare | orte`,
jeweils mit `--bundesland` und `--db`. **Jeder Schritt muss nach Abbruch
wiederaufnehmbar sein**: Fortschritt steht in SQLite (Status je Domain und
Schritt, erledigte Queries im Lauf-Protokoll); ein erneuter Aufruf macht nur das,
was noch offen ist.

### Speicher (`storage.py`)

SQLite-Tabellen: `kandidaten`, `seiten` (Rohseiten-Cache), `kanzleien`,
`nicht_gefunden`, `dubletten`, `lauf_protokoll`. Schemaversion über
`PRAGMA user_version`.

## Arbeitsweise

- **Plan vor Umsetzung:** Vor jeder größeren Änderung Plan mit Dateiliste und
  offenen Fragen vorlegen und abstimmen.
- **Kleine Commits** mit klaren Nachrichten, ein Thema je Commit.
- **pytest muss grün sein** vor jedem Commit (`pytest` im Ordner `kanzlei-recherche/`).
- **Tests nur gegen Fixtures** (`tests/fixtures/`): keine Netzwerkzugriffe, kein
  Ollama, keine Suchmaschine in Tests. Externe Clients werden injiziert und in
  Tests durch Fakes mit gespeicherten Beispielantworten ersetzt.
- **Live-Läufe nur über `cli.py run`** (bzw. die einzelnen CLI-Befehle), nie aus
  Tests heraus.
- `data/` (Datenbank, Exporte) und `.env` gehören nicht ins Git.
- Testdaten sind fiktiv (erfundene Namen, `.example`-Domains, fiktive Durchwahlen),
  nur Orte/PLZ sind echt.

## Stand

- Stufe 1 (Gerüst): Modelle, SQLite-Speicher, Excel-Export, CLI, Such-Interface mit
  ddgs-Adapter und `search-compare`. `crawl`, `extract`, `orte` sind Platzhalter;
  Google- und Brave-Adapter nur Interface + Konfiguration.
