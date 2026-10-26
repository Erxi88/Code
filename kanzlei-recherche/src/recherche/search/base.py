"""Gemeinsames Interface aller Discovery-Quellen.

Zwei Arten von Adaptern teilen sich Ergebnisformat, Konfiguration, Budget und Kostenlog:

- QueryAdapter (Stufe A, Websuche): search(query, max_results) – je Ort mit mehreren
  Query-Varianten aus queries.yaml.
- OrtsAdapter (Stufe B, Lückenfüller): search_ort(ort) – läuft nur für übergebene Orte.

Ein Adapter kennt nur seine Quelle (Anfrage, Antwort → SearchHit, Drosselung, Retry,
Cache). Blocklist, Normalisierung und Dubletten liegen in pipeline.py/dedupe.py und
gelten für alle Adapter gleich.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, ClassVar, Protocol

from pydantic import BaseModel

from ..config import QuelleConfig

if TYPE_CHECKING:
    from .pipeline import Ort


class SearchHit(BaseModel):
    """Einheitliches Ergebnisformat aller Quellen."""

    quelle: str  # Name des Adapters, z. B. "brave"
    quell_id: str  # stabile ID innerhalb der Quelle (URL, place_id, Eintrags-ID)
    name: str | None = None  # Kanzleiname bzw. Seitentitel bei Websuche
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    telefon: str | None = None
    website: str | None = None
    snippet: str | None = None
    roh_url: str | None = None  # Fundstelle (Trefferseite, Verzeichniseintrag)


class SearchError(RuntimeError):
    """Eine Anfrage ist endgültig fehlgeschlagen (nach allen Retries); der Lauf geht weiter."""


class LimitErreicht(SearchError):
    """Weitere Anfragen sind sinnlos – der Lauf wird sauber beendet und kann später
    wiederaufgenommen werden (Tageslimit, Kontingent, Budget, ungültiger Key)."""


class KontingentErschoepft(LimitErreicht):
    """Tages- oder Monatskontingent der Quelle aufgebraucht."""


class BudgetErschoepft(LimitErreicht):
    """max_anfragen_je_lauf erreicht."""


class ZugangVerweigert(LimitErreicht):
    """Key ungültig, fehlende Freischaltung oder Zugriff gesperrt."""


class Cache(Protocol):
    def cache_get(self, adapter: str, schluessel: str) -> dict | None: ...

    def cache_put(self, adapter: str, schluessel: str, antwort: dict) -> None: ...


class SearchAdapter(ABC):
    name: ClassVar[str]
    stufe: ClassVar[str]  # "A" (Websuche) oder "B" (Lückenfüller)
    implementiert: ClassVar[bool] = True

    def __init__(self, cfg: QuelleConfig, cache: Cache | None = None):
        self.cfg = cfg
        self.cache = cache if cfg.cache else None
        self.anfragen = 0  # tatsächlich gesendete Anfragen (für Kostenlog und Budget)
        self.max_anfragen: int | None = None

    # -------------------------------------------------------------- Status

    def fehlende_keys(self) -> list[str]:
        """Namen fehlender Umgebungsvariablen (API-Keys)."""
        return []

    def nicht_konfiguriert_grund(self) -> str | None:
        """None, wenn der Adapter einsatzbereit ist, sonst ein Grund."""
        if fehlend := self.fehlende_keys():
            return f"{', '.join(fehlend)} fehlt in .env"
        if not self.implementiert:
            return "Adapter noch nicht implementiert (Platzhalter)"
        return None

    @property
    def ist_konfiguriert(self) -> bool:
        return self.nicht_konfiguriert_grund() is None

    @property
    def aktiv(self) -> bool:
        return self.cfg.aktiv

    # -------------------------------------------------------------- Kosten

    def kosten(self, anfragen: int | None = None) -> float:
        n = self.anfragen if anfragen is None else anfragen
        return round(n * self.cfg.kosten_je_1000 / 1000, 4)

    def _anfrage_zaehlen(self) -> None:
        """Vor jeder echten (bezahlten) Anfrage aufrufen; prüft das Budget."""
        if self.max_anfragen is not None and self.anfragen >= self.max_anfragen:
            raise BudgetErschoepft(
                f"{self.name}: Budget von {self.max_anfragen} Anfragen je Lauf erreicht"
            )
        self.anfragen += 1


class QueryAdapter(SearchAdapter):
    stufe = "A"
    # True: liefert Ergebnisse seitenweise (bezahlte Anfragen je Seite) – die Pipeline kann
    # dann nach jeder Seite entscheiden, ob sich die nächste lohnt (Seitenregel je Einwohnerklasse)
    blaettert: ClassVar[bool] = False

    @property
    def seitengroesse(self) -> int:
        """Treffer je Seite; ohne echtes Paging die übliche Größe einer Ergebnisseite."""
        return 20

    @abstractmethod
    def search(self, query: str, max_results: int) -> list[SearchHit]:
        """Führt eine Query aus. SearchError: Query gescheitert; LimitErreicht: Lauf beenden."""

    def seiten(self, query: str, max_results: int) -> Iterator[list[SearchHit]]:
        """Ergebnisseiten einzeln; ohne Paging eine einzige Seite."""
        yield self.search(query, max_results)


class OrtsAdapter(SearchAdapter):
    stufe = "B"

    @abstractmethod
    def search_ort(self, ort: Ort) -> list[SearchHit]:
        """Alle Treffer der Quelle für einen Ort (Rubrik, Raster oder PLZ-Suche)."""


class OrtsPlatzhalter(OrtsAdapter):
    """Gemeinsame Basis der noch nicht gebauten Stufe-B-Adapter."""

    implementiert = False

    def search_ort(self, ort: Ort) -> list[SearchHit]:
        raise NotImplementedError(f"{self.name}: Adapter folgt (Stufe B)")
