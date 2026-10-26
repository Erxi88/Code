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
| Crawling, Extraktion (Regeln + Ollama), Normalisierung | Platzhalter |
| `orte` (Ortsliste aus OpenPLZ, Einwohner aus GV-ISys) | Platzhalter; Startliste mit 80 Orten |

## Installation

Ubuntu (lokal oder Mietserver):

```bash
./setup.sh                      # Python 3.12, .venv, Ollama, Modell aus settings.yaml
./setup.sh --modell qwen3.6:27b # anderes Modell laden (GPU-Server)
./setup.sh --ohne-ollama        # nur Python
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

recherche crawl   -b SH                     # Websites laden (Platzhalter)
recherche extract -b SH                     # Daten extrahieren (Platzhalter)
recherche export  -b SH [--out liste.xlsx]  # Excel schreiben
recherche run     -b SH [--orte …] [--adapter ddgs]  # Stufe A + crawl + extract + export
recherche orte    -b SH                     # Ortsliste (Platzhalter)
```

**Wiederaufnahme:** Jeder Schritt speichert seinen Fortschritt in SQLite, jede API-Antwort
wird gecacht. Nach einem Abbruch (auch Tages-/Monatslimit oder Budget) denselben Befehl
erneut starten: erledigte Abfragen werden übersprungen, nichts wird doppelt bezahlt.

**Kosten:** Jeder Lauf gibt die Zahl der gesendeten Anfragen und die geschätzten Kosten
aus (`kosten_je_1000` je Adapter); `max_anfragen_je_lauf` (Standard 1000) begrenzt das
Budget je Lauf und Adapter. Die Werte stehen auch im Blatt „Lauf-Protokoll".

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
  (`neu / geprüft / kontaktiert / kein Interesse / ungültig`)
- **Nicht gefunden**: Domain und Grund
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
