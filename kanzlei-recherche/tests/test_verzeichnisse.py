"""Verzeichnis-Adapter (Gerüst + schema.org-Parser) und Website-Nachsuche – fiktive Seiten, kein Netz."""

import json
import random
from pathlib import Path

import httpx
import pytest
from fakes import FakeQueryAdapter, hit

from recherche.config import VerzeichnisConfig
from recherche.models import Kandidat, SchrittStatus
from recherche.search.nachsuche import passender_treffer, website_nachsuche
from recherche.search.pipeline import Ort, orte_unvollstaendig, run_search
from recherche.search.verzeichnisse import (
    DasOertlicheAdapter,
    GelbeSeitenAdapter,
    StbVerzeichnisAdapter,
    gelbeseiten_eintraege,
    schema_org_eintraege,
    stbv_kanzleien,
    zeilen_verbinden,
)


def _json_ld(*eintraege):
    return ('<html><head><script type="application/ld+json">' + json.dumps({"@graph": list(eintraege)})
            + "</script></head><body></body></html>")


def _eintrag(name, strasse, plz, ort, telefon=None, url=None, i=None):
    return {"@type": "AccountingService", "@id": f"https://verzeichnis.example/eintrag/{i or name}", "name": name,
            "address": {"@type": "PostalAddress", "streetAddress": strasse, "postalCode": plz, "addressLocality": ort},
            **({"telephone": telefon} if telefon else {}), **({"url": url} if url else {})}


MICRODATA = """<div itemscope itemtype="https://schema.org/LocalBusiness" itemid="m1">
  <span itemprop="name">Steuerbüro Lund</span>
  <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
    <span itemprop="streetAddress">Markt 2</span> <span itemprop="postalCode">25813</span> <span itemprop="addressLocality">Husum</span>
  </div><a itemprop="telephone" href="tel:04841123">04841 123</a>
  <a itemprop="url" href="https://verzeichnis.example/eintrag/m1">Eintrag</a>
</div>"""


def test_schema_org_json_ld_und_microdata():
    html = _json_ld(_eintrag("Kanzlei Nord", "Wall 5", "24103", "Kiel", "0431 222", "https://kanzlei-nord.example/")) + MICRODATA
    hits = schema_org_eintraege(html, "https://verzeichnis.example/suche", "stb_verzeichnis")
    assert [(h.name, h.strasse, h.plz, h.ort, h.telefon, h.website) for h in hits] == [
        ("Kanzlei Nord", "Wall 5", "24103", "Kiel", "0431 222", "https://kanzlei-nord.example/"),
        ("Steuerbüro Lund", "Markt 2", "25813", "Husum", "04841 123", None),  # Link aufs Verzeichnis ≠ Website
    ]


def _adapter(seiten, **cfg):
    anfragen, pausen = [], []

    def handler(req):
        anfragen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /intern/\n")
        return httpx.Response(200, text=seiten.get(str(req.url), "<html></html>"))

    cfg = {"url_vorlage": "https://verzeichnis.example/suche?plz={plz}&seite={seite}", **cfg}
    a = StbVerzeichnisAdapter(
        VerzeichnisConfig(pause_s=30, pause_jitter_s=5, je_plz=True, max_seiten=3, **cfg),
        client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=pausen.append, rng=random.Random(1))
    return a, anfragen, pausen


def test_je_plz_mit_seiten_drosselung_und_robots():
    seiten = {
        "https://verzeichnis.example/suche?plz=24103&seite=1": _json_ld(_eintrag("A", "Wall 1", "24103", "Kiel", i=1)),
        "https://verzeichnis.example/suche?plz=24103&seite=2": _json_ld(_eintrag("B", "Wall 2", "24103", "Kiel", i=2)),
        "https://verzeichnis.example/suche?plz=24105&seite=1": _json_ld(_eintrag("C", "Ring 3", "24105", "Kiel", i=3)),
    }
    a, anfragen, pausen = _adapter(seiten)
    hits = a.search_ort(Ort("Kiel", "24103", plz_weitere=("24105",)))
    assert [h.name for h in hits] == ["A", "B", "C"]
    # 24103: Seite 1, 2, 3 (leer → Ende); 24105: Seite 1, 2 (leer) → 5 Anfragen, robots.txt einmal
    assert a.anfragen == 5 and sum("robots.txt" in u for u in anfragen) == 1
    assert len(pausen) == 4 and all(30 <= p <= 35 for p in pausen)


