import httpx
import pytest
from fakes import FakeAsyncClock, FakeWeb, beispiel_seiten

from recherche.config import CrawlConfig
from recherche.crawl import Crawler, finde_unterseiten, punkte, run_crawl
from recherche.models import Fund, Kandidat, SchrittStatus, Seitentyp

CFG = CrawlConfig(parallel_domains=5, retries=2, backoff_s=2.0, user_agent="kanzlei-recherche-test/1.0")


@pytest.mark.parametrize(("typ", "url", "text", "erwartet"), [
    (Seitentyp.IMPRESSUM, "https://a.example/impressum/", "Impressum", True),
    (Seitentyp.IMPRESSUM, "https://a.example/rechtliches/impressum.html", "", True),
    (Seitentyp.IMPRESSUM, "https://a.example/legal", "Impressum & Datenschutz", True),
    (Seitentyp.IMPRESSUM, "https://a.example/datenschutz/", "Datenschutz", False),
    (Seitentyp.KONTAKT, "https://a.example/kontakt.html", "Kontakt", True),
    (Seitentyp.KONTAKT, "https://a.example/anfahrt/", "So finden Sie uns", True),
    (Seitentyp.TEAM, "https://a.example/team/", "Unser Team", True),
    (Seitentyp.TEAM, "https://a.example/ueber-uns/", "Über uns", True),
    (Seitentyp.TEAM, "https://a.example/wir/", "Wir", True),
    (Seitentyp.TEAM, "https://a.example/x", "Ansprechpartner", True),
    (Seitentyp.TEAM, "https://a.example/x", "Mitarbeiter", True),
    (Seitentyp.TEAM, "https://a.example/karriere/", "Karriere", False),
    (Seitentyp.TEAM, "https://a.example/jobs/team", "Jobs im Team", False),
    (Seitentyp.TEAM, "https://a.example/wirtschaftspruefung/", "Wirtschaftsprüfung", False),
    (Seitentyp.TEAM, "https://a.example/news", "Wir freuen uns auf Sie", False),
])  # fmt: skip
def test_punkte(typ, url, text, erwartet):
    assert (punkte(typ, url, text) >= 5) is erwartet


def test_finde_unterseiten_nur_eigene_site():
    html = """<a href="/impressum">Impressum</a><a href="https://gelbeseiten.de/kontakt">Kontakt</a>
    <a href="https://www.a.example/team/">Team</a><a href="/team.pdf">Team (PDF)</a><a href="#top">Kontakt</a>"""
    seiten = finde_unterseiten(html, "https://a.example/", "a.example")
    assert seiten == {Seitentyp.IMPRESSUM: "https://a.example/impressum", Seitentyp.TEAM: "https://www.a.example/team/"}


def _setup(storage, seiten, *kandidaten, cfg=CFG, renderer=None):
    clock = FakeAsyncClock()
    web = FakeWeb(seiten, clock)
    ids = []
    for domain, extra in kandidaten:
        ids.append(storage.add_kandidat(Kandidat(bundesland="SH", domain=domain, website=f"https://{domain}/", **extra)))
    return web, clock, ids


def _crawl(storage, web, clock, cfg=CFG, renderer=None, blocklist=frozenset({"gelbeseiten.de"})):
    import asyncio

    async def lauf():
        c = Crawler(cfg, storage, set(blocklist), client=web.client(), renderer=renderer, clock=clock, sleep=clock.sleep)
        kandidaten = storage.kandidaten("SH", crawl_status=SchrittStatus.OFFEN, nur_mit_website=True)
        return await c.run(kandidaten, "SH")

    return asyncio.run(lauf())


def _alle_beispiele():
    seiten = {}
    for d in ("hansen-steuerberatung.example", "foerde-steuer.example", "steuerbuero-schuett.example"):
        seiten.update(beispiel_seiten(d))
    # Hansen: Startseite leitet auf www weiter, robots.txt sperrt nur /intern/
    seiten["https://hansen-steuerberatung.example/"] = (301, "", {"location": "https://www.hansen-steuerberatung.example/"})
    seiten["https://www.hansen-steuerberatung.example/robots.txt"] = (200, "User-agent: *\nDisallow: /intern/\n")
    seiten["https://hansen-steuerberatung.example/robots.txt"] = (200, "User-agent: *\nDisallow: /intern/\n")
    # Schütt: Startseite unter /index.php
    seiten["https://steuerbuero-schuett.example/"] = (301, "", {"location": "https://www.steuerbuero-schuett.example/index.php"})
    return seiten


