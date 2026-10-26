"""Gemeinsames Interface aller Such-Adapter.

Ein Adapter kennt nur seine Suchquelle: Anfrage stellen, Antwort in SearchHit
umwandeln, Drosselung/Retry. Blocklist, Domain-Normalisierung und Dubletten liegen
in pipeline.py und gelten für alle Adapter gleich.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class SearchHit(BaseModel):
    url: str
    titel: str | None = None
    snippet: str | None = None
    quelle: str  # Name des Adapters, z. B. "ddgs"


class SearchError(RuntimeError):
    """Suche endgültig fehlgeschlagen (nach allen Retries)."""


class SearchAdapter(ABC):
    name: str

    def nicht_konfiguriert_grund(self) -> str | None:
        """None, wenn der Adapter einsatzbereit ist, sonst ein Grund (z. B. fehlender Key)."""
        return None

    @property
    def ist_konfiguriert(self) -> bool:
        return self.nicht_konfiguriert_grund() is None

    @abstractmethod
    def search(self, query: str, max_results: int) -> list[SearchHit]:
        """Führt eine Query aus. Wirft SearchError, wenn sie endgültig scheitert."""