def test_robots_verbot_und_cache(storage):
    a, _, _ = _adapter({}, url_vorlage="https://verzeichnis.example/intern/suche?plz={plz}")
    from recherche.search.base import SearchError

    with pytest.raises(SearchError, match="robots.txt verbietet"):
        a.search_ort(Ort("Kiel", "24103"))
    seiten = {"https://verzeichnis.example/suche?plz=24103&seite=1": _json_ld(_eintrag("A", "Wall 1", "24103", "Kiel"))}
    b, _, _ = _adapter(seiten)
    b.cache = storage
    b.search_ort(Ort("Kiel", "24103"))
    c, anfragen_c, _ = _adapter({})
    c.cache = storage
    assert [h.name for h in c.search_ort(Ort("Kiel", "24103"))] == ["A"] and c.anfragen == 0  # alles aus dem Cache


def test_abgleich_und_website_nachsuche(storage):
    """Grundliste: bekannte Kanzlei → nur weiterer Fund; neue ohne Website → Brave „{Name} {Ort}"."""
    storage.add_kandidat(Kandidat(bundesland="SH", domain="hansen.example", name="Hansen & Petersen Steuerberater",
                                  strasse="Holstenstraße 42", plz="24103", ort="Kiel", crawl_status=SchrittStatus.FERTIG))
    seiten = {"https://verzeichnis.example/suche?plz=24103&seite=1": _json_ld(
        _eintrag("Hansen & Petersen Steuerberater PartG mbB", "Holstenstraße 42", "24103", "Kiel", i=1),
        _eintrag("Steuerbüro Ketelsen", "Wall 9", "24103", "Kiel", i=2),
        _eintrag("Steuerberaterin Imke Thordsen", "Ring 1", "24103", "Kiel", i=3))}
    a, _, _ = _adapter(seiten)
    stat = run_search(storage, a, [Ort("Kiel", "24103")], [], set(), "SH", "l1", 60)
    assert (stat.treffer, stat.neue_kandidaten, stat.bekannt) == (3, 2, 1)
    brave = FakeQueryAdapter("brave", {
        "Steuerbüro Ketelsen Kiel": ["https://www.11880.com/x", "https://steuerbuero-ketelsen.example/"],
        "Steuerberaterin Imke Thordsen Kiel": ["https://www.kiel-magazin.example/artikel"],
    }, kosten_je_1000=5.0)
    zeilen = []
    n = website_nachsuche(storage, brave, "SH", "stb_verzeichnis", {"11880.com"}, fortschritt=zeilen.append)
    assert (n.geprueft, n.gefunden, n.ohne_website) == (2, 1, 1)
    ketelsen = storage.kandidat_nach_domain("steuerbuero-ketelsen.example")
    assert ketelsen and ketelsen.crawl_status == SchrittStatus.OFFEN and ketelsen.strasse == "Wall 9"
    thordsen = next(k for k in storage.kandidaten("SH") if k.name == "Steuerberaterin Imke Thordsen")
    assert thordsen.domain is None and thordsen.strasse == "Ring 1"  # amtliche Anschrift bleibt
    assert website_nachsuche(storage, brave, "SH", "stb_verzeichnis", set()).geprueft == 0  # jeder nur einmal


def test_passender_treffer_verlangt_namensbezug():
    hits = [hit("brave", "https://www.steuerberater-kiel.example/"), hit("brave", "https://ketelsen-stb.example/")]
    assert passender_treffer("Steuerbüro Ketelsen", "Kiel", hits, set())[1] == "ketelsen-stb.example"
    assert passender_treffer("Steuerbüro Ketelsen", "Kiel", hits[:1], set()) is None


# ---------------------------------------------------------------------- verzeichnisspezifische Parser

BEISPIELE = Path(__file__).parent / "fixtures" / "verzeichnisse" / "beispiele"
ECHT = Path(__file__).parent / "fixtures" / "verzeichnisse" / "echt"
STBV_URL = "https://steuerberaterverzeichnis.berufs-org.de/?lang=de"


def test_zeilen_verbinden():
    assert zeilen_verbinden(["delego Steuerberatungs-", "gesellschaft mbH & Co. KG"]) == "delego Steuerberatungsgesellschaft mbH & Co. KG"
    assert zeilen_verbinden(["Consiliator Wirtschafts- und", "Steuerberatungsgesellschaft mbH"]) == \
        "Consiliator Wirtschafts- und Steuerberatungsgesellschaft mbH"
    assert zeilen_verbinden(["Nordlicht Treuhand-", "und Revisions-", "gesellschaft mbH"]) == \
        "Nordlicht Treuhand- und Revisionsgesellschaft mbH"


