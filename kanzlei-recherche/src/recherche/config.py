"""Laden von config/settings.yaml und .env.

Relative Pfade in settings.yaml gelten ab dem Projektordner (Elternordner von config/).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

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


class DdgsConfig(BaseModel):
    region: str = "de-de"
    backend: str = "duckduckgo"
    safesearch: str = "moderate"
    timeout_s: int = 15
    pause_s: float = 3.0
    pause_jitter_s: float = 2.0
    retries: int = 3
    backoff_s: float = 20.0


class GoogleConfig(BaseModel):
    per_page: int = 10
    max_per_query: int = 30
    pause_s: float = 1.0


class BraveConfig(BaseModel):
    per_page: int = 20
    max_per_query: int = 20
    pause_s: float = 1.1


class SearchConfig(BaseModel):
    adapter: str = "ddgs"
    compare_adapters: list[str] = Field(default_factory=lambda: ["ddgs", "google", "brave"])
    max_results_per_query: int = 20
    ddgs: DdgsConfig = Field(default_factory=DdgsConfig)
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    brave: BraveConfig = Field(default_factory=BraveConfig)


class CrawlConfig(BaseModel):
    parallel_domains: int = 10
    requests_per_second_per_domain: float = 1.0
    timeout_s: int = 15
    retries: int = 2
    respect_robots: bool = True
    user_agent: str = "kanzlei-recherche/0.1"


class LlmConfig(BaseModel):
    provider: str = "ollama"
    url: str = "http://localhost:11434"
    modell: str = "qwen3.5:9b"
    fallback_modell: str | None = "llama3.1:8b"
    think: bool = False
    temperature: float = 0.0
    timeout_s: int = 120
    max_text_chars: int = 12000


class OpenPlzConfig(BaseModel):
    url: str = "https://openplzapi.org"


class Settings(BaseModel):
    standard_bundesland: str = "SH"
    bundeslaender: dict[str, BundeslandConfig] = Field(default_factory=dict)
    pfade: PfadConfig = Field(default_factory=PfadConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    openplz: OpenPlzConfig = Field(default_factory=OpenPlzConfig)

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
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
    basis = pfad.resolve().parent.parent
    load_dotenv(basis / ".env", override=False)
    return Settings(**daten, basis_dir=basis)


def env(name: str) -> str | None:
    """Liest eine Umgebungsvariable; leere Werte zählen als nicht gesetzt."""
    wert = os.environ.get(name, "").strip()
    return wert or None
