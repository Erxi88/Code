import random

import pytest
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
from fakes import FakeClock, FakeDdgsClient

from recherche.config import DdgsConfig
from recherche.search.base import SearchError
from recherche.search.ddgs import DdgsAdapter


def _adapter(antworten, fehler=None, **cfg):
    clock = FakeClock()
    client = FakeDdgsClient(antworten, fehler)
    cfg = DdgsConfig(**{"pause_s": 3.0, "pause_jitter_s": 0.0, "retries": 2, "backoff_s": 10.0, **cfg})
    adapter = DdgsAdapter(cfg, client=client, sleep=clock.sleep, clock=clock, rng=random.Random(1))
    return adapter, client, clock


def test_antwort_wird_zu_searchhits(ddgs_antworten):
    adapter, client, _ = _adapter(ddgs_antworten)
    hits = adapter.search("Steuerberater Kiel", 20)
    assert len(hits) == 7  # Treffer ohne URL fällt weg
    assert hits[0].url == "https://www.hansen-steuerberatung.example/"
    assert hits[0].titel == "Hansen & Petersen Steuerberater – Kiel"
    assert hits[0].snippet.startswith("Ihre Steuerberater in Kiel")
    assert {h.quelle for h in hits} == {"ddgs"}


def test_parameter_an_ddgs(ddgs_antworten):
    adapter, client, _ = _adapter(ddgs_antworten)
    adapter.search("Steuerberater Kiel", 5)
    query, kwargs = client.aufrufe[0]
    assert query == "Steuerberater Kiel"
    assert kwargs == {"region": "de-de", "safesearch": "moderate", "max_results": 5, "backend": "duckduckgo"}


def test_leere_antwort(ddgs_antworten):
    adapter, _, _ = _adapter(ddgs_antworten)
    assert adapter.search("gibt es nicht", 10) == []


def test_keine_treffer_ist_kein_fehler(ddgs_antworten):
    adapter, client, clock = _adapter(ddgs_antworten, fehler=[DDGSException("No results found.")])
    assert adapter.search("Steuerberater Kleinstdorf", 10) == []
    assert len(client.aufrufe) == 1
    assert clock.pausen == []


def test_drosselung_zwischen_anfragen(ddgs_antworten):
    adapter, _, clock = _adapter(ddgs_antworten)
    adapter.search("Steuerberater Kiel", 10)
    assert clock.pausen == []  # erste Anfrage sofort
    clock.t += 1.0  # 1 s Verarbeitung
    adapter.search("Steuerkanzlei Kiel", 10)
    assert clock.pausen == [pytest.approx(2.0)]  # 3 s Mindestabstand - 1 s
    clock.t += 5.0
    adapter.search("Steuerkanzlei Kiel", 10)
    assert len(clock.pausen) == 1  # Abstand schon groß genug


def test_drosselung_mit_jitter(ddgs_antworten):
    adapter, _, clock = _adapter(ddgs_antworten, pause_jitter_s=2.0)
    adapter.search("Steuerberater Kiel", 10)
    adapter.search("Steuerkanzlei Kiel", 10)
    assert 3.0 <= clock.pausen[0] <= 5.0


@pytest.mark.parametrize("fehler", [RatelimitException("202 Ratelimit"), TimeoutException("timeout"), DDGSException("x")])
def test_retry_mit_backoff(ddgs_antworten, fehler):
    adapter, client, clock = _adapter(ddgs_antworten, fehler=[fehler, fehler])
    hits = adapter.search("Steuerkanzlei Kiel", 10)
    assert len(hits) == 3
    assert len(client.aufrufe) == 3
    # Backoff 10 s, dann 20 s; die Drosselpause ist danach bereits abgelaufen
    assert clock.pausen == [10.0, 20.0]


def test_retry_erschoepft(ddgs_antworten):
    fehler = [RatelimitException("Ratelimit")] * 3
    adapter, client, _ = _adapter(ddgs_antworten, fehler=fehler)
    with pytest.raises(SearchError, match="nach 3 Versuchen"):
        adapter.search("Steuerkanzlei Kiel", 10)
    assert len(client.aufrufe) == 3


def test_andere_fehler_werden_nicht_verschluckt(ddgs_antworten):
    adapter, _, _ = _adapter(ddgs_antworten, fehler=[ValueError("Bug")])
    with pytest.raises(ValueError):
        adapter.search("Steuerkanzlei Kiel", 10)


def test_ddgs_ist_ohne_key_konfiguriert():
    assert DdgsAdapter(client=FakeDdgsClient({})).ist_konfiguriert