def test_stbv_karten_zu_kanzleien():
    """Gesellschaft + ihre Berufsträger = eine Kanzlei; Zweigstellen-Zeilen und c/o; Einzel-StB ohne Firmenzeile."""
    hits, zahl = stbv_kanzleien((BEISPIELE / "stbv_24103.html").read_text(encoding="utf-8"), STBV_URL)
    assert zahl == (9, 9)
    k = {h.name: h for h in hits}
    assert list(k) == ["Hansen & Petersen Steuerberatungsgesellschaft mbH", "Förde Steuerberater PartG mbB",
                       "Steuerberaterin Imke Thordsen", "Nordlicht Treuhand- und Revisionsgesellschaft mbH"]
    hp = k["Hansen & Petersen Steuerberatungsgesellschaft mbH"]
    assert (hp.strasse, hp.plz, hp.ort) == ("Holstenstraße 42", "24103", "Kiel")
    # Maren Petersen nennt eine abweichende Firmenzeile – gleiche Adresse, eindeutig gleiches erstes Wort
    assert "2 Berufsträger (Steuerberater Dipl.-Kaufmann Jan Hansen; Steuerberaterin Maren Petersen)" in hp.snippet
    assert hp.roh_url.endswith("/details/G1/?lang=de") and hp.website is None and hp.telefon is None
    assert "Steuerbevollmächtigter Timo Brodersen" in k["Förde Steuerberater PartG mbB"].snippet  # c/o-Firma
    assert "2 Berufsträger" in k["Nordlicht Treuhand- und Revisionsgesellschaft mbH"].snippet  # ohne „Beratungsstelle Kiel"
    assert len({h.quell_id for h in hits}) == 4


def test_stbv_post_je_plz_mit_drosselung_cache_und_warnung(storage):
    html = (BEISPIELE / "stbv_24103.html").read_text(encoding="utf-8")
    anfragen, pausen, meldungen = [], [], []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        anfragen.append((req.method, req.content.decode()))
        if b"plzFilter=24105" in req.content:
            return httpx.Response(200, text=html.replace("9\n          von\n          9", "9\n          von\n          12"))
        return httpx.Response(200, text=html)

    cfg = VerzeichnisConfig(url_vorlage=STBV_URL, formular={"nachnameOrFirmenname": "", "plzFilter": "{plz}"},
                            pause_s=30, pause_jitter_s=5, je_plz=True, max_seiten=1)
    a = StbVerzeichnisAdapter(cfg, cache=storage, client=httpx.Client(transport=httpx.MockTransport(handler)),
                              sleep=pausen.append, rng=random.Random(1))
    a.melden = meldungen.append
    hits = a.search_ort(Ort("Kiel", "24103", plz_weitere=("24105",)))
    assert len(hits) == 4  # dieselben Kanzleien unter beiden PLZ
    assert anfragen == [("POST", "nachnameOrFirmenname=&plzFilter=24103"), ("POST", "nachnameOrFirmenname=&plzFilter=24105")]
    assert len(pausen) == 1 and 30 <= pausen[0] <= 35
    assert meldungen == [f"stb_verzeichnis: nur 9 von 12 Treffern angezeigt ({STBV_URL}) – Rest fehlt"]
    b = StbVerzeichnisAdapter(cfg, cache=storage, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(b.search_ort(Ort("Kiel", "24103"))) == 4 and b.anfragen == 0  # POST-Antwort aus dem Cache


def test_gelbeseiten_kacheln_mit_base64():
    hits = gelbeseiten_eintraege((BEISPIELE / "gelbeseiten.html").read_text(encoding="utf-8"),
                                 "https://www.gelbeseiten.de/suche/steuerberater/kiel")
    assert [(h.quell_id, h.name, h.strasse, h.plz, h.ort, h.telefon, h.website) for h in hits] == [
        ("aaaa-0001", "Thordsen Imke Steuerberaterin", "Ring 1", "24103", "Kiel", "0431 55 01 99",
         "http://www.thordsen-stb.example"),
        ("aaaa-0002", "Steuerbüro Ketelsen", None, "24105", "Kiel", "0431 55 02 99", None),  # Telefon nur base64
    ]


def test_url_platzhalter_start_und_ort_klein():
    seiten = {f"https://verzeichnis.example/?kw=Steuerberater&ci=Bad%20Segeberg&recFrom={s}":
              _json_ld(_eintrag(f"K{s}", "Weg 1", "23795", "Bad Segeberg", i=s)) for s in (1, 26)}
    a = DasOertlicheAdapter(
        VerzeichnisConfig(url_vorlage="https://verzeichnis.example/?kw={rubrik}&ci={ort}&recFrom={start}", seitengroesse=25),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(200, text=seiten.get(str(r.url), "")))),
        sleep=lambda s: None)
    assert [h.name for h in a.search_ort(Ort("Bad Segeberg", "23795"))] == ["K1", "K26"]
    g = GelbeSeitenAdapter(VerzeichnisConfig(url_vorlage="https://g.example/suche/steuerberater/{ort_klein}"))
    assert g._url(Ort("Bad Segeberg", "23795"), "23795", 1) == "https://g.example/suche/steuerberater/bad%20segeberg"


