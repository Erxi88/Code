"""DuckDuckGo-Adapter über die Bibliothek ddgs (ohne API-Key).

DuckDuckGo bremst bei vielen Anfragen. Deshalb:
- Mindestpause zwischen zwei Anfragen (pause_s + Zufallsanteil pause_jitter_s),
- Retry mit exponentiellem Backoff bei Rate-Limit, Timeout und sonstigen ddgs-Fehlern.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any, Protocol

from ..config import DdgsConfig
from .base import SearchAdapter, SearchError, SearchHit

log = logging.getLogger(__name__)

# ddgs meldet „keine Treffer" als DDGSException mit diesem Text – das ist kein Fehler.
KEINE_TREFFER = "No results found."


class TextClient(Protocol):
    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]: ...


def _standard_client(cfg: DdgsConfig) -> TextClient:
    from ddgs import DDGS

    return DDGS(timeout=cfg.timeout_s)


def _ddgs_fehlerklassen() -> tuple[type[BaseException], ...]:
    try:
        from ddgs.exceptions import DDGSException

        return (DDGSException,)
    except ImportError:  # pragma: no cover
        return ()


class DdgsAdapter(SearchAdapter):
    name = "ddgs"

    def __init__(
        self,
        cfg: DdgsConfig | None = None,
        client: TextClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ):
        self.cfg = cfg or DdgsConfig()
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._letzte_anfrage: float | None = None
        # Fehler, bei denen ein Retry sinnvoll ist (RatelimitException/TimeoutException
        # sind Unterklassen von DDGSException)
        self._retry_fehler = _ddgs_fehlerklassen() + (TimeoutError, ConnectionError)

    @property
    def client(self) -> TextClient:
        if self._client is None:
            self._client = _standard_client(self.cfg)
        return self._client

    def _drosseln(self) -> None:
        if self._letzte_anfrage is None:
            return
        pause = self.cfg.pause_s + self._rng.uniform(0, self.cfg.pause_jitter_s)
        rest = pause - (self._clock() - self._letzte_anfrage)
        if rest > 0:
            self._sleep(rest)

    def search(self, query: str, max_results: int) -> list[SearchHit]:
        versuche = self.cfg.retries + 1
        for versuch in range(1, versuche + 1):
            self._drosseln()
            try:
                roh = self.client.text(
                    query,
                    region=self.cfg.region,
                    safesearch=self.cfg.safesearch,
                    max_results=max_results,
                    backend=self.cfg.backend,
                )
            except self._retry_fehler as e:
                self._letzte_anfrage = self._clock()
                if str(e).strip() == KEINE_TREFFER:
                    return []
                if versuch == versuche:
                    raise SearchError(f"ddgs: {query!r} nach {versuche} Versuchen: {e}") from e
                warte = self.cfg.backoff_s * 2 ** (versuch - 1)
                log.warning("ddgs %s (Versuch %d/%d), warte %.0f s", e, versuch, versuche, warte)
                self._sleep(warte)
                continue
            self._letzte_anfrage = self._clock()
            return [h for h in (self._hit(r) for r in roh or []) if h is not None]
        raise AssertionError("unreachable")  # pragma: no cover

    def _hit(self, roh: dict[str, Any]) -> SearchHit | None:
        url = (roh.get("href") or roh.get("url") or "").strip()
        if not url:
            return None
        return SearchHit(
            url=url,
            titel=(roh.get("title") or "").strip() or None,
            snippet=(roh.get("body") or "").strip() or None,
            quelle=self.name,
        )
