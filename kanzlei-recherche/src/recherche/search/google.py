"""Google Custom Search JSON API – Platzhalter bis zur Suche-Sitzung.

Vorgesehen: API-Key und Search-Engine-ID aus .env (GOOGLE_CSE_API_KEY, GOOGLE_CSE_ID),
10 Treffer je Anfrage (Parameter num), Paging über start=1, 11, 21 bis maximal
max_per_query (30) Treffer je Query.
"""

from __future__ import annotations

from ..config import GoogleConfig, env
from .base import SearchAdapter, SearchHit

API_URL = "https://www.googleapis.com/customsearch/v1"
ENV_KEY = "GOOGLE_CSE_API_KEY"
ENV_CX = "GOOGLE_CSE_ID"


class GoogleCseAdapter(SearchAdapter):
    name = "google"
    implementiert = False

    def __init__(self, cfg: GoogleConfig | None = None):
        self.cfg = cfg or GoogleConfig()
        self.api_key = env(ENV_KEY)
        self.cx = env(ENV_CX)

    def seiten_starts(self, max_results: int) -> list[int]:
        """Start-Indizes fürs Paging (1-basiert), begrenzt auf max_per_query."""
        gesamt = min(max_results, self.cfg.max_per_query)
        return list(range(1, gesamt + 1, self.cfg.per_page))

    def nicht_konfiguriert_grund(self) -> str | None:
        fehlend = [n for n, w in ((ENV_KEY, self.api_key), (ENV_CX, self.cx)) if not w]
        if fehlend:
            return f"{', '.join(fehlend)} fehlt in .env"
        if not self.implementiert:
            return "Adapter noch nicht implementiert (folgt in der Suche-Sitzung)"
        return None

    def search(self, query: str, max_results: int) -> list[SearchHit]:
        raise NotImplementedError("Google-Adapter folgt in der Suche-Sitzung")
