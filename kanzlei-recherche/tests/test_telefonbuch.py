"""Stufe C `recherche telefonbuch` (Umbau 05.11.2026): Ortssuche statt Namenssuche – Das Örtliche (Controller-URL,
Seitenzählung, Skript-Array handlerData mit Website/E-Mail), Rückfall Gelbe Seiten nur über Stadtseiten aus der
Sitemap; Zuordnung über Domain bzw. Nachname + PLZ/Straße, Protokoll eindeutig/mehrdeutig/kein Treffer.
Fiktive Seiten im Aufbau der echten (beispiele/dasoertliche_ort_*.html, gelbeseiten.html), kein Netz."""

from pathlib import Path

import httpx
import pytest

from recherche.config import VerzeichnisConfig
from recherche.models import Fund, Kandidat, Kanzlei, SchrittStatus
from recherche.search.base import SearchHit
from recherche.search.verzeichnisse import (
    DasOertlicheAdapter,
    GelbeSeitenAdapter,
    dasoertliche_eintraege,
)
from recherche.stbv_lauf import LUECKEN_SPALTEN, QUELLE, luecken, luecken_werte
from recherche.telefonbuch import (
    EINDEUTIG,
    KEIN_TREFFER,
    MEHRDEUTIG,
    MERKER,
    namenswoerter,
    passt,
    protokoll_csv,
    run_telefonbuch,
)

BEISPIELE = Path(__file__).parent / "fixtures" / "verzeichnisse" / "beispiele"
ECHT = Path(__file__).parent / "fixtures" / "verzeichnisse" / "echt"
OE_URL = "https://www.dasoertliche.de/Controller?form_name=search_nat&kw={rubrik}&ci={ort}&recFrom={start}"
GS_URL = "https://www.gelbeseiten.de/branchen/steuerberater/{ort_klein}"
GS_SITEMAP = "https://www.gelbeseiten.de/sitemap_index_TrefferlisteStadt.xml"


def _lesen(name):
    return (BEISPIELE / name).read_text(encoding="utf-8")


def _adapter(klasse, antwort, **cfg):
    abrufe, pausen = [], []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        abrufe.append(str(req.url))
        return antwort(str(req.url))

    c = VerzeichnisConfig(nur_ergaenzen=True, pause_s=30.0, pause_jitter_s=0.0, **cfg)
    return klasse(c, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=pausen.append), abrufe, pausen


def _oertliche(url):
    if "ci=Reinbek" in url:
        return httpx.Response(200, text=_lesen("dasoertliche_ort_2.html" if "recFrom=26" in url
                                               else "dasoertliche_ort_1.html"))
    return httpx.Response(200, text="<html><body>Keine Treffer</body></html>")


def _gelbe(url):
    if url == GS_SITEMAP:
        return httpx.Response(200, text=_lesen("gelbeseiten_sitemap_staedte.xml"))
    if url.endswith("/branchen/steuerberater/kiel"):
        return httpx.Response(200, text=_lesen("gelbeseiten.html"))
    return httpx.Response(404)


def _amtlich(storage, name, strasse, plz, ort):
    kid = storage.add_kandidat(Kandidat(bundesland="SH", name=name, strasse=strasse, plz=plz, ort=ort, quelle=QUELLE))
    storage.add_fund(Fund(kandidat_id=kid, bundesland="SH", quelle=QUELLE, quell_id=f"q-{kid}",
                          snippet="Amtliches Steuerberaterverzeichnis: 1 Berufsträger (x)"))
    return kid


def test_namenswoerter_und_treffer_nur_mit_nachname_und_plz_oder_strasse():
    assert namenswoerter("Steuerberaterin Imke Thordsen") == {"thordsen"}
    assert namenswoerter("Steuerberater Lund, Ott & Rau") == {"lund", "ott", "rau"}

    def h(name, strasse, plz):
        return SearchHit(quelle="dasoertliche", quell_id=name, name=name, strasse=strasse, plz=plz, ort="Kiel")

    args = ("Steuerberaterin Imke Thordsen", "Ring 1", "24103")
    assert passt(h("Thordsen, Imke Steuerberaterin", "Ring 1", "24103"), *args)
    assert passt(h("Thordsen Imke", "Ring 1", "24105"), *args)  # PLZ anders, Straße gleich
    assert not passt(h("Thordsen, Imke", "Wall 9", "24937"), *args)  # Namensvetterin anderswo
    assert not passt(h("Steuerbüro Lund", "Ring 1", "24103"), *args)  # gleiche Adresse, anderer Name


def test_dasoertliche_skriptarray_mit_website_und_email():
    hits = dasoertliche_eintraege(_lesen("dasoertliche_ort_1.html"), "https://www.dasoertliche.de/")
    lund = hits[0]
    assert (lund.name, lund.strasse, lund.plz, lund.ort, lund.telefon) == (
        "Lund Ole Steuerberater", "Gutenbergstr. 27", "21465", "Reinbek", "040 71 11 11 11")
    assert (lund.website, lund.email) == ("http://www.lund-steuer.example", "info@lund-steuer.example")
    assert "form_name=detail" in lund.roh_url and len(hits) == 6


@pytest.mark.skipif(not (ECHT / "dasoertliche_reinbek.html").exists(), reason="echte Seite nur lokal")
def test_echte_reinbek_seite():
    hits = dasoertliche_eintraege((ECHT / "dasoertliche_reinbek.html").read_text(encoding="utf-8"),
                                  "https://www.dasoertliche.de/")
    assert len(hits) == 12 and all(h.telefon for h in hits)
    assert sum(bool(h.website) for h in hits) >= 4


