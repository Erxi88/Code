import random

import pytest
from fakes import FakeClock, FakeHttp, fixture_json

from recherche.config import GoogleCseConfig
from recherche.search.base import KontingentErschoepft, ZugangVerweigert
from recherche.search.google import GoogleCseAdapter
from recherche.search.pipeline import Ort, run_search

S1, S2, S3 = (fixture_json("search", "google_cse", f"seite{i}.json") for i in (1, 2, 3))
TAG = fixture_json("search", "google_cse", "fehler_tageslimit.json")
MINUTE = fixture_json("search", "google_cse", "fehler_minutenlimit.json")
GESPERRT = fixture_json("search", "google_cse", "fehler_403_gesperrt.json")


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "geheim")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx-1")


def _adapter(antworten, cache=None):
    http = FakeHttp(antworten)
    clock = FakeClock()
    cfg = GoogleCseConfig(aktiv=True, pause_s=0.0, retries=2, backoff_s=2.0)
    a = GoogleCseAdapter(cfg, cache=cache, client=http.client(), sleep=clock.sleep, clock=clock, rng=random.Random(0))
    return a, http, clock


def test_paging_bis_30():
    a, http, _ = _adapter([(200, S1), (200, S2), (200, S3)])
    hits = a.search("Steuerberater Kiel", 30)
    assert len(hits) == 24
    assert [http.params(i)["start"] for i in range(3)] == ["1", "11", "21"]
    p = http.params(0)
    assert (p["key"], p["cx"], p["num"], p["gl"], p["cr"], p["hl"]) == ("geheim", "cx-1", "10", "de", "countryDE", "de")
    assert hits[0].quelle == "google_cse" and hits[0].website == "https://www.hansen-steuerberatung.example/"
    assert hits[0].snippet == "Ihre Steuerberater in Kiel: Jahresabschluss, Lohn, Beratung."


def test_seiten_starts():
    a, _, _ = _adapter([])
    assert a.seiten_starts(30) == [1, 11, 21]
    assert a.seiten_starts(100) == [1, 11, 21]
    assert a.seiten_starts(10) == [1]


def test_stop_ohne_next_page():
    a, http, _ = _adapter([(200, S3)])
    assert len(a.search("Steuerberater Kiel", 30)) == 4
    assert len(http.anfragen) == 1


def test_tageslimit_beendet_lauf_ohne_retry():
    a, http, _ = _adapter([(429, TAG)])
    with pytest.raises(KontingentErschoepft, match="Tageslimit"):
        a.search("Steuerberater Kiel", 30)
    assert len(http.anfragen) == 1


def test_minutenlimit_mit_retry():
    a, _, clock = _adapter([(429, MINUTE), (200, S3)])
    assert len(a.search("Steuerberater Kiel", 30)) == 4
    assert 2.0 in clock.pausen


def test_neukunden_sperre():
    a, _, _ = _adapter([(403, GESPERRT)])
    with pytest.raises(ZugangVerweigert, match="für neue Projekte gesperrt"):
        a.search("Steuerberater Kiel", 30)


def test_5xx_retry():
    a, _, _ = _adapter([(500, {"error": {"message": "backend"}}), (200, S3)])
    assert len(a.search("Steuerberater Kiel", 30)) == 4


def test_wiederaufnahme_nach_tageslimit(storage):
    """Tag 1: Seite 1 + 2 bezahlt, dann Tageslimit. Tag 2: Seiten 1 + 2 aus dem Cache."""
    a, _, _ = _adapter([(200, S1), (200, S2), (429, TAG)], cache=storage)
    vorlagen = ["Steuerberater {ort}"]
    stat = run_search(storage, a, [Ort("Kiel", "24103")], vorlagen, set(), "SH", "tag1", 30)
    assert stat.abbruch and "Tageslimit" in stat.abbruch
    assert storage.kandidaten("SH") == []
    b, http, _ = _adapter([(200, S3)], cache=storage)
    stat2 = run_search(storage, b, [Ort("Kiel", "24103")], vorlagen, set(), "SH", "tag2", 30)
    assert stat2.abbruch is None and b.anfragen == 1 and len(http.anfragen) == 1
    assert stat2.treffer == 24
    assert "geheim" not in str(storage.conn.execute("SELECT schluessel FROM api_cache").fetchall())


def test_ohne_keys(monkeypatch):
    monkeypatch.delenv("GOOGLE_CSE_API_KEY")
    monkeypatch.delenv("GOOGLE_CSE_ID")
    grund = GoogleCseAdapter(GoogleCseConfig()).nicht_konfiguriert_grund()
    assert "GOOGLE_CSE_API_KEY" in grund and "GOOGLE_CSE_ID" in grund
