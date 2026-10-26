"""Gemeinsame HTTP-Mechanik der API-Adapter (Brave, Google CSE, SerpAPI, später Places).

Drosselung (Mindestabstand + Zufallsanteil), Retry mit exponentiellem Backoff,
Antwort-Cache in SQLite (kein zweites Bezahlen bei Wiederaufnahme), Zählen der
gesendeten Anfragen für Budget und Kostenlog.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from abc import abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx

from ..config import QuelleConfig
from .base import Cache, LimitErreicht, QueryAdapter, SearchError, SearchHit

log = logging.getLogger(__name__)

# Parameter, die nie in Cache-Schlüssel oder Logs landen
GEHEIME_PARAMETER = frozenset({"key", "api_key", "cx"})


@dataclass(frozen=True)
class Retry:
    """Ergebnis von _pruefe(): diese Antwort lohnt einen erneuten Versuch."""

    grund: str
    # wird geworfen, wenn auch der letzte Versuch so endet
    endgueltig: type[SearchError] = SearchError


def text_ohne_html(text: str | None) -> str | None:
    if not text:
        return None
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip() or None


def url_id(url: str) -> str:
    """Stabile Quell-ID für Web-Treffer: URL ohne Fragment und abschließenden Slash."""
    return url.split("#", 1)[0].rstrip("/")


class HttpQueryAdapter(QueryAdapter):
    def __init__(
        self,
        cfg: QuelleConfig,
        cache: Cache | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ):
        super().__init__(cfg, cache)
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._letzte_anfrage: float | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.cfg.timeout_s)
        return self._client

    # -------------------------------------------------------------- von Unterklassen

    @abstractmethod
    def _seiten(self, query: str, max_results: int) -> Iterator[tuple[dict, dict]]:
        """(Query-Parameter, Header) je Ergebnisseite, in Reihenfolge."""

    @abstractmethod
    def _treffer(self, antwort: dict) -> list[SearchHit]:
        """Treffer einer Ergebnisseite."""

    @abstractmethod
    def _hat_mehr(self, antwort: dict, treffer: list[SearchHit], erwartet: int) -> bool:
        """True, wenn sich die nächste Seite lohnt."""

    @abstractmethod
    def _pruefe(self, resp: httpx.Response) -> Retry | None:
        """None bei Erfolg, Retry bei vorübergehendem Fehler; wirft bei endgültigem Fehler."""

    # -------------------------------------------------------------- Ablauf

    def search(self, query: str, max_results: int) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for params, headers in self._seiten(query, max_results):
            antwort = self._hole(params, headers)
            neue = self._treffer(antwort)
            hits.extend(neue)
            if len(hits) >= max_results or not self._hat_mehr(antwort, neue, self._seitengroesse(params)):
                break
        return hits[:max_results]

    def _seitengroesse(self, params: dict) -> int:
        return int(params.get("count") or params.get("num") or 0)

    def _cache_schluessel(self, params: dict) -> str:
        return json.dumps(
            {k: v for k, v in sorted(params.items()) if k not in GEHEIME_PARAMETER}, ensure_ascii=False
        )

    def _drosseln(self) -> None:
        if self._letzte_anfrage is None:
            return
        pause = self.cfg.pause_s + self._rng.uniform(0, self.cfg.pause_jitter_s)
        rest = pause - (self._clock() - self._letzte_anfrage)
        if rest > 0:
            self._sleep(rest)

    def _hole(self, params: dict, headers: dict) -> dict:
        schluessel = self._cache_schluessel(params)
        if self.cache is not None and (gecacht := self.cache.cache_get(self.name, schluessel)) is not None:
            return gecacht
        versuche = self.cfg.retries + 1
        for versuch in range(1, versuche + 1):
            self._drosseln()
            self._anfrage_zaehlen()
            try:
                resp = self.client.get(self.cfg.url, params=params, headers=headers)
                retry = self._pruefe(resp)
            except httpx.TransportError as e:  # Timeout, Verbindungsfehler
                retry = Retry(f"{type(e).__name__}: {e}")
            finally:
                self._letzte_anfrage = self._clock()
            if retry is None:
                try:
                    antwort = resp.json()
                except ValueError as e:
                    raise SearchError(f"{self.name}: Antwort ist kein JSON ({resp.text[:100]!r})") from e
                if self.cache is not None:
                    self.cache.cache_put(self.name, schluessel, antwort)
                return antwort
            if versuch == versuche:
                raise retry.endgueltig(f"{self.name}: {retry.grund} (nach {versuche} Versuchen)")
            warte = self.cfg.backoff_s * 2 ** (versuch - 1)
            log.warning("%s: %s – Versuch %d/%d, warte %.0f s", self.name, retry.grund, versuch, versuche, warte)
            self._sleep(warte)
        raise AssertionError("unreachable")  # pragma: no cover


def fehlertext(resp: httpx.Response) -> str:
    """Kurzer Fehlertext aus einer JSON- oder Text-Antwort (ohne Geheimnisse)."""
    try:
        daten = resp.json()
    except ValueError:
        return resp.text[:200]
    fehler = daten.get("error", daten) if isinstance(daten, dict) else daten
    if isinstance(fehler, dict):
        return str(fehler.get("detail") or fehler.get("message") or fehler.get("code") or fehler)[:300]
    return str(fehler)[:300]


__all__ = ["HttpQueryAdapter", "LimitErreicht", "Retry", "fehlertext", "text_ohne_html", "url_id"]
