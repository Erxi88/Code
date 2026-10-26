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
- **Danach `recherche coverage`**: gefundene Kanzleien je Kreis gegen
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
  den Lauf sauber; ein erneuter Aufruf macht dort weiter. Jede API-Antwort wird in
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
- `recherche search-compare --orte …` vergleicht **alle aktiven Adapter** (oder
  `--adapter a,b,…`): Kanzleien je Adapter und Ort, Schnittmenge, „nur in diesem
  Adapter", Paar-Matrix. Treffer werden über die Dubletten-Logik zusammengefasst.

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
- Blatt „Lauf-Protokoll": Suchbegriff, Trefferzahl, neue Kandidaten, Anfragen, Kosten, Laufzeit.

### CLI (`cli.py`)

`recherche search | fill | coverage | crawl | extract | export | run | search-compare | orte`,
jeweils mit `--bundesland` und `--db`. **Jeder Schritt muss nach Abbruch
wiederaufnehmbar sein**: Fortschritt steht in SQLite (Status je Domain und
Schritt, erledigte Queries im Lauf-Protokoll); ein erneuter Aufruf macht nur das,
was noch offen ist.

### Speicher (`storage.py`)

SQLite-Tabellen: `kandidaten` (eigene ID, Domain optional), `funde` (jeder Treffer je
Quelle), `api_cache`, `seiten` (Rohseiten-Cache), `kanzleien`, `nicht_gefunden`,
`dubletten`, `lauf_protokoll` (inkl. Anfragen und Kosten). Schemaversion über
`PRAGMA user_version` (aktuell 2); ältere Datenbanken werden beim Öffnen migriert.

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

- Stufe 1 (Gerüst): Modelle, SQLite-Speicher, Excel-Export, CLI.
- Discovery-Sitzung: Ergebnisformat und Dubletten-Logik über Adresse/Telefon, Schema v2,
  Brave- und Google-CSE-Adapter fertig, `coverage`, `fill`, `search-compare` für alle
  aktiven Adapter. Platzhalter: `serpapi`, `places` (Raster fertig), `gelbeseiten`,
  `dasoertliche`, `elf880`, `stb_verzeichnis`.
- Noch Platzhalter: `crawl`, `extract`, `orte` (OpenPLZ + Einwohner aus GV-ISys).
