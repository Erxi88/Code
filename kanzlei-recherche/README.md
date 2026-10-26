# kanzlei-recherche

Automatisierte Recherche von Steuerberaterkanzleien je Bundesland mit dem Ziel maximaler
Vollständigkeit. Die Discovery vereinigt mehrere Quellen: zuerst eine Websuche je Ort
(Stufe A, Brave Search API), dann – nur für Orte unter der Referenzzahl – Lückenfüller
(Stufe B: Google Places, Branchenverzeichnisse, amtliches Verzeichnis). Danach werden die
Homepages (Impressum, Kontakt, Team) ausgelesen (lokales LLM über Ollama) und die Daten
landen in einer Excel-Liste.

Projektregeln und Arbeitsweise: siehe [CLAUDE.md](CLAUDE.md).

## Stand

| Baustein | Stand |
|---|---|
| Modelle, SQLite-Speicher (Schema v2 mit Migration), Excel-Export | fertig |
| Discovery: Interface, Pipeline, Dubletten-Logik (Domain, Telefon, Adresse, PLZ+Name) | fertig |
| Stufe A: Brave (Standard), Google CSE (nur Alt-Key), ddgs (Entwicklung) | fertig |
| Stufe A: SerpAPI | Platzhalter |
| Stufe B: Places (Raster fertig), Gelbe Seiten, Das Örtliche, 11880, amtl. Verzeichnis | Platzhalter |
| `coverage`, `fill`, `search-compare` (alle aktiven Adapter) | fertig |
| Crawling (robots.txt, Drosselung je Domain, Cache, Playwright-Rückfall) | fertig |
| Extraktion: Regeln + Ollama, Zusammenführung mit Abweichung, OpenPLZ, Konfidenz | fertig |
| `fixture-add`, `fixture-eval` (Trefferquote, Laufzeit, Token je Modell) | fertig; 3 fiktive Beispiele, echte Fixtures lokal |
| `orte` (Gemeinden aus OpenPLZ, Kreis/Einwohner aus GV-ISys, Zusatz-Suchorte Kiel/Lübeck) | fertig; bis zum ersten Lauf gilt die Startliste mit 80 Orten |
| Einwohnerregel: Query-Varianten und Seiten je Einwohnerklasse | fertig |

## Installation

Ubuntu (lokal oder Mietserver):

```bash
./setup.sh                      # Python 3.12, .venv, Ollama, Modell aus settings.yaml
./setup.sh --modell qwen3.6:27b # anderes Modell laden (GPU-Server)
./setup.sh --ohne-ollama        # nur Python
./setup.sh --mit-playwright     # zusätzlich Chromium für JavaScript-Seiten
source .venv/bin/activate
```

Manuell: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

## Bedienung

Alle Befehle kennen `--bundesland/-b` (Standard aus `settings.yaml`), `--db` und `--config`.

```bash
# Stufe A – Websuche je Ort (Standard: Brave)
recherche search  -b SH [--orte Kiel,Husum] [--adapter brave|google_cse|ddgs]
# Abdeckung prüfen
recherche coverage -b SH [--out coverage.xlsx]
# Stufe B – Lückenfüller, nie flächendeckend ohne --alle
recherche fill -b SH --quelle places (--luecken | --orte Kiel,Husum | --alle)
# Quellen vergleichen (alle aktiven Adapter oder Auswahl)
recherche search-compare --orte Kiel,Flensburg,Lübeck,Neumünster,Husum [--adapter brave,ddgs] [--out vergleich.csv]

# Stufe 2 – Websites lesen und Daten extrahieren
recherche crawl   -b SH [--limit 20] [--wiederholen]
recherche extract -b SH [--ohne-llm] [--modell qwen3.6:27b] [--limit 20] [--wiederholen]
recherche export  -b SH [--out liste.xlsx]  # Excel schreiben
recherche run     -b SH [--orte …] [--adapter ddgs]  # Stufe A + crawl + extract + export
# Vollständige Ortsliste (schreibt config/orte_SH.csv nur mit --force)
recherche orte    -b SH [--dry-run] [--force] [--out datei.csv] [--gv pfad-oder-url] [--ohne-gv]

# Extraktion an echten Kanzleiseiten messen (Fixtures nur lokal, siehe unten)
recherche fixture-add  --domain kanzlei-beispiel.de
recherche fixture-eval [--live] [--modell gemma4:26b] [--nur beispiele|echt]
```