def test_branchenverzeichnis_ergaenzt_nur_bekannte_ohne_website(storage):
    """nur_ergaenzen: Thordsen (amtlich, ohne Website) bekommt Telefon + Website → crawl/extract neu; Ketelsen hat eine
    Website und bleibt unangetastet; ein unbekannter Eintrag wird nicht angelegt."""
    thordsen = storage.add_kandidat(Kandidat(bundesland="SH", quelle="stb_verzeichnis", suchort="Kiel",
                                             name="Steuerberaterin Imke Thordsen", strasse="Ring 1", plz="24103", ort="Kiel",
                                             crawl_status=SchrittStatus.FERTIG, extract_status=SchrittStatus.FERTIG))
    storage.add_kandidat(Kandidat(bundesland="SH", domain="ketelsen.example", name="Steuerbüro Ketelsen",
                                  telefon="0431 55 02 99", plz="24105", ort="Kiel"))
    html = (BEISPIELE / "gelbeseiten.html").read_text(encoding="utf-8").replace(
        "</div>\n<form", '<article class="mod mod-Treffer" data-realid="aaaa-0003"><h2 class="mod-Treffer__name">'
        'Unbekannt Steuerberatung</h2></article></div>\n<form')
    a = GelbeSeitenAdapter(
        VerzeichnisConfig(url_vorlage="https://www.gelbeseiten.de/suche/steuerberater/{ort_klein}", nur_ergaenzen=True),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(200, text=html))))
    vorher = len(storage.kandidaten("SH"))
    stat = run_search(storage, a, [Ort("Kiel", "24103")], [], set(), "SH", "l1", 60)
    assert (stat.treffer, stat.ergaenzt, stat.mit_website, stat.nicht_im_bestand, stat.neue_kandidaten) == (3, 1, 1, 1, 0)
    assert len(storage.kandidaten("SH")) == vorher
    k = storage.get_kandidat(thordsen)
    assert (k.telefon, k.domain, k.strasse) == ("0431 55 01 99", "thordsen-stb.example", "Ring 1")
    assert k.crawl_status == SchrittStatus.OFFEN and k.extract_status == SchrittStatus.OFFEN
    assert storage.kandidat_nach_domain("ketelsen.example").strasse is None  # Website-Kandidat nicht ergänzt


def test_orte_unvollstaendig(storage):
    orte = [Ort("Kiel", "24103"), Ort("Husum", "25813"), Ort("Heide", "25746")]
    storage.add_kandidat(Kandidat(bundesland="SH", suchort="Husum", name="Steuerbüro A", plz="25813", ort="Husum"))
    storage.add_kandidat(Kandidat(bundesland="SH", suchort="Heide", name="B", telefon="0481 1", strasse="Markt 1", plz="25746"))
    storage.add_kandidat(Kandidat(bundesland="SH", suchort="Kiel", domain="c.example", name="C"))
    assert [o.name for o in orte_unvollstaendig(storage, "SH", orte)] == ["Husum"]


@pytest.mark.skipif(not (ECHT / "stbv_24103.html").exists(), reason="echte Verzeichnisseiten nur lokal (echt/)")
def test_echte_verzeichnisseiten():
    stbv, zahl = stbv_kanzleien((ECHT / "stbv_24103.html").read_text(encoding="utf-8"), STBV_URL)
    assert zahl[0] == zahl[1] and 40 <= len(stbv) <= zahl[0]
    assert all(h.plz and h.strasse and h.name for h in stbv)
    gs = gelbeseiten_eintraege((ECHT / "gelbeseiten_kiel.html").read_text(encoding="utf-8"), "https://www.gelbeseiten.de/")
    assert len(gs) >= 40 and sum(bool(h.telefon) for h in gs) >= 0.9 * len(gs) and all(h.plz for h in gs)
    for datei, basis in (("dasoertliche_kiel.html", "https://www.dasoertliche.de/"),
                         ("11880_kiel.html", "https://www.11880.com/suche/steuerberater/kiel")):
        hits = schema_org_eintraege((ECHT / datei).read_text(encoding="utf-8"), basis, "x")
        assert len(hits) >= 20 and all(h.plz and h.telefon for h in hits) and not any(h.website for h in hits)