def test_beispiel_websites(storage):
    web, clock, ids = _setup(storage, _alle_beispiele(), ("hansen-steuerberatung.example", {}),
                             ("foerde-steuer.example", {}), ("steuerbuero-schuett.example", {}))
    stat = _crawl(storage, web, clock)
    assert (stat.fertig, stat.nicht_gefunden) == (3, 0)
    hansen = storage.kandidat_seiten(ids[0])
    assert {t: s.url for t, s in hansen.items()} == {
        Seitentyp.START: "https://hansen-steuerberatung.example/",
        Seitentyp.IMPRESSUM: "https://www.hansen-steuerberatung.example/impressum/",
        Seitentyp.KONTAKT: "https://www.hansen-steuerberatung.example/kontakt/",
        Seitentyp.TEAM: "https://www.hansen-steuerberatung.example/team/",  # nicht /karriere/
    }
    assert hansen[Seitentyp.START].final_url == "https://www.hansen-steuerberatung.example/"
    assert "Holstenstraße 42" in hansen[Seitentyp.IMPRESSUM].text and hansen[Seitentyp.IMPRESSUM].html
    foerde = {t: s.url for t, s in storage.kandidat_seiten(ids[1]).items()}
    assert foerde[Seitentyp.IMPRESSUM] == "https://foerde-steuer.example/rechtliches/impressum.html"
    assert foerde[Seitentyp.TEAM] == "https://foerde-steuer.example/ueber-uns/"  # nicht „Jobs"
    schuett = {t: s.url for t, s in storage.kandidat_seiten(ids[2]).items()}
    assert schuett[Seitentyp.IMPRESSUM] == schuett[Seitentyp.KONTAKT] == "https://www.steuerbuero-schuett.example/impressum-kontakt.php"
    assert web.urls().count("https://www.steuerbuero-schuett.example/impressum-kontakt.php") == 1
    assert all(storage.get_kandidat(i).crawl_status == SchrittStatus.FERTIG for i in ids)
    assert storage.nicht_gefunden("SH") == []


def test_drosselung_je_domain_und_parallel(storage):
    """Echte Zeit mit 20 Anfragen/s: Abstand je Domain >= 50 ms, Domains laufen parallel."""
    import asyncio
    import time

    web = FakeWeb(_alle_beispiele(), clock=time.monotonic)
    for d in ("hansen-steuerberatung.example", "foerde-steuer.example", "steuerbuero-schuett.example"):
        storage.add_kandidat(Kandidat(bundesland="SH", domain=d, website=f"https://{d}/"))
    cfg = CFG.model_copy(update={"requests_per_second_per_domain": 20.0})

    async def lauf():
        c = Crawler(cfg, storage, set(), client=web.client())
        return await c.run(storage.kandidaten("SH"), "SH")

    start = time.monotonic()
    assert asyncio.run(lauf()).fertig == 3
    dauer = time.monotonic() - start
    je_domain: dict[str, list[float]] = {}
    for url, t in web.anfragen:
        je_domain.setdefault(httpx.URL(url).host.removeprefix("www."), []).append(t)
    for zeiten in je_domain.values():
        assert all(b - a >= 0.045 for a, b in zip(zeiten, zeiten[1:], strict=False)), zeiten
    seriell = sum(len(z) - 1 for z in je_domain.values()) * 0.05
    assert dauer < seriell * 0.8  # parallel statt nacheinander


def test_weiterleitung_ist_eigene_gedrosselte_anfrage(storage):
    web, clock, _ = _setup(storage, _alle_beispiele(), ("hansen-steuerberatung.example", {}))
    _crawl(storage, web, clock)
    zeiten = [t for u, t in web.anfragen if "hansen" in u]
    assert all(b - a >= 1.0 for a, b in zip(zeiten, zeiten[1:], strict=False))
    assert web.urls()[:3] == ["https://hansen-steuerberatung.example/robots.txt", "https://hansen-steuerberatung.example/",
                              "https://www.hansen-steuerberatung.example/robots.txt"]


