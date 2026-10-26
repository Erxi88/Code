"""Brave Search API (Web Search) – Standardquelle für Stufe A.

- Key aus .env (BRAVE_API_KEY), Header X-Subscription-Token
- bis 20 Treffer je Anfrage (count), Paging über offset = Seitenindex (0..9)
- country=DE, search_lang=de
- Retry mit Backoff bei 5xx und 429 (Ratenlimit je Sekunde)
- 429 mit Monatskontingent 0 bzw. 402: Kontingent erschöpft → Lauf endet sauber
- 401/403: Key ungültig → Lauf endet sauber
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import httpx

from ..config import BraveConfig, env
from .base import KontingentErschoepft, SearchError, SearchHit, ZugangVerweigert
from .http_base import HttpQueryAdapter, Retry, fehlertext, text_ohne_html, url_id

ENV_KEY = "BRAVE_API_KEY"
MAX_OFFSET = 9  # Brave erlaubt Seitenindex 0..9


def _monatsrest(resp: httpx.Response) -> int | None:
    """Restkontingent des Monats aus X-RateLimit-Remaining ('<je Sekunde>, <je Monat>')."""
    roh = resp.headers.get("X-RateLimit-Remaining")
    if not roh:
        return None
    teile = [t.strip() for t in roh.split(",")]
    try:
        return int(teile[-1]) if len(teile) >= 2 else None
    except ValueError:
        return None


class BraveAdapter(HttpQueryAdapter):
    name = "brave"
    cfg: BraveConfig

    def __init__(self, cfg: BraveConfig | None = None, **kwargs):
        super().__init__(cfg or BraveConfig(), **kwargs)
        self.api_key = env(ENV_KEY)

    def fehlende_keys(self) -> list[str]:
        return [] if self.api_key else [ENV_KEY]

    def _seiten(self, query: str, max_results: int) -> Iterator[tuple[dict, dict]]:
        per_page = min(self.cfg.per_page, 20)
        gesamt = min(max_results, self.cfg.max_per_query)
        seiten = min(math.ceil(gesamt / per_page), MAX_OFFSET + 1)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key or "",
        }
        for offset in range(seiten):
            yield (
                {
                    "q": query,
                    "count": per_page,
                    "offset": offset,
                    "country": self.cfg.country,
                    "search_lang": self.cfg.search_lang,
                    "safesearch": self.cfg.safesearch,
                    "result_filter": "web",
                },
                headers,
            )

    def _treffer(self, antwort: dict) -> list[SearchHit]:
        hits = []
        for r in (antwort.get("web") or {}).get("results") or []:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            hits.append(
                SearchHit(
                    quelle=self.name,
                    quell_id=url_id(url),
                    name=text_ohne_html(r.get("title")),
                    website=url,
                    snippet=text_ohne_html(r.get("description")),
                    roh_url=url,
                )
            )
        return hits

    def _hat_mehr(self, antwort: dict, treffer: list[SearchHit], erwartet: int) -> bool:
        mehr = (antwort.get("query") or {}).get("more_results_available")
        if mehr is False:
            return False
        return len(treffer) >= erwartet

    def _pruefe(self, resp: httpx.Response) -> Retry | None:
        s = resp.status_code
        if s == 200:
            return None
        text = fehlertext(resp)
        if s == 429:
            if _monatsrest(resp) == 0:
                raise KontingentErschoepft(f"brave: Monatskontingent erschöpft ({text})")
            return Retry(f"429 Ratenlimit ({text})", endgueltig=KontingentErschoepft)
        if s == 402:
            raise KontingentErschoepft(f"brave: Guthaben/Kontingent aufgebraucht ({text})")
        if s in (401, 403):
            raise ZugangVerweigert(f"brave: Zugriff verweigert – {ENV_KEY} prüfen ({text})")
        if s >= 500:
            return Retry(f"HTTP {s} ({text})")
        raise SearchError(f"brave: HTTP {s} ({text})")
