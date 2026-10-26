"""Fake-Clients und -Adapter für Tests (keine Netzwerkzugriffe)."""

from __future__ import annotations

from recherche.search.base import SearchAdapter, SearchError, SearchHit


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.pausen: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.pausen.append(s)
        self.t += s


class FakeDdgsClient:
    """Liefert gespeicherte ddgs-Antworten; optional zuerst eine Folge von Fehlern."""

    def __init__(self, antworten: dict[str, list[dict]], fehler: list[Exception] | None = None):
        self.antworten = antworten
        self.fehler = list(fehler or [])
        self.aufrufe: list[tuple[str, dict]] = []

    def text(self, query: str, **kwargs):
        self.aufrufe.append((query, kwargs))
        if self.fehler:
            raise self.fehler.pop(0)
        return self.antworten.get(query, [])[: kwargs.get("max_results") or None]


class FakeAdapter(SearchAdapter):
    """Adapter mit fester Domain-Liste je Query."""

    def __init__(self, name: str, treffer: dict[str, list[str]], grund: str | None = None,
                 fehler_bei: set[str] | None = None):
        self.name = name
        self.treffer = treffer
        self.grund = grund
        self.fehler_bei = fehler_bei or set()
        self.aufrufe: list[str] = []

    def nicht_konfiguriert_grund(self) -> str | None:
        return self.grund

    def search(self, query: str, max_results: int) -> list[SearchHit]:
        self.aufrufe.append(query)
        if query in self.fehler_bei:
            raise SearchError(f"{self.name}: Fehler bei {query}")
        return [SearchHit(url=u, titel=None, snippet=None, quelle=self.name)
                for u in self.treffer.get(query, [])[:max_results]]
