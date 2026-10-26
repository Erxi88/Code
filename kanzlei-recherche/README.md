# kanzlei-recherche

Automatisierte Web-Recherche von Steuerberaterkanzleien je Bundesland. Für jeden Ort wird
im Web nach Kanzleien gesucht, deren Homepages (Impressum, Kontakt, Team) werden ausgelesen
und die Daten landen in einer Excel-Liste. Keine laufenden Kosten, kein API-Key im
Standardbetrieb (DuckDuckGo-Suche über `ddgs`, lokales LLM über Ollama).

Projektregeln und Arbeitsweise: siehe [CLAUDE.md](CLAUDE.md).

## Stand (Stufe 1 – Gerüst)

| Baustein | Stand |
|---|---|
| Modelle, SQLite-Speicher | fertig |
| Excel-Export (4 Blätter) | fertig |
| Suche: Interface, Pipeline (Blocklist, Dubletten), ddgs-Adapter | fertig |
| Suche: Google-CSE-, Brave-Adapter | nur Interface + Konfiguration |
| `search-compare` | fertig |
| Crawling, Extraktion (Regeln + Ollama), Normalisierung | Platzhalter |
| `orte` (vollständige Ortsliste aus OpenPLZ) | Platzhalter; Startliste mit 80 Orten in `config/orte_SH.csv` |

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
recherche search  -b SH [--orte Kiel,Husum] [--adapter ddgs]   # Websuche → Kandidaten
recherche crawl   -b SH                                         # Websites laden (Platzhalter)
recherche extract -b SH                                         # Daten extrahieren (Platzhalter)
recherche export  -b SH [--out liste.xlsx]                      # Excel schreiben
recherche run     -b SH [--orte …] [--out …]                    # alles nacheinander
recherche search-compare --orte Kiel,Flensburg,Lübeck,Neumünster,Husum [--out vergleich.csv]
recherche orte    -b SH                                         # Ortsliste (Platzhalter)
```

**Wiederaufnahme:** Jeder Schritt speichert seinen Fortschritt in SQLite. Nach einem
Abbruch denselben Befehl erneut starten: Schon erfolgreiche Queries werden übersprungen,
fehlgeschlagene wiederholt.

**search-compare** lässt alle Adapter aus `search.compare_adapters` mit denselben Queries
laufen. Blocklist und Dubletten-Filter gelten für alle gleich. Ausgabe: Kanzlei-Domains je
Adapter und Ort, Überschneidung, nur-in-A / nur-in-B. Adapter ohne Key oder noch ohne
Implementierung werden mit Grund übersprungen. Der Vergleich schreibt nichts in die Datenbank.

## Konfiguration

- `config/settings.yaml`: Bundesländer und Ortslisten, Pfade, Such-Adapter samt Drosselung,
  Crawl-Parallelität, LLM-Modell und Ollama-URL.
- `config/queries.yaml`: Query-Vorlagen mit `{ort}`, `{plz}`, `{kreis}`.
- `config/domain_blocklist.txt`: Portale, Verzeichnisse, Social Media (Subdomains inklusive).
- `config/orte_SH.csv`: `ort;plz;kreis`, eine Zeile je Ort.
- `.env` (aus `.env.example`): nur für die optionalen Adapter
  `GOOGLE_CSE_API_KEY`, `GOOGLE_CSE_ID`, `BRAVE_API_KEY`.

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
- **Dubletten**: welche Domain behalten wurde, welche zusammengeführt, nach welchem Kriterium
- **Lauf-Protokoll**: Suchbegriff, Trefferzahl, neue Domains, Laufzeit, Fehler

## Entwicklung

```bash
pytest
```

Tests laufen nur gegen Fixtures in `tests/fixtures/`: keine Netzwerkzugriffe, kein Ollama.
Die 20 Testkanzleien sind erfunden (`.example`-Domains), nur Orte und PLZ sind echt.
