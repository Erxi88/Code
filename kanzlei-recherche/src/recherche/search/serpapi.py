"""SerpAPI (Google-Trefferseite) – Platzhalter für den Pilot-Vergleich in search-compare.

Vorgesehen: Key aus .env (SERPAPI_KEY), engine=google, gl=de, hl=de, google_domain=google.de,
num=10, Paging über start bis max_per_query; Treffer aus organic_results (link, title, snippet).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from ..config import SerpApiConfig, env
from .base import SearchHit
from .http_base import HttpQueryAdapter, Retry

ENV_KEY = "SERPAPI_KEY"


class SerpApiAdapter(HttpQueryAdapter):
    name = "serpapi"
    implementiert = False
    cfg: SerpApiConfig

    def __init__(self, cfg: SerpApiConfig | None = None, **kwargs):
        super().__init__(cfg or SerpApiConfig(), **kwargs)
        self.api_key = env(ENV_KEY)

    def fehlende_keys(self) -> list[str]:
        return [] if self.api_key else [ENV_KEY]

    def search(self, query: str, max_results: int) -> list[SearchHit]:
        raise NotImplementedError("SerpAPI-Adapter folgt (Pilot-Vergleich)")

    def _seiten(self, query: str, max_results: int) -> Iterator[tuple[dict, dict]]:
        raise NotImplementedError

    def _treffer(self, antwort: dict) -> list[SearchHit]:
        raise NotImplementedError

    def _hat_mehr(self, antwort: dict, treffer: list[SearchHit], erwartet: int) -> bool:
        raise NotImplementedError

    def _pruefe(self, resp: httpx.Response) -> Retry | None:
        raise NotImplementedError
