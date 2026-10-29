import random

import httpx
import pytest
from fakes import FakeClock, FakeHttp, fixture_json

from recherche.config import BraveConfig
from recherche.search.base import KontingentErschoepft, SearchError, ZugangVerweigert
from recherche.search.brave import BraveAdapter
from recherche.search.pipeline import Ort, run_search

SEITE0 = fixture_json("search", "brave", "kiel_seite0.json")
SEITE1 = fixture_json("search", "brave", "kiel_seite1.json")
LEER = fixture_json("search", "brave", "leer.json")
F429 = fixture_json("search", "brave", "fehler_429.json")
F401 = fixture_json("search", "brave", "fehler_401.json")


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")


def _adapter(antworten, cache=None, **cfg):
    http = FakeHttp(antworten)
    clock = FakeClock()
    cfg = BraveConfig(**{"aktiv": True, "pause_s": 1.1, "retries": 2, "backoff_s": 5.0, **cfg})
    a = BraveAdapter(cfg, cache=cache, client=http.client(), sleep=clock.sleep, clock=clock, rng=random.Random(0))
    return a, http, clock


def test_paging_bis_letzte_seite():
    a, http, _ = _adapter([(200, SEITE0), (200, SEITE1)])
    hits = a.search("Steuerberater Kiel", 60)
    assert len(hits) == 25
    assert a.anfragen == 2
    assert [http.params(i)["offset"] for i in range(2)] == ["0", "1"]  # Brave: offset = Seitenindex


def test_parameter_und_header():
    a, http, _ = _adapter([(200, SEITE1)])
    a.search("Steuerberater Kiel", 20)
    p = http.params(0)
    assert p == {"q": "Steuerberater Kiel", "count": "20", "offset": "0", "country": "DE",
                 "search_lang": "de", "safesearch": "moderate", "result_filter": "web"}
    req = http.anfragen[0]
    assert req.headers["X-Subscription-Token"] == "test-key"
    assert str(req.url).startswith("https://api.search.brave.com/res/v1/web/search")


def test_ergebnisformat():
    a, _, _ = _adapter([(200, SEITE0)])
    h = a.search("Steuerberater Kiel", 20)[0]
    assert h.quelle == "brave"
    assert h.website == h.roh_url == "https://www.hansen-steuerberatung.example/"
    assert h.quell_id == "https://www.hansen-steuerberatung.example"
    assert h.name == "Hansen & Petersen Steuerberater PartG mbB – Kiel"
    assert h.snippet == "Ihre Steuerberater in Kiel: Jahresabschluss, Lohn, Beratung."  # ohne <strong>
    assert h.plz is None and h.telefon is None


def test_stop_ohne_more_results():
    a, http, _ = _adapter([(200, SEITE1)])
    assert len(a.search("Steuerberater Kiel", 60)) == 5
    assert len(http.anfragen) == 1


def test_max_results_begrenzt_seiten():
    a, http, _ = _adapter([(200, SEITE0)])
    assert len(a.search("Steuerberater Kiel", 20)) == 20
    assert len(http.anfragen) == 1


def test_max_per_query_und_offset_obergrenze():
    a, _, _ = _adapter([], max_per_query=500)
    seiten = list(a._seiten("q", 500))
    assert [p["offset"] for p, _ in seiten] == list(range(10))


def test_leere_antwort():
    a, _, _ = _adapter([(200, LEER)])
    assert a.search("Steuerberater Kleinstdorf", 20) == []


def test_drosselung():
    a, _, clock = _adapter([(200, SEITE0), (200, SEITE1)])
    a.search("Steuerberater Kiel", 60)
    assert clock.pausen == [pytest.approx(1.1)]


def test_retry_bei_429_ratenlimit():
    kopf = {"X-RateLimit-Remaining": "0, 950"}
    a, http, clock = _adapter([(429, F429, kopf), (200, SEITE1)])
    assert len(a.search("Steuerberater Kiel", 20)) == 5
    assert a.anfragen == 2
    assert clock.pausen[0] == 5.0


def test_retry_bei_5xx_und_timeout():
    a, _, clock = _adapter([(503, {"error": "busy"}), httpx.ReadTimeout("timeout"), (200, SEITE1)])
    assert len(a.search("Steuerberater Kiel", 20)) == 5
    assert [p for p in clock.pausen if p >= 5] == [5.0, 10.0]


def test_dauerhaftes_ratenlimit_beendet_lauf():
    a, _, _ = _adapter([(429, F429)] * 3)
    with pytest.raises(KontingentErschoepft, match="nach 3 Versuchen"):
        a.search("Steuerberater Kiel", 20)


def test_monatskontingent_erschoepft_ohne_retry():
    a, http, _ = _adapter([(429, F429, {"X-RateLimit-Remaining": "1, 0"})])
    with pytest.raises(KontingentErschoepft, match="Brave-Guthaben erschöpft – im Dashboard aufladen"):
        a.search("Steuerberater Kiel", 20)
    assert len(http.anfragen) == 1


def test_402_guthaben():
    a, _, _ = _adapter([(402, {"error": {"detail": "Payment required"}})])
    with pytest.raises(KontingentErschoepft):
        a.search("Steuerberater Kiel", 20)


def test_ungueltiger_key():
    a, _, _ = _adapter([(401, F401)])
    with pytest.raises(ZugangVerweigert, match="BRAVE_API_KEY"):
        a.search("Steuerberater Kiel", 20)


