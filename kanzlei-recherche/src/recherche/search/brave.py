"""Brave Search API – Platzhalter bis zur Suche-Sitzung.

Vorgesehen: API-Key aus .env (BRAVE_API_KEY), bis 20 Treffer je Anfrage (count=20).
"""

from __future__ import annotations

from ..config import BraveConfig, env
from .base import SearchAdapter, SearchHit

API_URL = "https://api.search.brave.com/res/v1/web/search"
ENV_KEY = "BRAVE_API_KEY"


class BraveAdapter(SearchAdapter):
    name = "brave"
    implementiert = False

    def __init__(self, cfg: BraveConfig | None = None):
        self.cfg = cfg or BraveConfig()
        self.api_key = env(ENV_KEY)

    def nicht_konfiguriert_grund(self) -> str | None:
        if not self.api_key:
            return f"{ENV_KEY} fehlt in .env"
        if not self.implementiert:
            return "Adapter noch nicht implementiert (folgt in der Suche-Sitzung)"
        return None

    def search(self, query: str, max_results: int) -> list[SearchHit]:
        raise NotImplementedError("Brave-Adapter folgt in der Suche-Sitzung")