**Extraktion:** `crawl` liest für jeden Kandidaten mit Website Start-, Impressum-, Kontakt-
und Teamseite (1 Anfrage/s je Domain, robots.txt, Cache). `extract` zieht Adresse, Telefon
und E-Mail per Regeln aus dem Impressum und Name, Rechtsform, Personen und Teamgröße per
Ollama, prüft PLZ/Ort gegen OpenPLZ und führt alles mit den Discovery-Daten zusammen –
ohne diese zu überschreiben (Unterschiede stehen in „Abweichung"). Ist Ollama nicht
erreichbar, endet der Lauf sauber; `--ohne-llm` extrahiert nur per Regeln.

**Fixtures:** `fixture-add` legt die Seiten einer Domain unter
`tests/fixtures/kanzleiseiten/echt/<domain>/` ab (nicht im Git) und erzeugt eine
`erwartet.json`-Vorlage zum Ausfüllen. `fixture-eval --live` fragt Ollama und zeichnet die
Antworten je Modell auf; ohne `--live` wird mit den Aufzeichnungen gerechnet. Ausgabe:
Trefferquote je Feld, mittlere Laufzeit und Token je Kanzlei, Modellvergleich.

**Wiederaufnahme:** Jeder Schritt speichert seinen Fortschritt in SQLite, jede API-Antwort
wird gecacht. Nach einem Abbruch (auch Tages-/Monatslimit oder Budget) denselben Befehl
erneut starten: erledigte Abfragen werden übersprungen, nichts wird doppelt bezahlt.

**Kosten:** Jeder Lauf gibt die Zahl der gesendeten Anfragen und die geschätzten Kosten
aus (`kosten_je_1000` je Adapter); `max_anfragen_je_lauf` (Standard 1000) begrenzt das
Budget je Lauf und Adapter. Die Werte stehen auch im Blatt „Lauf-Protokoll".

**Ortsliste:** `orte` lädt alle Gemeinden des Bundeslandes aus OpenPLZ und ergänzt Kreis und
Einwohner aus dem Gemeindeverzeichnis (GV-ISys) von Destatis. Die Datei („Alle politisch
selbständigen Gemeinden", XLSX) einmal herunterladen und den Pfad (oder die URL) unter
`orte.gv_isys.quelle` eintragen. Erst `--dry-run` ansehen: Der Befehl zeigt den Vergleich
mit der bisherigen Liste und eine Kostenabschätzung je Einwohnerstufe.

**Einwohnerregel:** Kleine Orte werden mit weniger Query-Varianten und Ergebnisseiten
gesucht (`discovery.stufe_a.einwohner_regel`), außer die Ergebnisseite ist voll mit
Kanzlei-Websites – dann wird weitergeblättert. Reihenfolge in `queries.yaml` beachten.

**coverage** zeigt gefundene Kanzleien je Kreis und Quelle, den Anteil der Kanzleien, die
nur eine Quelle kennt, und – sobald `config/referenz_kammern.yaml` ausgefüllt ist – Quote
und Lücke je Kreis sowie die Orte mit der größten Lücke (Eingabe für `fill --luecken`).

## Konfiguration

- `config/settings.yaml`: Bundesländer und Ortslisten, Pfade, Discovery (Stufe-A-Adapter,
  Budget, je Quelle `aktiv`, Drosselung, Retry, `kosten_je_1000`), Crawl-Parallelität,
  LLM-Modell und Ollama-URL.
- `config/referenz_kammern.yaml`: Referenzzahlen für `coverage` (`kanzleien` oder
  `mitglieder` mit `faktor`, Land gesamt und optional je Kreis).
- `config/queries.yaml`: Query-Vorlagen mit `{ort}`, `{plz}`, `{kreis}`.
- `config/domain_blocklist.txt`: Portale, Verzeichnisse, Social Media (Subdomains inklusive).
- `config/orte_SH.csv`: `ort;plz;kreis;einwohner`, eine Zeile je Ort (`einwohner` optional,
  wird später von `orte` aus GV-ISys befüllt).
- `.env` (aus `.env.example`): `BRAVE_API_KEY` (Stufe A), optional `GOOGLE_CSE_API_KEY` +
  `GOOGLE_CSE_ID`, `SERPAPI_KEY`, `GOOGLE_PLACES_API_KEY`.

### LLM-Modelle

| Einsatz | Modell (Ollama-Tag) |
|---|---|
| lokal, Standard | `qwen3.5:9b` (neuestes Qwen-3-Modell der 8B-Klasse) |
| lokal, Fallback | `llama3.1:8b` |
| GPU-Server, Standard | `qwen3.6:27b` (dicht) |
| GPU-Server, Alternative | `gemma4:26b` (Gemma 4 26B-A4B, MoE; Ollama ≥ 0.20) |

Bei allen Modellen wird der Thinking-Modus abgeschaltet (`think: false`) und das
JSON-Schema über den Ollama-Parameter `format` erzwungen.

## Ausgabe

Excel mit vier Blättern:
- **Kanzleien**: Autofilter, fixierte Kopfzeile, Status-Dropdown
  (`neu / geprüft / kontaktiert / kein Interesse / ungültig`); u. a. „Website vorhanden",
  „Berufsträger (Anzahl)", „Abweichung" (Discovery vs. Website). Auch Kanzleien ohne
  Website (aus Stufe B) stehen hier.
- **Nicht gefunden**: Domain und Grund plus Discovery-Daten (Name, Adresse, Telefon) zum
  Nachpflegen von Hand
- **Dubletten**: welcher Datensatz behalten wurde, welcher zusammengeführt oder nur
  verdächtig ist, Kriterium und Art (`zusammengeführt` / `unsicher` / `verdacht`)
- **Lauf-Protokoll**: Suchbegriff, Trefferzahl, neue Kandidaten, Anfragen, Kosten, Laufzeit, Fehler

## Entwicklung

```bash
pytest
```

Tests laufen nur gegen Fixtures in `tests/fixtures/` (u. a. gespeicherte Brave- und
CSE-Antworten): keine Netzwerkzugriffe, kein Ollama.
Die 20 Testkanzleien sind erfunden (`.example`-Domains), nur Orte und PLZ sind echt.