def test_sonstiger_fehler_ohne_retry():
    a, http, _ = _adapter([(422, {"error": {"detail": "invalid"}})])
    with pytest.raises(SearchError):
        a.search("Steuerberater Kiel", 20)
    assert len(http.anfragen) == 1


def test_kaputtes_json_ist_fehler_der_abfrage():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>Wartung</html>"))
    a = BraveAdapter(BraveConfig(aktiv=True), client=httpx.Client(transport=transport))
    with pytest.raises(SearchError, match="kein JSON"):
        a.search("Steuerberater Kiel", 20)


def test_key_fehlt(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY")
    assert BraveAdapter(BraveConfig()).nicht_konfiguriert_grund() == "BRAVE_API_KEY fehlt in .env"


def test_cache_spart_anfragen(storage):
    a, _, _ = _adapter([(200, SEITE0), (200, SEITE1)], cache=storage)
    a.search("Steuerberater Kiel", 60)
    zweiter, http2, _ = _adapter([], cache=storage)
    assert len(zweiter.search("Steuerberater Kiel", 60)) == 25
    assert zweiter.anfragen == 0 and http2.anfragen == []
    # Key steht nie im Cache-Schlüssel
    assert "test-key" not in str(storage.conn.execute("SELECT schluessel FROM api_cache").fetchall())


def test_kostenlog_in_pipeline(storage):
    def antwort(req):
        return (200, SEITE1) if req.url.params["q"] == "Steuerberater Kiel" else (200, LEER)
    a, _, _ = _adapter(antwort, cache=storage, kosten_je_1000=5.0)
    stat = run_search(storage, a, [Ort("Kiel", "24103")], ["Steuerberater {ort}", "Steuerkanzlei {ort}"],
                      {"11880.com"}, "SH", "lauf1", 60)
    assert (stat.anfragen, stat.kosten, stat.neue_kandidaten) == (2, 0.01, 4)  # 5 Treffer, 11880 blockiert
    assert storage.anfragen_summe("SH", "lauf1") == {"brave": (2, 0.01)}


def test_402_meldung_und_kein_retry():
    a, http, _ = _adapter([(402, {"error": {"detail": "Payment required"}})])
    with pytest.raises(KontingentErschoepft, match="Brave-Guthaben erschöpft – im Dashboard aufladen, dann erneut starten"):
        a.search("Steuerberater Kiel", 20)
    assert len(http.anfragen) == 1


def test_guthabentext_im_429_bricht_sofort_ab():
    a, http, _ = _adapter([(429, {"error": {"code": "USAGE_LIMIT_EXCEEDED", "detail": "Usage limit exceeded"}})] * 3)
    with pytest.raises(KontingentErschoepft, match="Brave-Guthaben"):
        a.search("Steuerberater Kiel", 20)
    assert len(http.anfragen) == 1


def test_restkontingent_null_in_erfolgreicher_antwort_stoppt_vor_naechster_anfrage():
    a, http, _ = _adapter([(200, SEITE0, {"X-RateLimit-Remaining": "1, 0"}), (200, SEITE1)])
    assert len(a.search("Steuerberater Kiel", 20)) == 20  # diese Antwort wird noch genutzt
    with pytest.raises(KontingentErschoepft, match="Restkontingent 0"):
        a.search("Steuerkanzlei Kiel", 20)
    assert len(http.anfragen) == 1


def test_harter_timeout_bei_haengendem_request():
    """Lauf fror eine Stunde ein: ein hängender Request wird nach timeout_s + 2 s abgebrochen."""
    import threading

    freigabe = threading.Event()

    def haengt(req):
        freigabe.wait(5)  # simuliert eine Antwort, die nie fertig wird
        return (200, SEITE1)

    a, _, _ = _adapter(haengt, timeout_s=0.1, retries=1)
    meldungen = []
    a.melden = meldungen.append
    try:
        with pytest.raises(SearchError, match="harter Timeout"):
            a.search("Steuerberater Kiel", 20)
    finally:
        freigabe.set()
    assert a.anfragen == 2 and "Versuch 1/2" in meldungen[0]


def test_fortschritt_mit_ortsnummer_und_anfragen(storage):
    a, _, _ = _adapter([(200, SEITE1), (200, LEER)])
    zeilen = []
    run_search(storage, a, [Ort("Kiel", "24103"), Ort("Husum", "25813")], ["Steuerberater {ort}"], set(), "SH", "l1", 20,
               fortschritt=zeilen.append)
    assert zeilen[0].startswith("Ort 1/2 · Steuerberater Kiel: 5 Treffer")
    assert zeilen[1].startswith("Ort 2/2 · Steuerberater Husum") and "gesamt 2 Anfragen ≈ 0.01" in zeilen[1]


def test_schaetzung_vor_dem_start(storage):
    from recherche.search.pipeline import Suchplan, schaetze_anfragen

    a, _, _ = _adapter([(200, SEITE1)])
    plan = Suchplan(["Steuerberater {ort}", "Steuerkanzlei {ort}"])
    orte = [Ort("Kiel", "24103"), Ort("Husum", "25813")]
    assert schaetze_anfragen(storage, a, orte, plan, "SH", 60) == (4, 12)  # ohne Seitenregel: 60/20 = 3 je Query
    run_search(storage, a, orte[:1], ["Steuerberater {ort}"], set(), "SH", "l1", 20)
    assert schaetze_anfragen(storage, a, orte, plan, "SH", 60) == (3, 9)  # erledigte Query zählt nicht
