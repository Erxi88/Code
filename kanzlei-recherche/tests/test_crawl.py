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


def _robots_ueberall(domain, antwort):
    """robots.txt-Antwort für alle Varianten (http/https, mit/ohne www)."""
    return {f"{s}://{w}{domain}/robots.txt": antwort for s in ("https", "http") for w in ("", "www.")}


def test_robots(storage):
    seiten = {**beispiel_seiten("foerde-steuer.example"),
              **_robots_ueberall("foerde-steuer.example", (200, "User-agent: *\nDisallow: /\n"))}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    (ng,) = storage.nicht_gefunden("SH")
    assert "robots.txt verbietet" in ng.grund
    assert [u for u in web.urls() if not u.endswith("robots.txt")] == []


def test_robots_mit_5xx_wird_beim_wiederholen_neu_geladen(storage):
    seiten = {**beispiel_seiten("foerde-steuer.example"), **_robots_ueberall("foerde-steuer.example", (503, ""))}
    web, clock, ids = _setup(storage, seiten, ("foerde-steuer.example", {}))
    assert _crawl(storage, web, clock).trotz_robots == 1  # gelesen, mit Hinweis
    assert storage.cache_get("robots", "https://foerde-steuer.example")["status"] == 503
    # Server wieder in Ordnung und sperrt jetzt: crawl --wiederholen lädt die robots.txt neu
    web.seiten["https://foerde-steuer.example/robots.txt"] = (200, "User-agent: *\nDisallow: /\n")
    import asyncio

    async def lauf(gruendlich):
        c = Crawler(CFG, storage, set(), client=web.client(), clock=clock, sleep=clock.sleep, gruendlich=gruendlich)
        return await c.erlaubt("https://foerde-steuer.example/")

    assert asyncio.run(lauf(False)) is True  # ohne --wiederholen: gecachter Fehler
    assert asyncio.run(lauf(True)) is False


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
    assert storage.nicht_gefunden("SH")[0].grund.startswith("Kein Impressum gefunden (probiert: /impressum")
    # Rateversuche begrenzt (max_seiten_je_domain)
    assert len([u for u in web.urls() if "impressum" in u]) == 3


