"""Stufe C `recherche telefonbuch` (Vorgabe 05.11.2026): Namenssuche in Das Örtliche, Rückfall Gelbe Seiten – nur mit
Nachname und PLZ/Straße; fiktive Seiten im Aufbau der echten (JSON-LD bzw. article.mod-Treffer), kein Netz."""

import json
from pathlib import Path

import httpx

from recherche.config import VerzeichnisConfig
from recherche.models import Fund, Kandidat, SchrittStatus
from recherche.search.base import SearchHit
from recherche.search.verzeichnisse import DasOertlicheAdapter, GelbeSeitenAdapter
from recherche.stbv_lauf import LUECKEN_SPALTEN, QUELLE, luecken, luecken_werte
from recherche.telefonbuch import (
    MERKER,
    namenswoerter,
    passt,
    run_telefonbuch,
    suchname,
)

BEISPIELE = Path(__file__).parent / "fixtures" / "verzeichnisse" / "beispiele"


def _json_ld(*eintraege):
    return ('<html><head><script type="application/ld+json">' + json.dumps({"@graph": list(eintraege)})
            + "</script></head><body></body></html>")


def _eintrag(i, name, strasse, plz, ort, telefon=None, url=None):
    return {"@type": "LocalBusiness", "@id": f"https://www.dasoertliche.de/eintrag/{i}", "name": name,
            "address": {"@type": "PostalAddress", "streetAddress": strasse, "postalCode": plz, "addressLocality": ort},
            **({"telephone": telefon} if telefon else {}), **({"url": url} if url else {})}


def _adapter(klasse, seiten: dict[str, str], vorlage: str):
    abrufe, pausen = [], []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        abrufe.append(str(req.url))
        for teil, html in seiten.items():
            if teil in str(req.url):
                return httpx.Response(200, text=html)
        return httpx.Response(200, text="<html><body>Keine Treffer</body></html>")

    cfg = VerzeichnisConfig(nur_ergaenzen=True, name_url_vorlage=vorlage, name_pause_s=30.0, name_pause_jitter_s=0.0)
    return klasse(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=pausen.append), abrufe, pausen


def _amtlich(storage, name, strasse, plz, ort, snippet="Amtliches Steuerberaterverzeichnis: 1 Berufsträger (x)"):
    kid = storage.add_kandidat(Kandidat(bundesland="SH", name=name, strasse=strasse, plz=plz, ort=ort, quelle=QUELLE))
    storage.add_fund(Fund(kandidat_id=kid, bundesland="SH", quelle=QUELLE, quell_id=f"q-{kid}", snippet=snippet))
    return kid


def test_suchname_und_namenswoerter():
    assert suchname("Steuerberaterin Imke Thordsen") == "Imke Thordsen"
    assert suchname("Steuerberater Lund, Ott & Rau") == "Lund Ott Rau"
    assert namenswoerter("Steuerberaterin Imke Thordsen") == {"thordsen"}
    assert namenswoerter("Steuerberater Lund, Ott & Rau") == {"lund", "ott", "rau"}
    assert "nordlicht" in namenswoerter("Nordlicht Treuhand GmbH")


def test_treffer_nur_mit_nachname_und_plz_oder_strasse():
    k = Kandidat(bundesland="SH", name="Steuerberaterin Imke Thordsen", strasse="Ring 1", plz="24103", ort="Kiel")

    def h(name, strasse, plz):
        return SearchHit(quelle="dasoertliche", quell_id=name, name=name, strasse=strasse, plz=plz, ort="Kiel")

    assert passt(h("Thordsen, Imke Steuerberaterin", "Ring 1", "24103"), k)
    assert passt(h("Thordsen Imke", "Ring 1", "24105"), k)  # PLZ anders, Straße gleich
    assert not passt(h("Thordsen, Imke", "Wall 9", "24937"), k)  # Namensvetterin anderswo
    assert not passt(h("Steuerbüro Lund", "Ring 1", "24103"), k)  # gleiche Adresse, anderer Name