def test_ortssuche_seiten_zuordnung_rueckfall_und_protokoll(storage, tmp_path):
    lund = _amtlich(storage, "Steuerberater Ole Lund", "Gutenbergstr. 27", "21465", "Reinbek")
    ott = _amtlich(storage, "Steuerberaterin Eva Ott", "Hagebuttenweg 2", "21465", "Reinbek")
    rau = _amtlich(storage, "Steuerberater Kai Rau", "Sandweg 9", "21465", "Reinbek")
    behn = _amtlich(storage, "Steuerberaterin Ina Behn", "Kückallee 26", "21465", "Reinbek")  # Seite 2
    kock = _amtlich(storage, "Steuerberater Jan Kock", "Markt 1", "21465", "Reinbek")  # nicht im Telefonbuch
    imke = _amtlich(storage, "Steuerberaterin Imke Thordsen", "Ring 1", "24103", "Kiel")  # nur Gelbe Seiten
    nord = storage.add_kandidat(Kandidat(bundesland="SH", domain="nordlicht-stb.example",
                                         website="https://nordlicht-stb.example/", name="Nordlicht", ort="Reinbek"))
    storage.upsert_kanzlei(Kanzlei(kandidat_id=nord, lauf_bundesland="SH", kanzleiname="Nordlicht StBG mbH",
                                   domain="nordlicht-stb.example", plz="21465", ort="Reinbek"))  # ohne Telefon

    oe, abrufe_oe, pausen = _adapter(DasOertlicheAdapter, _oertliche, url_vorlage=OE_URL, max_seiten=20)
    gs, abrufe_gs, _ = _adapter(GelbeSeitenAdapter, _gelbe, url_vorlage=GS_URL, max_seiten=1, staedte_sitemap=GS_SITEMAP)
    for a in (oe, gs):
        a.cache = storage
    erreichbar = lambda url: "thordsen" not in url  # noqa: E731
    zeilen = []
    stat = run_telefonbuch(storage, [oe, gs], "SH", set(), fortschritt=zeilen.append, erreichbar=erreichbar)

    ergebnis = {z.offen.kandidat.id: z for z in stat.protokoll}
    assert {k: z.ergebnis for k, z in ergebnis.items()} == {
        lund: EINDEUTIG, ott: EINDEUTIG, rau: MEHRDEUTIG, behn: EINDEUTIG, kock: KEIN_TREFFER, imke: EINDEUTIG,
        nord: EINDEUTIG}
    k = storage.get_kandidat(lund)  # zwei Einträge, eine Nummer → eindeutig, Website aus dem Skript-Array
    assert (k.telefon, k.domain, k.crawl_status) == ("040 71 11 11 11", "lund-steuer.example", SchrittStatus.OFFEN)
    assert storage.get_kandidat(ott).domain is None and "ohne Namensbezug" in ergebnis[ott].grund
    assert storage.get_kandidat(rau).telefon is None and "2 passende Einträge" in ergebnis[rau].grund
    assert storage.get_kandidat(behn).telefon == "040 7 77 77 77"  # zweite Ergebnisseite
    assert storage.get_kandidat(nord).telefon == "040 7 66 66 66"  # Kanzlei mit Website: Telefon über die Domain
    i = storage.get_kandidat(imke)
    assert (i.telefon, i.domain, ergebnis[imke].quelle) == ("0431 55 01 99", None, "gelbeseiten")
    assert "nicht erreichbar" in ergebnis[imke].grund

    # Seiten: Reinbek 2 (Seitenzählung), Kiel 1; Gelbe Seiten: Sitemap + Kiel, Reinbek ohne Stadtseite → keine Abfrage
    assert sum("ci=Reinbek" in u for u in abrufe_oe) == 2 and sum("ci=Kiel" in u for u in abrufe_oe) == 1
    assert abrufe_gs == [GS_SITEMAP, "https://www.gelbeseiten.de/branchen/steuerberater/kiel"]
    assert pausen and set(pausen) == {30.0}
    assert (stat.eindeutig, stat.mehrdeutig, stat.kein_treffer) == (5, 1, 1)
    assert any("Reinbek" in z and "eindeutig" in z for z in zeilen)

    text = protokoll_csv(stat, tmp_path / "tb.csv").read_text(encoding="utf-8-sig")
    assert text.startswith("Kandidat;Art;Name") and "mehrdeutig" in text and "Website ohne Telefon" in text

    werte = {z.name: luecken_werte(z) for z in luecken(storage, "SH")}
    spalte = LUECKEN_SPALTEN.index("Telefon-Quelle")
    assert werte["Steuerberater Ole Lund"][spalte] == "Das Örtliche"
    assert werte["Steuerberaterin Imke Thordsen"][spalte] == "Gelbe Seiten"
    assert werte["Steuerberater Ole Lund"][LUECKEN_SPALTEN.index("Website-Herkunft")] == "Telefonbuch (Das Örtliche)"
    assert storage.cache_get(MERKER, str(kock))["ergebnis"] == KEIN_TREFFER

    # fortsetzbar: alles erledigt – keine neuen Abfragen
    vorher = len(abrufe_oe) + len(abrufe_gs)
    assert run_telefonbuch(storage, [oe, gs], "SH", set()).protokoll == []
    assert len(abrufe_oe) + len(abrufe_gs) == vorher
