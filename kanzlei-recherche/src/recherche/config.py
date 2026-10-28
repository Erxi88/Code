"""Laden von config/settings.yaml und .env.

Relative Pfade in settings.yaml gelten ab dem Projektordner (Elternordner von config/).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

PROJEKT_DIR = Path(__file__).resolve().parents[2]
STANDARD_CONFIG = PROJEKT_DIR / "config" / "settings.yaml"


class BundeslandConfig(BaseModel):
    name: str
    orte_datei: str


class PfadConfig(BaseModel):
    db: str = "data/recherche.sqlite"
    ausgabe: str = "data/export/kanzleien_{bundesland}_{datum}.xlsx"
    queries: str = "config/queries.yaml"
    blocklist: str = "config/domain_blocklist.txt"
    referenz: str = "config/referenz_kammern.yaml"
    impressum_ausschluss: str = "config/impressum_ausschluss.yaml"
    fixtures_beispiele: str = "tests/fixtures/kanzleiseiten/beispiele"
    fixtures_echt: str = "tests/fixtures/kanzleiseiten/echt"  # nur lokal, in .gitignore


# ---------------------------------------------------------------------- Quellen


class QuelleConfig(BaseModel):
    """Gemeinsame Einstellungen aller Discovery-Quellen."""

    aktiv: bool = False
    pause_s: float = 1.0  # Mindestabstand zwischen zwei Anfragen
    pause_jitter_s: float = 0.0  # zusätzliche Zufallspause 0..jitter
    retries: int = 3
    backoff_s: float = 5.0  # Wartezeit vor Retry n: backoff_s * 2**(n-1)
    timeout_s: float = 15.0
    kosten_je_1000: float = 0.0  # für das Kostenlog
    cache: bool = True  # API-Antworten in SQLite cachen (Wiederaufnahme ohne Doppelkosten)


class DdgsConfig(QuelleConfig):
    aktiv: bool = True
    region: str = "de-de"
    backend: str = "duckduckgo"
    safesearch: str = "moderate"
    pause_s: float = 3.0
    pause_jitter_s: float = 2.0
    backoff_s: float = 20.0
    cache: bool = False


class BraveConfig(QuelleConfig):
    url: str = "https://api.search.brave.com/res/v1/web/search"
    per_page: int = 20  # Brave: count max. 20
    max_per_query: int = 60  # Paging über offset (Seitenindex 0..9)
    country: str = "DE"
    search_lang: str = "de"
    safesearch: str = "moderate"
    pause_s: float = 1.1
    kosten_je_1000: float = 5.0


class GoogleCseConfig(QuelleConfig):
    url: str = "https://www.googleapis.com/customsearch/v1"
    per_page: int = 10  # Google: num max. 10
    max_per_query: int = 30
    gl: str = "de"
    cr: str = "countryDE"
    hl: str = "de"
    kosten_je_1000: float = 5.0
    freikontingent_tag: int = 100


class SerpApiConfig(QuelleConfig):
    url: str = "https://serpapi.com/search.json"
    per_page: int = 10
    max_per_query: int = 30
    gl: str = "de"
    hl: str = "de"
    kosten_je_1000: float = 15.0


class PlacesConfig(QuelleConfig):
    url: str = "https://places.googleapis.com/v1/places:searchText"
    text_query: str = "Steuerberater"
    included_type: str = "accounting"
    max_treffer_je_zelle: int = 60  # Text Search (New): bis 3 Seiten à 20
    zelle_km: float = 5.0  # Startgröße der Rasterzellen
    min_zelle_km: float = 0.5  # nicht feiner teilen
    kosten_je_1000: float = 32.0


class VerzeichnisConfig(QuelleConfig):
    rubrik: str = "Steuerberater"
    pause_s: float = 10.0
    pause_jitter_s: float = 5.0


class QuellenConfig(BaseModel):
    brave: BraveConfig = Field(default_factory=lambda: BraveConfig(aktiv=True))
    google_cse: GoogleCseConfig = Field(default_factory=GoogleCseConfig)
    serpapi: SerpApiConfig = Field(default_factory=SerpApiConfig)
    ddgs: DdgsConfig = Field(default_factory=DdgsConfig)
    places: PlacesConfig = Field(default_factory=PlacesConfig)
    gelbeseiten: VerzeichnisConfig = Field(default_factory=VerzeichnisConfig)
    dasoertliche: VerzeichnisConfig = Field(default_factory=VerzeichnisConfig)
    elf880: VerzeichnisConfig = Field(default_factory=VerzeichnisConfig)
    stb_verzeichnis: VerzeichnisConfig = Field(
        default_factory=lambda: VerzeichnisConfig(pause_s=30.0, pause_jitter_s=15.0)
    )

    def get(self, name: str) -> QuelleConfig:
        cfg = getattr(self, name, None)
        if not isinstance(cfg, QuelleConfig):
            raise KeyError(name)
        return cfg

    def namen(self) -> list[str]:
        return list(type(self).model_fields)


class EinwohnerStufe(BaseModel):
    """Wie gründlich ein Ort gesucht wird. varianten/seiten None = alle bzw. bis max_results_per_query."""

    unter: int | None = None  # gilt für Orte mit weniger Einwohnern (None = alle übrigen)
    varianten: int | None = None  # erste N Vorlagen aus queries.yaml
    seiten: int | None = None  # Ergebnisseiten je Query

    @field_validator("varianten", "seiten", mode="before")
    @classmethod
    def _alle(cls, v):
        return None if v in ("alle", "all") else v


class WeiterWennVollConfig(BaseModel):
    """Seitenbegrenzung aussetzen, wenn die letzte erlaubte Seite voll war und überwiegend
    Kanzlei-Domains enthielt (keine Portale/Verzeichnisse laut Blocklist)."""

    aktiv: bool = True
    anteil_kanzlei_domains: float = 0.5


class StufeAConfig(BaseModel):
    adapter: str = "brave"
    max_results_per_query: int = 60  # harte Obergrenze je Query
    einwohner_regel: list[EinwohnerStufe] = Field(
        default_factory=lambda: [
            EinwohnerStufe(unter=2000, varianten=1, seiten=1),
            EinwohnerStufe(unter=20000, varianten=2, seiten=2),
            EinwohnerStufe(varianten=None, seiten=3),
        ]
    )
    ohne_einwohner: EinwohnerStufe = Field(default_factory=lambda: EinwohnerStufe(seiten=3))
    stadtteil: EinwohnerStufe = Field(default_factory=lambda: EinwohnerStufe(seiten=2))
    weiter_wenn_voll: WeiterWennVollConfig = Field(default_factory=WeiterWennVollConfig)


class CoverageConfig(BaseModel):
    quote_ziel: float = 0.9  # Ort/Kreis gilt als Lücke, wenn gefunden < erwartet * quote_ziel
    top_orte: int = 15


class DiscoveryConfig(BaseModel):
    stufe_a: StufeAConfig = Field(default_factory=StufeAConfig)
    max_anfragen_je_lauf: int | None = 1000
    quellen: QuellenConfig = Field(default_factory=QuellenConfig)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)


# ---------------------------------------------------------------------- Rest


class CrawlConfig(BaseModel):
    parallel_domains: int = 10
    requests_per_second_per_domain: float = 1.0
    timeout_s: float = 15.0
    retries: int = 2
    backoff_s: float = 2.0
    respect_robots: bool = True
    user_agent: str = "kanzlei-recherche/0.1"
    max_seiten_je_domain: int = 6
    min_text_zeichen: int = 300  # darunter gilt eine Seite als „ohne Text" (JavaScript)
    playwright: bool = False  # Rückfall für JavaScript-Seiten, nur wenn aktiviert
    playwright_timeout_s: float = 20.0
    # Teamseite trotz großem HTML fast ohne Text → mit Playwright nachladen (nur mit playwright: true)
    playwright_team_min_html: int = 20_000
    playwright_team_min_anteil: float = 0.02
    max_bytes: int = 3_000_000


class LlmConfig(BaseModel):
    provider: str = "ollama"
    url: str = "http://localhost:11434"
    modell: str = "qwen3.5:9b"
    fallback_modell: str | None = "llama3.1:8b"
    think: bool = False
    temperature: float = 0.0
    timeout_s: float = 300.0
    num_ctx: int = 16384  # Kontextfenster; Ollama-Standard ist zu klein und schneidet ab
    retries: int = 1  # erneuter Versuch bei ungültiger Antwort
    # Text aller Seiten zusammen; "auto" = passend zu num_ctx (siehe extract/prompt.py)
    max_text_chars: int | Literal["auto"] = "auto"


class ExtractConfig(BaseModel):
    # Kanzlei-Erkennung (Berufsträger, Kanzlei-Rechtsform, Kanzleiwort im Namen, Steuerberaterkammer):
    # sonst Status „keine Kanzlei", Konfidenz höchstens „mittel", eigenes Excel-Blatt
    kanzlei_pruefung: bool = False
    kanzlei_rechtsformen: list[str] = Field(default_factory=lambda: ["PartG mbB", "PartG", "Einzelkanzlei"])


class OpenPlzConfig(BaseModel):
    url: str = "https://openplzapi.org"
    page_size: int = 50
    pause_s: float = 0.5


class GvSpalten(BaseModel):
    """Spaltennamen im GV-ISys-Auszug (Vergleich ohne Groß/Klein, Leerzeichen, Bindestriche;
    Teilübereinstimmung genügt). Entweder `ags` oder land/rb/kreis/gem."""

    satzart: str | None = "satzart"
    textkennzeichen: str | None = "textkennzeichen"
    ags: str | None = "amtlicher gemeindeschlüssel"
    land: str | None = "land"
    rb: str | None = "rb"
    kreis: str | None = "kreis"
    gem: str | None = "gem"
    name: str = "gemeindename"
    einwohner: str = "insgesamt"
    kreisname: str | None = None  # nur bei CSV ohne Kreis-Zeilen (Satzart 40)


class GvIsysConfig(BaseModel):
    quelle: str | None = None  # Pfad oder URL (XLSX oder CSV)
    blatt: str | None = None  # XLSX-Blatt; Standard: erstes Blatt mit Spalte „Satzart"
    spalten: GvSpalten = Field(default_factory=GvSpalten)


class StadtteilConfig(BaseModel):
    """Zusatz-Suchorte je PLZ-Bezirk für große Städte („Steuerberater Kiel" bricht nach drei Seiten ab)."""

    aktiv: bool = True
    ab_einwohner: int = 100_000
    nur_kreisfrei: bool = True


class OrteConfig(BaseModel):
    gv_isys: GvIsysConfig = Field(default_factory=GvIsysConfig)
    stadtteile: StadtteilConfig = Field(default_factory=StadtteilConfig)


class Settings(BaseModel):
    standard_bundesland: str = "SH"
    bundeslaender: dict[str, BundeslandConfig] = Field(default_factory=dict)
    pfade: PfadConfig = Field(default_factory=PfadConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
    openplz: OpenPlzConfig = Field(default_factory=OpenPlzConfig)
    orte: OrteConfig = Field(default_factory=OrteConfig)

    # nicht aus YAML, sondern beim Laden gesetzt
    basis_dir: Path = PROJEKT_DIR

    def pfad(self, relativ: str | Path) -> Path:
        p = Path(relativ)
        return p if p.is_absolute() else self.basis_dir / p

    def db_pfad(self) -> Path:
        return self.pfad(self.pfade.db)

    def ausgabe_pfad(self, bundesland: str, datum: date | None = None) -> Path:
        datum = datum or date.today()
        return self.pfad(
            self.pfade.ausgabe.format(bundesland=bundesland, datum=datum.isoformat())
        )

    def bundesland(self, code: str) -> BundeslandConfig:
        try:
            return self.bundeslaender[code.upper()]
        except KeyError:
            bekannt = ", ".join(sorted(self.bundeslaender)) or "keine"
            raise ValueError(
                f"Bundesland {code!r} ist nicht in settings.yaml konfiguriert (bekannt: {bekannt})"
            ) from None


def load_settings(pfad: str | Path | None = None) -> Settings:
    """Lädt settings.yaml und – falls vorhanden – .env aus dem Projektordner."""
    pfad = Path(pfad) if pfad else STANDARD_CONFIG
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8-sig")) or {}
    basis = pfad.resolve().parent.parent
    load_dotenv(basis / ".env", override=False)
    return Settings(**daten, basis_dir=basis)


def env(name: str) -> str | None:
    """Liest eine Umgebungsvariable; leere Werte zählen als nicht gesetzt."""
    wert = os.environ.get(name, "").strip()
    return wert or None