def test_telefonbuch_das_oertliche_rueckfall_gelbe_seiten_cache_und_luecken_spalte(storage):
    imke = _amtlich(storage, "Steuerberaterin Imke Thordsen", "Ring 1", "24103", "Kiel")
    ole = _amtlich(storage, "Steuerberater Ole Ketelsen", "Wall 9", "24103", "Kiel")
    ina = _amtlich(storage, "Steuerberaterin Ina Namenlos", "Markt 3", "24103", "Kiel")
    mit_website = storage.add_kandidat(Kandidat(bundesland="SH", name="Steuerberater Kai Web", domain="web.example",
                                                website="https://web.example/", quelle=QUELLE))
    storage.add_fund(Fund(kandidat_id=mit_website, bundesland="SH", quelle=QUELLE, quell_id="qw"))

    oertliche, abrufe_o, pausen = _adapter(DasOertlicheAdapter, {
        "Ole%20Ketelsen": _json_ld(_eintrag(1, "Ketelsen Ole Steuerberater", "Wall 9", "24103", "Kiel",
                                            "0431 9 87 65")),
        # Imke: Das Örtliche nennt nur eine Namensvetterin in Flensburg → Rückfall Gelbe Seiten
        "Imke%20Thordsen": _json_ld(_eintrag(2, "Thordsen, Imke", "Hafen 1", "24937", "Flensburg", "0461 1 23 45")),
    }, "https://www.dasoertliche.de/?kw={name}&ci={ort}&form_name=search_nat")
    gelbe, abrufe_g, _ = _adapter(GelbeSeitenAdapter, {
        "Imke%20Thordsen": (BEISPIELE / "gelbeseiten.html").read_text(encoding="utf-8")},
        "https://www.gelbeseiten.de/suche/{name}/{ort_klein}")
    for a in (oertliche, gelbe):
        a.cache = storage
    zeilen = []
    stat = run_telefonbuch(storage, [oertliche, gelbe], "SH", set(), fortschritt=zeilen.append)
    assert (stat.geprueft, stat.telefon, stat.website, stat.ohne) == (3, 2, 1, 1)
    assert stat.je_quelle == {"dasoertliche": 1, "gelbeseiten": 1}
    assert storage.get_kandidat(ole).telefon == "0431 9 87 65"
    k = storage.get_kandidat(imke)
    assert (k.telefon, k.domain, k.crawl_status) == ("0431 55 01 99", "thordsen-stb.example", SchrittStatus.OFFEN)
    assert storage.get_kandidat(ina).telefon is None and storage.cache_get(MERKER, str(ina))
    assert not any("Kai%20Web" in u for u in abrufe_o)  # mit Website: keine Suche
    assert pausen and set(pausen) == {30.0}  # Abstand wie beim amtlichen Verzeichnis

    werte = {z.name: luecken_werte(z) for z in luecken(storage, "SH")}
    spalte = LUECKEN_SPALTEN.index("Telefon-Quelle")
    assert werte["Steuerberater Ole Ketelsen"][spalte] == "Das Örtliche"
    assert werte["Steuerberaterin Imke Thordsen"][spalte] == "Gelbe Seiten"
    assert werte["Steuerberaterin Ina Namenlos"][spalte] is None
    herkunft = LUECKEN_SPALTEN.index("Website-Herkunft")
    assert werte["Steuerberaterin Imke Thordsen"][herkunft] == "Telefonbuch (Gelbe Seiten)"
    assert werte["Steuerberater Kai Web"][herkunft] == "Website aus anderer Quelle"

    # fortsetzbar: alles erledigt – keine neuen Abrufe
    vorher = len(abrufe_o) + len(abrufe_g)
    assert run_telefonbuch(storage, [oertliche, gelbe], "SH", set()).geprueft == 0
    assert len(abrufe_o) + len(abrufe_g) == vorher
