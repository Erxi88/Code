"""Verzeichnis-Adapter (Gerüst + schema.org-Parser) und Website-Nachsuche – fiktive Seiten, kein Netz."""

import json
import random

import httpx
from fakes import FakeQueryAdapter, hit

from recherche.config import VerzeichnisConfig
from recherche.models import Kandidat, SchrittStatus
from recherche.search.nachsuche import passender_treffer, website_nachsuche
from recherche.search.pipeline import Ort, run_search
from recherche.search.verzeichnisse import StbVerzeichnisAdapter, schema_org_eintraege


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
    import pytest

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