def test_soft_404_ist_kein_impressum(storage):
    seiten = {"https://a.example/": (200, START_OHNE_IMPRESSUM + "<footer>Impressum</footer>")}
    for pfad in ("/impressum", "/impressum.html", "/impressum/"):
        seiten["https://a.example" + pfad] = seiten["https://a.example/"]
    web, clock, _ = _setup(storage, seiten, ("a.example", {}))
    _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH")[0].grund.startswith("Kein Impressum gefunden (")


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


def test_teamseite_mit_wenig_text_wird_gerendert(storage):
    """97 KB HTML, aber kaum Text (Namen per JavaScript): Teamseite mit Playwright nachladen."""
    team = "https://www.hansen-steuerberatung.example/team/"
    original = _alle_beispiele()
    js_team = ("<html><body><h1>Unser Team</h1><p>Lernen Sie uns kennen.</p><div id='team'></div>"
               "<script>var daten = '" + "x" * 60_000 + "';</script></body></html>")
    aufrufe = []

    async def renderer(url):
        aufrufe.append(url)
        return original[team][1]

    web, clock, ids = _setup(storage, {**original, team: (200, js_team)}, ("hansen-steuerberatung.example", {}))
    _crawl(storage, web, clock, cfg=CFG.model_copy(update={"playwright": True}), renderer=renderer)
    assert "Ole Brandt" in storage.kandidat_seiten(ids[0])[Seitentyp.TEAM].text
    assert aufrufe == [team]  # Start, Impressum, Kontakt haben genug Text


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


@pytest.mark.parametrize("playwright,renderer,erwartet", [
    (False, None, "nötig, aber crawl.playwright ist aus"),
    (True, "fehler", "fehlgeschlagen (RuntimeError: Chromium fehlt)"),
    (True, "kein_browser", "fehlgeschlagen (RuntimeError: BrowserType.launch: Executable doesn't exist at /x) – Browser fehlt"),
    (True, "leer", "gerendert, aber nicht mehr Text"),
])  # fmt: skip
def test_playwright_ergebnis_wird_gemeldet(storage, playwright, renderer, erwartet):
    import asyncio

    async def rendern(url):
        if renderer == "fehler":
            raise RuntimeError("Chromium fehlt")
        if renderer == "kein_browser":
            raise RuntimeError("BrowserType.launch: Executable doesn't exist at /x\n╔═══╗")
        return JS_SEITE

    web, clock, _ = _setup(storage, {"https://js.example/": (200, JS_SEITE)}, ("js.example", {}))

    async def lauf():
        c = Crawler(CFG.model_copy(update={"playwright": playwright}), storage, set(), client=web.client(),
                    renderer=rendern, clock=clock, sleep=clock.sleep)
        return await c.hole("https://js.example/", "js.example", Seitentyp.START)

    assert asyncio.run(lauf()).playwright.startswith(erwartet)


# ---- Pilot: robots.txt-Gründe, mehr Impressum-Pfade, Playwright nach 403

@pytest.mark.parametrize(("robots", "hinweis"), [
    ((503, ""), "robots.txt nicht abrufbar (HTTP 503), Seite gelesen"),
    ((500, ""), "robots.txt nicht abrufbar (HTTP 500), Seite gelesen"),  # obh-partner.de: nur /robots.txt 500
    (httpx.ConnectTimeout("x"), "robots.txt nicht abrufbar (Timeout), Seite gelesen"),
    (httpx.ConnectError("Connection reset by peer"), "robots.txt nicht abrufbar (ConnectError), Seite gelesen"),
])  # fmt: skip
def test_robots_nicht_abrufbar_seite_wird_trotzdem_gelesen(storage, robots, hinweis):
    seiten = {**beispiel_seiten("foerde-steuer.example"), **_robots_ueberall("foerde-steuer.example", robots)}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    meldungen = []
    import asyncio

    async def lauf():
        c = Crawler(CFG, storage, set(), client=web.client(), clock=clock, sleep=clock.sleep)
        return await c.run(storage.kandidaten("SH", crawl_status=SchrittStatus.OFFEN, nur_mit_website=True), "SH",
                           meldungen.append)

    stat = asyncio.run(lauf())
    assert (stat.fertig, stat.trotz_robots) == (1, 1) and hinweis in meldungen[0]
    # alle vier Varianten probiert, bevor die Seite ohne robots.txt gelesen wird
    assert sum(u.endswith("/robots.txt") for u in web.urls()) == 4
    assert storage.cache_get("robots", "https://foerde-steuer.example")["versuche"][1].startswith("http://foerde")


def test_robots_variante_mit_www_wird_genommen(storage):
    seiten = {**beispiel_seiten("foerde-steuer.example"),
              "https://foerde-steuer.example/robots.txt": httpx.ConnectError("Connection refused"),
              "http://foerde-steuer.example/robots.txt": httpx.ConnectError("Connection refused"),
              "https://www.foerde-steuer.example/robots.txt": (200, "User-agent: *\nDisallow: /\n")}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH")[0].grund == "Startseite: robots.txt verbietet den Zugriff"


@pytest.mark.parametrize("meldung", [
    "[Errno -2] Name or service not known", "[Errno 11001] getaddrinfo failed", "Could not resolve host: bos-kiel.de",
])  # fmt: skip
def test_dns_fehler_ist_tote_domain_und_wird_beim_wiederholen_uebersprungen(storage, meldung):
    fehler = httpx.ConnectError(meldung)
    seiten = {f"{s}://{w}tot.example/{p}": fehler for s in ("https", "http") for w in ("", "www.") for p in ("", "robots.txt")}
    web, clock, ids = _setup(storage, seiten, ("tot.example", {}))
    stat = _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH")[0].grund == "Startseite: Domain nicht erreichbar (DNS)"
    assert stat.dns == 1 and not any(u.endswith("/") for u in web.urls())  # keine Seitenabrufe
    assert storage.fehler_zuruecksetzen("SH", "crawl", ausser="Domain nicht erreichbar (DNS)") == 0
    assert storage.fehler_zuruecksetzen("SH", "crawl") == 1


@pytest.mark.parametrize("robots", [
    "User-agent: GPTBot\nDisallow: /\n\nUser-agent: ClaudeBot\nDisallow: /\n\nUser-agent: CCBot\nDisallow: /",
    "User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php",
    "User-agent: *\nDisallow:",
    "User-agent: re\nDisallow: /",  # Teilstring von „kanzlei-recherche" – betrifft uns nicht
])  # fmt: skip
def test_ki_bot_und_pfad_sperren_lassen_die_startseite_zu(storage, robots):
    seiten = {**beispiel_seiten("foerde-steuer.example"), "https://foerde-steuer.example/robots.txt": (200, robots)}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    assert _crawl(storage, web, clock).fertig == 1


def test_robots_txt_weiterleitung_wird_gefolgt(storage):
    seiten = {**beispiel_seiten("foerde-steuer.example"),
              "https://foerde-steuer.example/robots.txt": (301, "", {"location": "https://cdn.example/robots.txt"}),
              "https://cdn.example/robots.txt": (200, "User-agent: *\nDisallow: /\n")}
    web, clock, _ = _setup(storage, seiten, ("foerde-steuer.example", {}))
    _crawl(storage, web, clock)
    assert storage.nicht_gefunden("SH")[0].grund == "Startseite: robots.txt verbietet den Zugriff"


def test_impressum_wird_vor_kontakt_und_team_geraten(storage):
    """Ohne Impressum-Link: erst raten, dann Kontakt/Team – sonst ist das Budget verbraucht."""
    start = ("<body><h1>Kanzlei</h1><p>" + "Steuerberatung in Kiel. " * 30 + "</p><a href='/kontakt'>Kontakt</a>"
             "<a href='/team'>Team</a><a href='/ueber-uns'>Über uns</a></body>")
    seiten = {"https://a.example/": (200, start),
              "https://a.example/impressum.html": (200, "<body><p>Angaben gemäß § 5 TMG</p><p>Wall 5, 24103 Kiel</p></body>")}
    web, clock, ids = _setup(storage, seiten, ("a.example", {}))
    assert _crawl(storage, web, clock, cfg=CFG.model_copy(update={"max_seiten_je_domain": 4})).fertig == 1
    assert storage.kandidat_seiten(ids[0])[Seitentyp.IMPRESSUM].url == "https://a.example/impressum.html"


def test_gruendlich_probiert_weitere_pfade(storage):
    seiten = {"https://a.example/": (200, START_OHNE_IMPRESSUM),
              "https://a.example/imprint": (200, "<body><h1>Imprint</h1><p>Wall 5, 24103 Kiel</p></body>")}
    web, clock, ids = _setup(storage, seiten, ("a.example", {}))
    _crawl(storage, web, clock)
    assert "probiert: /impressum, /impressum/, /impressum.html" in storage.nicht_gefunden("SH")[0].grund
    storage.fehler_zuruecksetzen("SH", "crawl")
    import asyncio

    async def lauf():
        c = Crawler(CFG, storage, set(), client=web.client(), clock=clock, sleep=clock.sleep, gruendlich=True)
        return await c.run(storage.kandidaten("SH", crawl_status=SchrittStatus.OFFEN, nur_mit_website=True), "SH")

    assert asyncio.run(lauf()).fertig == 1
    assert storage.kandidat_seiten(ids[0])[Seitentyp.IMPRESSUM].url == "https://a.example/imprint"


@pytest.mark.parametrize("linktext", ["Anbieterkennzeichnung", "Rechtliche Hinweise", "Rechtliches", "Imprint"])
def test_impressum_linktexte(linktext):
    html = f"<a href='/seite-42'>{linktext}</a><a href='/team'>Team</a>"
    assert finde_unterseiten(html, "https://a.example/", "a.example")[Seitentyp.IMPRESSUM] == "https://a.example/seite-42"


def test_playwright_nach_403_auf_der_startseite(storage):
    original = beispiel_seiten("foerde-steuer.example")
    start = "https://foerde-steuer.example/"
    seiten = {**original, start: (403, "Access denied")}
    aufrufe = []

    async def renderer(url):
        aufrufe.append(url)
        return original[start][1]

    web, clock, ids = _setup(storage, seiten, ("foerde-steuer.example", {}))
    assert _crawl(storage, web, clock, cfg=CFG.model_copy(update={"playwright": True}), renderer=renderer).fertig == 1
    assert aufrufe == [start]
    assert storage.kandidat_seiten(ids[0])[Seitentyp.START].http_status == 200


def test_robots_check_diagnose():
    import asyncio

    from recherche.crawl import robots_check

    clock = FakeAsyncClock()
    web = FakeWeb({
        "https://obh.example/robots.txt": (500, ""),
        "http://obh.example/robots.txt": (301, "", {"location": "https://obh.example/robots.txt"}),
        "https://www.obh.example/robots.txt": httpx.ConnectError("[Errno -2] Name or service not known"),
        "http://www.obh.example/robots.txt": (200, "User-agent: *\nDisallow: /intern/\n"),
        "https://obh.example/": (200, "<html><body>Kanzlei</body></html>"),
    }, clock)
    zeilen = asyncio.run(robots_check(CFG, "obh.example", client=web.client()))
    text = "\n".join(zeilen)
    assert "HTTP 500 → nicht abrufbar (5xx)" in text
    assert "301 http://obh.example/robots.txt" in text  # Weiterleitungskette
    assert "DNS – Domain existiert nicht" in text
    assert "gilt Gruppe „*\"; Startseite: erlaubt (keine passende Regel)" in text
    assert zeilen[-1].strip().startswith("HTTP 200")


def test_robots_entscheidung_nennt_regel():
    from recherche.robots import Robots

    r = Robots.parse("User-agent: *\nDisallow: /\nAllow: /impressum\n")
    assert r.entscheidung("kanzlei-recherche/0.1", "https://x/") == (False, "Disallow: /")
    assert r.entscheidung("kanzlei-recherche/0.1", "https://x/impressum") == (True, "Allow: /impressum")


LAENDER = {"01067": "Sachsen", "04109": "Sachsen", "24103": "Schleswig-Holstein", "20095": "Hamburg"}


@pytest.mark.parametrize("name, snippet, plz, erwartet", [
    ("Steuerberater Dresden – Kanzlei Muster", "Wilsdruffer Str. 1, 01067 Dresden", None, "01067 Dresden – Sachsen"),
    ("Kanzlei Muster", "Büros in 01067 Dresden und 24103 Kiel", None, None),  # eine PLZ im Land genügt
    ("Kanzlei Muster", "Ihr Steuerberater im Norden", None, None),  # keine PLZ erkennbar → crawlen
    ("Kanzlei Muster", None, "20095", "20095"),  # PLZ aus der Discovery (Verzeichnis)
    ("Kanzlei Muster", "Kundennummer 12345 Muster", None, None),  # unbekannte Zahl, kein Land
])  # fmt: skip
def test_ausserhalb_laut_suchtreffer(name, snippet, plz, erwartet):
    from recherche.crawl import ausserhalb_laut_suchtreffer

    k = Kandidat(bundesland="SH", domain="m.example", plz=plz)
    funde = [Fund(kandidat_id=1, bundesland="SH", quelle="brave", quell_id="1", name=name, snippet=snippet)]
    grund = ausserhalb_laut_suchtreffer(k, funde, LAENDER.get, "Schleswig-Holstein")
    assert (grund is None) if erwartet is None else (erwartet in grund and grund.startswith("außerhalb Schleswig-Holstein"))


def test_crawl_ueberspringt_kandidaten_ausserhalb(storage):
    from recherche.crawl import ausserhalb_laut_suchtreffer

    seiten = beispiel_seiten("foerde-steuer.example")
    web, clock, ids = _setup(storage, seiten, ("foerde-steuer.example", {}), ("dresden-steuer.example", {}))
    storage.add_fund(Fund(kandidat_id=ids[1], bundesland="SH", quelle="brave", quell_id="d",
                          name="Steuerberater Dresden", snippet="Prager Str. 2, 01067 Dresden"))
    stat = run_crawl(storage, CFG, "SH", set(), client=web.client(),
                     land_filter=lambda k, f: ausserhalb_laut_suchtreffer(k, f, LAENDER.get, "Schleswig-Holstein"))
    assert (stat.fertig, stat.ausserhalb) == (1, 1)
    (ng,) = storage.nicht_gefunden("SH")
    assert ng.domain == "dresden-steuer.example" and ng.grund.startswith("außerhalb Schleswig-Holstein laut Suchtreffer")
    assert not any("dresden" in u for u in web.urls())


def test_wiederholen_rendert_gecachte_403_startseite(storage):
    """Erster Lauf ohne Playwright cacht HTTP 403; crawl --wiederholen mit Playwright rendert jetzt."""
    import asyncio

    original = beispiel_seiten("foerde-steuer.example")
    start = "https://foerde-steuer.example/"
    web, clock, ids = _setup(storage, {**original, start: (403, "Access denied")}, ("foerde-steuer.example", {}))
    assert _crawl(storage, web, clock).fertig == 0
    assert storage.get_seite(start).http_status == 403
    storage.fehler_zuruecksetzen("SH", "crawl")

    async def renderer(url):
        return original[start][1]

    async def lauf(gruendlich):
        c = Crawler(CFG.model_copy(update={"playwright": True}), storage, set(), client=web.client(), renderer=renderer,
                    clock=clock, sleep=clock.sleep, gruendlich=gruendlich)
        return await c.run(storage.kandidaten("SH", crawl_status=SchrittStatus.OFFEN, nur_mit_website=True), "SH")

    assert asyncio.run(lauf(True)).fertig == 1
    assert storage.get_seite(start).http_status == 200  # Cache durch das gerenderte HTML ersetzt
