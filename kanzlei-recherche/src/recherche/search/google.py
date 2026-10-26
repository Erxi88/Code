"""Google Custom Search JSON API – nur für Alt-Keys.

Die API ist für Neukunden gesperrt (neue Projekte erhalten 403) und wird zum
01.01.2027 abgeschaltet. Der Adapter bleibt für den Fall eines bestehenden Keys.

- Keys aus .env (GOOGLE_CSE_API_KEY, GOOGLE_CSE_ID)
- 10 Treffer je Anfrage (num), Paging über start=1, 11, 21 bis max_per_query (30)
- Tageslimit (dailyLimitExceeded/quotaExceeded) → Lauf endet sauber, morgen weiter
- rateLimitExceeded (je Minute) und 5xx → Retry mit Backoff
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from ..config import GoogleCseConfig, env
from .base import KontingentErschoepft, SearchError, SearchHit, ZugangVerweigert
from .http_base import HttpQueryAdapter, Retry, fehlertext, text_ohne_html, url_id

ENV_KEY = "GOOGLE_CSE_API_KEY"
ENV_CX = "GOOGLE_CSE_ID"
MAX_START = 91  # Google liefert höchstens die ersten 100 Treffer (start + num <= 101)

TAGESLIMIT_GRUENDE = {"dailyLimitExceeded", "quotaExceeded", "dailyLimitExceededUnreg"}
MINUTENLIMIT_GRUENDE = {"rateLimitExceeded", "userRateLimitExceeded"}


def _gruende(resp: httpx.Response) -> set[str]:
    try:
        fehler = resp.json().get("error") or {}
    except ValueError:
        return set()
    gruende = {e.get("reason") for e in fehler.get("errors") or [] if e.get("reason")}
    for d in fehler.get("details") or []:
        if d.get("reason"):
            gruende.add(d["reason"])
    if fehler.get("status"):
        gruende.add(fehler["status"])
    return gruende


class GoogleCseAdapter(HttpQueryAdapter):
    name = "google_cse"
    cfg: GoogleCseConfig

    def __init__(self, cfg: GoogleCseConfig | None = None, **kwargs):
        super().__init__(cfg or GoogleCseConfig(), **kwargs)
        self.api_key = env(ENV_KEY)
        self.cx = env(ENV_CX)

    def fehlende_keys(self) -> list[str]:
        return [n for n, w in ((ENV_KEY, self.api_key), (ENV_CX, self.cx)) if not w]

    def seiten_starts(self, max_results: int) -> list[int]:
        """Start-Indizes fürs Paging (1-basiert), begrenzt auf max_per_query."""
        gesamt = min(max_results, self.cfg.max_per_query)
        return [s for s in range(1, gesamt + 1, self.cfg.per_page) if s <= MAX_START]

    def _seiten(self, query: str, max_results: int) -> Iterator[tuple[dict, dict]]:
        for start in self.seiten_starts(max_results):
            yield (
                {
                    "key": self.api_key or "",
                    "cx": self.cx or "",
                    "q": query,
                    "num": self.cfg.per_page,
                    "start": start,
                    "gl": self.cfg.gl,
                    "cr": self.cfg.cr,
                    "hl": self.cfg.hl,
                },
                {"Accept": "application/json"},
            )

    def _treffer(self, antwort: dict) -> list[SearchHit]:
        hits = []
        for r in antwort.get("items") or []:
            url = (r.get("link") or "").strip()
            if not url:
                continue
            hits.append(
                SearchHit(
                    quelle=self.name,
                    quell_id=url_id(url),
                    name=text_ohne_html(r.get("title")),
                    website=url,
                    snippet=text_ohne_html(r.get("snippet")),
                    roh_url=url,
                )
            )
        return hits

    def _hat_mehr(self, antwort: dict, treffer: list[SearchHit], erwartet: int) -> bool:
        if not (antwort.get("queries") or {}).get("nextPage"):
            return False
        return len(treffer) >= erwartet

    def _pruefe(self, resp: httpx.Response) -> Retry | None:
        s = resp.status_code
        if s == 200:
            return None
        gruende = _gruende(resp)
        text = fehlertext(resp)
        if gruende & TAGESLIMIT_GRUENDE or "per day" in text.lower():
            raise KontingentErschoepft(f"google_cse: Tageslimit erreicht – morgen erneut starten ({text})")
        if s == 429 or gruende & MINUTENLIMIT_GRUENDE:
            return Retry(f"Ratenlimit ({text})", endgueltig=KontingentErschoepft)
        if s in (401, 403):
            raise ZugangVerweigert(
                "google_cse: Zugriff verweigert. Die Custom Search JSON API ist für neue Projekte "
                f"gesperrt (Abschaltung 01.01.2027); nur Alt-Keys funktionieren ({text})"
            )
        if s >= 500:
            return Retry(f"HTTP {s} ({text})")
        raise SearchError(f"google_cse: HTTP {s} ({text})")