def test_cache_kein_zweites_laden(storage):
    web, clock, ids = _setup(storage, _alle_beispiele(), ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    n = len(web.anfragen)
    storage.set_crawl_status(ids[0], SchrittStatus.OFFEN)
    _crawl(storage, web, clock)
    assert len(web.anfragen) == n  # Seiten und robots.txt aus SQLite


def test_nur_offene_kandidaten(storage):
    web, clock, ids = _setup(storage, _alle_beispiele(), ("foerde-steuer.example", {}), ("hansen-steuerberatung.example", {}))
    storage.set_crawl_status(ids[1], SchrittStatus.FERTIG)
    _crawl(storage, web, clock)
    assert not any("hansen" in u for u in web.urls())


def test_nicht_erreichbar_mit_discovery_daten(storage):
    seiten = {"https://weg.example/": httpx.ConnectTimeout("t"), "http://weg.example/": httpx.ConnectError("x"),
              "https://weg.example/robots.txt": (404, "")}
    web, clock, ids = _setup(storage, seiten, ("weg.example", {"strasse": "Wall 5", "plz": "24103", "ort": "Kiel",
                                                               "telefon": "0431 1", "name": "Startseite – Titel"}))
    storage.add_fund(Fund(kandidat_id=ids[0], bundesland="SH", quelle="places", quell_id="p1", name="Kanzlei Weg"))
    stat = _crawl(storage, web, clock)
    assert stat.nicht_gefunden == 1
    (ng,) = storage.nicht_gefunden("SH")
    assert ng.grund.startswith("Startseite: Timeout")
    assert (ng.name, ng.strasse, ng.plz, ng.ort, ng.telefon) == ("Kanzlei Weg", "Wall 5", "24103", "Kiel", "0431 1")
    assert storage.get_kandidat(ids[0]).crawl_status == SchrittStatus.FEHLER
    # Timeout: 1 + 2 Retries mit Backoff 2 s, 4 s
    assert web.urls().count("https://weg.example/") == 3
    assert [p for p in clock.pausen if p >= 2] [:2] == [2.0, 4.0]


def test_retry_bei_5xx(storage):
    start = beispiel_seiten("foerde-steuer.example")["https://foerde-steuer.example/"]
    seiten = {**beispiel_seiten("foerde-steuer.example"), "https://foerde-steuer.example/": [(503, "busy"), start]}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    assert _crawl(storage, web, clock).fertig == 1


def test_404_wird_gecacht(storage):
    web, clock, ids = _setup(storage, {}, ("leer.example", {}))
    _crawl(storage, web, clock)
    (ng,) = storage.nicht_gefunden("SH")
    assert ng.grund == "Startseite: HTTP 404"
    assert storage.get_seite("https://leer.example/").http_status == 404


@pytest.mark.parametrize(("robots", "erwartet"), [((200, "User-agent: *\nDisallow: /\n"), "robots.txt"),
                                                  ((503, ""), "robots.txt")])
def test_robots(storage, robots, erwartet):
    seiten = {**beispiel_seiten("foerde-steuer.example"), "https://foerde-steuer.example/robots.txt": robots,
              "http://foerde-steuer.example/robots.txt": robots}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    (ng,) = storage.nicht_gefunden("SH")
    assert erwartet in ng.grund
    assert [u for u in web.urls() if not u.endswith("robots.txt")] == []


def test_vorubergehende_fehler_werden_nicht_gecacht(storage):
    foerde = beispiel_seiten("foerde-steuer.example")
    seiten = {**foerde, "https://foerde-steuer.example/robots.txt": (503, ""),
              "http://foerde-steuer.example/robots.txt": (503, "")}
    web, clock, ids = _setup(storage, seiten, ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    assert "robots.txt" in storage.nicht_gefunden("SH")[0].grund
    # Server wieder in Ordnung: --wiederholen liest die Seite jetzt
    web.seiten["https://foerde-steuer.example/robots.txt"] = (404, "")
    storage.fehler_zuruecksetzen("SH", "crawl")
    assert _crawl(storage, web, clock).fertig == 1


def test_5xx_nach_retries_wird_nicht_gecacht(storage):
    foerde = beispiel_seiten("foerde-steuer.example")
    seiten = {**foerde, "https://foerde-steuer.example/": (503, "busy"), "http://foerde-steuer.example/": (503, "busy")}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    assert storage.get_seite("https://foerde-steuer.example/") is None
    web.seiten["https://foerde-steuer.example/"] = foerde["https://foerde-steuer.example/"]
    storage.fehler_zuruecksetzen("SH", "crawl")
    assert _crawl(storage, web, clock).fertig == 1


def test_robots_nur_fuer_unseren_user_agent(storage):
    robots = (200, "User-agent: kanzlei-recherche-test\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    seiten = {**beispiel_seiten("foerde-steuer.example"), "https://foerde-steuer.example/robots.txt": robots}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    assert "robots.txt" in storage.nicht_gefunden("SH")[0].grund


def test_weiterleitung_auf_portal(storage):
    seiten = {"https://alt.example/": (301, "", {"location": "https://www.gelbeseiten.de/eintrag/123"}),
              "https://www.gelbeseiten.de/eintrag/123": (200, "<body>" + "Eintrag " * 100 + "</body>")}
    web, clock, _ = _setup(storage, seiten, ("alt.example", {}))
    _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH")[0].grund == "Weiterleitung auf Portal (gelbeseiten.de)"


START_OHNE_IMPRESSUM = "<body><h1>Kanzlei</h1><p>" + "Steuerberatung in Kiel. " * 30 + "</p><a href='/team'>Team</a></body>"


def test_impressum_wird_geraten(storage):
    seiten = {"https://a.example/": (200, START_OHNE_IMPRESSUM),
              "https://a.example/impressum": (200, "<body><h1>Impressum</h1><p>Wall 5, 24103 Kiel</p></body>")}
    web, clock, ids = _setup(storage, seiten, ("a.example", {}))
    assert _crawl(storage, web, clock).fertig == 1
    assert storage.kandidat_seiten(ids[0])[Seitentyp.IMPRESSUM].url == "https://a.example/impressum"


def test_kein_impressum(storage):
    web, clock, ids = _setup(storage, {"https://a.example/": (200, START_OHNE_IMPRESSUM)}, ("a.example", {}))
    _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH")[0].grund == "Kein Impressum gefunden"
    # Rateversuche begrenzt (max_seiten_je_domain)
    assert len([u for u in web.urls() if "impressum" in u]) == 3


def test_soft_404_ist_kein_impressum(storage):
    seiten = {"https://a.example/": (200, START_OHNE_IMPRESSUM + "<footer>Impressum</footer>")}
    for pfad in ("/impressum", "/impressum.html", "/impressum/"):
        seiten["https://a.example" + pfad] = seiten["https://a.example/"]
    web, clock, _ = _setup(storage, seiten, ("a.example", {}))
    _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH")[0].grund == "Kein Impressum gefunden"


JS_SEITE = "<html><body><div id='app'></div><script src='/app.js'></script></body></html>"


def test_javascript_seite_ohne_playwright(storage):
    web, clock, _ = _setup(storage, {"https://js.example/": (200, JS_SEITE)}, ("js.example", {}))
    _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH")[0].grund == "Seite ohne Text (vermutlich JavaScript)"


def test_javascript_seite_mit_playwright_rueckfall(storage):
    gerendert = beispiel_seiten("foerde-steuer.example")["https://foerde-steuer.example/"][1]
    seiten = {**beispiel_seiten("foerde-steuer.example"), "https://foerde-steuer.example/": (200, JS_SEITE)}
    aufrufe = []

    async def renderer(url):
        aufrufe.append(url)
        return gerendert

    web, clock, ids = _setup(storage, seiten, ("foerde-steuer.example", {}))
    stat = _crawl(storage, web, clock, cfg=CFG.model_copy(update={"playwright": True}), renderer=renderer)
    assert stat.fertig == 1
    assert aufrufe == ["https://foerde-steuer.example/"]  # nur die fast leere Seite
    assert "Förde" in storage.kandidat_seiten(ids[0])[Seitentyp.START].text


def test_kein_html(storage):
    seiten = {"https://a.example/": (200, "%PDF-1.4", {"content-type": "application/pdf"})}
    web, clock, _ = _setup(storage, seiten, ("a.example", {}))
    _crawl(storage, web, clock)
    assert "kein HTML (application/pdf)" in storage.nicht_gefunden("SH")[0].grund


def test_erfolg_entfernt_alten_nicht_gefunden_eintrag(storage):
    seiten = {**_alle_beispiele(), "https://foerde-steuer.example/": [httpx.ConnectError("x")] * 3 + [
        beispiel_seiten("foerde-steuer.example")["https://foerde-steuer.example/"]]}
    seiten["http://foerde-steuer.example/"] = httpx.ConnectError("x")
    web, clock, ids = _setup(storage, seiten, ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    assert len(storage.nicht_gefunden("SH")) == 1
    storage.fehler_zuruecksetzen("SH", "crawl")
    _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH") == []
    assert storage.get_kandidat(ids[0]).crawl_status == SchrittStatus.FERTIG


def test_run_crawl_limit(storage):
    web, clock, _ = _setup(storage, _alle_beispiele(), ("foerde-steuer.example", {}), ("steuerbuero-schuett.example", {}))
    cfg = CFG.model_copy(update={"requests_per_second_per_domain": 1000.0})
    stat = run_crawl(storage, cfg, "SH", set(), limit=1, client=web.client())
    assert stat.fertig == 1
    assert len(storage.kandidaten("SH", crawl_status=SchrittStatus.OFFEN)) == 1
