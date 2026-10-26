"""Fake-Clients und -Adapter für Tests (keine Netzwerkzugriffe)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from recherche.config import QuelleConfig
from recherche.search.base import OrtsAdapter, QueryAdapter, SearchError, SearchHit

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_json(*teile: str) -> dict:
    return json.loads(FIXTURES.joinpath(*teile).read_text(encoding="utf-8"))


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


class FakeHttp:
    """httpx.MockTransport mit Antwortfolge: jede Anfrage nimmt die nächste (Status, JSON, Header).

    Statt einer Folge kann auch eine Funktion request → (Status, JSON, Header) übergeben werden.
    """

    def __init__(self, antworten):
        self.antworten = antworten if callable(antworten) else list(antworten)
        self.anfragen: list[httpx.Request] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        if callable(self.antworten):
            antwort = self.antworten(request)
        else:
            antwort = self.antworten.pop(0)
        if isinstance(antwort, Exception):
            raise antwort
        status, daten, *rest = antwort
        return httpx.Response(status, json=daten, headers=rest[0] if rest else None)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handler))

    def params(self, i: int = -1) -> dict[str, str]:
        return dict(self.anfragen[i].url.params)


def hit(quelle: str, url: str | None = None, **felder) -> SearchHit:
    return SearchHit(quelle=quelle, quell_id=felder.pop("quell_id", url or felder.get("name") or "x"),
                     website=url, roh_url=url, **felder)


class FakeQueryAdapter(QueryAdapter):
    """Websuche mit fester Trefferliste je Query (URLs oder fertige SearchHits)."""

    def __init__(self, name: str, treffer: dict[str, list], grund: str | None = None,
                 fehler_bei: dict[str, Exception] | None = None, aktiv: bool = True, kosten_je_1000: float = 0.0):
        super().__init__(QuelleConfig(aktiv=aktiv, kosten_je_1000=kosten_je_1000))
        self.name = name
        self.treffer = treffer
        self.grund = grund
        self.fehler_bei = fehler_bei or {}
        self.aufrufe: list[str] = []

    def nicht_konfiguriert_grund(self) -> str | None:
        return self.grund

    def search(self, query: str, max_results: int) -> list[SearchHit]:
        self.aufrufe.append(query)
        self._anfrage_zaehlen()
        if query in self.fehler_bei:
            raise self.fehler_bei[query]
        return [t if isinstance(t, SearchHit) else hit(self.name, t)
                for t in self.treffer.get(query, [])][:max_results]


class FakeOrtsAdapter(OrtsAdapter):
    """Lückenfüller mit fester Trefferliste je Ortsname."""

    def __init__(self, name: str, treffer: dict[str, list[SearchHit]], grund: str | None = None,
                 fehler_bei: set[str] | None = None, aktiv: bool = True):
        super().__init__(QuelleConfig(aktiv=aktiv))
        self.name = name
        self.treffer = treffer
        self.grund = grund
        self.fehler_bei = fehler_bei or set()
        self.aufrufe: list[str] = []

    def nicht_konfiguriert_grund(self) -> str | None:
        return self.grund

    def search_ort(self, ort) -> list[SearchHit]:
        self.aufrufe.append(ort.name)
        self._anfrage_zaehlen()
        if ort.name in self.fehler_bei:
            raise SearchError(f"{self.name}: Fehler bei {ort.name}")
        return list(self.treffer.get(ort.name, []))
