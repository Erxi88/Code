"""Standortlisten-Crawler für Ketten (Prüfkatalog 9, `recherche ketten`) – nur fiktive Seiten, kein Netz."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from recherche.ketten import Kette, KettenConfig, setze_config
from recherche.ketten_crawl import (
    QUELLE,
    standort_links,
    standorte_anlegen,
    uebersicht_laden,
)
from recherche.models import Kandidat, Kanzlei
from recherche.storage import Storage

FIX = Path(__file__).parent / "fixtures" / "ketten"
UEBERSICHT = "https://www.kette.example/kanzlei-suche"


def _kette(**kw) -> Kette:
    werte = dict(schluessel="kette", name="Kette Steuerberatungsgesellschaft mbH", zentrale="kette.example",
                 uebersicht=UEBERSICHT, link_muster=re.compile(r"https?://(?!www\.)[a-z0-9-]+\.kette\.example/?", re.I),
                 standortmuster="subdomain")
    werte.update(kw)
    return Kette(**werte)


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "t.db") as st:
        yield st


def test_standort_links_subdomain():
    html = (FIX / "uebersicht.html").read_text(encoding="utf-8")
    assert standort_links(html, UEBERSICHT, _kette()) == [
        "https://heide.kette.example/", "https://ratzeburg.kette.example/", "https://husum.kette.example/"]


def test_standort_links_pfad():
    html = '<a href="/standorte/kiel/">Kiel</a> <a href="/standorte/">Alle</a> <a href="/standorte/heide#x">Heide</a>'
    k = _kette(standortmuster="pfad", link_muster=re.compile(r"https://www\.kette\.example/standorte/[a-z-]+/?$"))
    assert standort_links(html, UEBERSICHT, k) == ["https://www.kette.example/standorte/kiel",
                                                    "https://www.kette.example/standorte/heide"]


def test_standorte_anlegen_mit_zentrale_und_wiederaufnahme(storage):
    urls = ["https://heide.kette.example/", "https://ratzeburg.kette.example/"]
    stat = standorte_anlegen(storage, "SH", _kette(), urls, "lauf1")
    assert (stat.standorte, stat.neu, stat.bekannt) == (2, 3, 0)  # zwei Standorte + Zentrale
    domains = {k.domain for k in storage.kandidaten("SH")}
    assert domains == {"heide.kette.example", "ratzeburg.kette.example", "kette.example"}
    funde = storage.funde("SH", QUELLE)
    assert len(funde) == 2 and "Zentrale kette.example" in funde[0].snippet
    # erneuter Lauf: nichts doppelt
    stat2 = standorte_anlegen(storage, "SH", _kette(), urls, "lauf2")
    assert (stat2.neu, stat2.bekannt) == (0, 2)
    assert len(storage.kandidaten("SH")) == 3 and len(storage.funde("SH", QUELLE)) == 2


def test_pfad_standorte_ohne_eigene_domain(storage):
    k = _kette(standortmuster="pfad", link_muster=re.compile(r"https://www\.kette\.example/standorte/"))
    urls = ["https://www.kette.example/standorte/kiel", "https://www.kette.example/standorte/heide"]
    standorte_anlegen(storage, "SH", k, urls, "lauf1")
    standorte_anlegen(storage, "SH", k, urls, "lauf2")
    pfade = [x for x in storage.kandidaten("SH") if x.domain is None]
    assert sorted(x.website for x in pfade) == sorted(urls)


def test_uebersicht_laden_robots_und_cache(storage):
    aufrufe = []

    def handler(req: httpx.Request) -> httpx.Response:
        aufrufe.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /intern/\n")
        return httpx.Response(200, text=(FIX / "uebersicht.html").read_text(encoding="utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    html = uebersicht_laden(_kette(), client, "kanzlei-recherche/0.1", storage)
    assert "Kanzlei Heide" in html
    assert uebersicht_laden(_kette(), client, "kanzlei-recherche/0.1", storage) == html
    assert len(aufrufe) == 2  # zweiter Aufruf aus dem Cache


def test_uebersicht_robots_verbot():
    meldungen = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="User-agent: *\nDisallow: /\n")))
    assert uebersicht_laden(_kette(), client, "kanzlei-recherche/0.1", None, meldungen.append) is None
    assert "robots.txt verbietet" in meldungen[0]


def test_pfad_standorte_bilden_eine_gruppe_mit_der_zentrale():
    from recherche.extract.standorte import gruppen, zentrale

    k = _kette(standortmuster="pfad", link_muster=re.compile(r"https://www\.kette\.example/standorte/"))
    setze_config(KettenConfig(ketten=[k]))
    try:
        zeilen = [Kanzlei(id=1, lauf_bundesland="SH", domain="kette.example", website="https://www.kette.example/"),
                  Kanzlei(id=2, lauf_bundesland="SH", website="https://www.kette.example/standorte/kiel"),
                  Kanzlei(id=3, lauf_bundesland="SH", website="https://www.kette.example/standorte/heide")]
        (g,), _ = gruppen(zeilen)
        assert len(g) == 3 and zentrale(g).id == 1
    finally:
        setze_config(KettenConfig())


def test_crawl_beginnt_bei_pfad_standort():
    import asyncio

    from recherche.config import CrawlConfig
    from recherche.crawl import Crawler
    from recherche.models import Kandidat

    geholt = []

    class Probe(Crawler):
        async def hole(self, url, domain, typ):
            geholt.append(url)
            raise RuntimeError("stopp")

    c = Probe.__new__(Probe)
    c.cfg = CrawlConfig()
    k = Kandidat(bundesland="SH", website="https://www.kette.example/standorte/kiel")
    with pytest.raises(RuntimeError):
        asyncio.run(c._crawl(k, "kette.example", None))
    assert geholt == ["https://www.kette.example/standorte/kiel"]


def test_standorte_fest_ohne_uebersicht(tmp_path):
    """wetreu: keine Übersichtsseite (JS-Widget) → feste Liste aus ketten.yaml (Domains oder URLs)."""
    from recherche.ketten_crawl import feste_standorte

    (tmp_path / "ketten.yaml").write_text(
        "ketten:\n  nordtreu:\n    name: Nordtreu\n    zentrale: nordtreu.example\n    standorte_fest:\n"
        "      - fehmarn.nordtreu.example\n      - https://lbb.nordtreu.example/start\n      - www.nordtreu.example\n"
        "      - fehmarn.nordtreu.example/\n", encoding="utf-8")
    k = KettenConfig.laden(tmp_path / "ketten.yaml").ketten[0]
    assert k.uebersicht is None
    assert feste_standorte(k) == ["https://fehmarn.nordtreu.example/", "https://lbb.nordtreu.example/start"]
    assert k.enthaelt("lbb.nordtreu.example")


def test_subdomains_aus_suchtreffern_sind_standorte(storage):
    from recherche.ketten_crawl import standorte_aus_suchtreffern

    for d in ("kiel.kette.example", "jobs.kette.example", "kette.example", "andere.example"):
        storage.add_kandidat(Kandidat(bundesland="SH", domain=d, website=f"https://{d}/", quelle="brave"))
    k = _kette()
    assert standorte_aus_suchtreffern(storage, "SH", k) == ["https://kiel.kette.example/"]
    assert not k.enthaelt("jobs.kette.example") and not k.enthaelt("karriere.kette.example")
    stat = standorte_anlegen(storage, "SH", k, standorte_aus_suchtreffern(storage, "SH", k), "lauf")
    assert (stat.neu, stat.bekannt) == (0, 1)  # vorhandener Kandidat, nur mit der Zentrale verknüpft
    assert storage.funde("SH", QUELLE)[0].snippet == "Standort der Kette Kette Steuerberatungsgesellschaft mbH (Zentrale kette.example)"


def test_cli_ketten_mit_fester_liste_ohne_netz(tmp_path):
    from typer.testing import CliRunner

    from recherche import cli

    cfg = tmp_path / "settings.yaml"
    (tmp_path / "ketten.yaml").write_text(
        "ketten:\n  nordtreu:\n    name: Nordtreu\n    zentrale: nordtreu.example\n    standorte_fest:\n"
        "      - fehmarn.nordtreu.example\n      - lbb.nordtreu.example\n", encoding="utf-8")
    cfg.write_text(f"bundeslaender:\n  SH:\n    name: Schleswig-Holstein\n    orte_datei: x.csv\n"
                   f"pfade:\n  db: {tmp_path / 'db.sqlite'}\n  ketten: {tmp_path / 'ketten.yaml'}\n", encoding="utf-8")
    r = CliRunner().invoke(cli.app, ["ketten", "-b", "SH", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    ausgabe = " ".join(r.output.split())
    assert "Nordtreu: 2 Standorte (0 aus der Übersicht, 2 aus standorte_fest, 0 aus Suchtreffern), 3 neu" in ausgabe
