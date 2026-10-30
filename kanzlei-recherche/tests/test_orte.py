import json

import httpx
import pytest
from conftest import FIXTURES
from openpyxl import Workbook

from recherche.config import GvSpalten, StadtteilConfig
from recherche.orte import (
    OpenPlzOrte,
    OrteFehler,
    gemeinde_name,
    gemeinden_aus_localities,
    gv_quelle_laden,
    kreis_bezeichnung,
    lade_gv,
    orte_erzeugen,
    vergleichen,
)  # fmt: skip
from recherche.search.pipeline import Ort, load_orte, orte_filtern, schreibe_orte

SEITEN = [json.loads((FIXTURES / "openplz" / f"sh_localities_seite{i}.json").read_text(encoding="utf-8")) for i in (1, 2, 3)]
GV_CSV = FIXTURES / "gv_isys" / "ausschnitt_sh.csv"


def openplz(mit_header=True, fehler=None):
    anfragen = []

    def handler(req: httpx.Request):
        anfragen.append(dict(req.url.params))
        if fehler:
            return httpx.Response(fehler.pop(0))
        seite = int(req.url.params["page"])
        daten = SEITEN[seite - 1] if seite <= 3 else []
        return httpx.Response(200, json=daten, headers={"x-total-pages": "3"} if mit_header else {})

    return httpx.Client(transport=httpx.MockTransport(handler)), anfragen


def _quelle(client, cache=None, page_size=5):
    return OpenPlzOrte(cache=cache, client=client, page_size=page_size, pause_s=0.0, sleep=lambda s: None)


# ---------------------------------------------------------------- OpenPLZ


def test_paging_mit_header():
    client, anfragen = openplz()
    locs = _quelle(client).localities("01")
    assert len(locs) == 13
    assert [a["page"] for a in anfragen] == ["1", "2", "3"]
    assert anfragen[0]["pageSize"] == "5"


def test_paging_ohne_header_endet_an_kurzer_seite():
    client, anfragen = openplz(mit_header=False)
    assert len(_quelle(client).localities("01")) == 13
    assert len(anfragen) == 3  # Seite 3 hat nur 3 von 5 Einträgen


def test_paging_volle_letzte_seite_ohne_header():
    """Ohne Header und mit voller letzter Seite endet das Paging an der ersten leeren Seite."""
    anfragen = []

    def handler(req):
        anfragen.append(req.url.params["page"])
        seite = int(req.url.params["page"])
        return httpx.Response(200, json=SEITEN[seite - 1] if seite <= 2 else [])

    q = _quelle(httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(q.localities("01")) == 10
    assert anfragen == ["1", "2", "3"]


def test_retry_bei_5xx_dann_erfolg():
    antworten = [httpx.Response(503), httpx.Response(200, json=SEITEN[2])]
    pausen = []
    q = OpenPlzOrte(client=httpx.Client(transport=httpx.MockTransport(lambda r: antworten.pop(0))),
                    page_size=5, pause_s=0, sleep=pausen.append)
    assert len(q.localities("01")) == 3
    assert 2.0 in pausen


def test_dauerhafter_fehler():
    q = OpenPlzOrte(client=openplz(fehler=[503] * 4)[0], pause_s=0, sleep=lambda s: None)
    with pytest.raises(OrteFehler, match="HTTP 503"):
        q.localities("01")


def test_cache(storage):
    client, anfragen = openplz()
    _quelle(client, cache=storage).localities("01")
    client2, anfragen2 = openplz()
    assert len(_quelle(client2, cache=storage).localities("01")) == 13
    assert anfragen2 == []


def test_gemeinden_aus_localities():
    g = gemeinden_aus_localities([e for s in SEITEN for e in s])
    assert len(g) == 8
    kiel = g["01002000"]
    assert (kiel.name, kiel.plz, kiel.kreis_openplz) == ("Kiel", ["24103", "24105", "24159"], "Kreisfreie Stadt Kiel")
    assert kiel.ortsnamen["24159"] == {"Kiel", "Kiel-Holtenau"}
    assert g["01051044"].name == "Heide" and g["01051044"].kreis_openplz == "Kreis Dithmarschen"


@pytest.mark.parametrize(("roh", "name"), [("Kiel, Landeshauptstadt", "Kiel"), ("Glücksburg (Ostsee), Stadt", "Glücksburg (Ostsee)"),
                                           ("Reinfeld (Holstein), Stadt", "Reinfeld (Holstein)"), ("Hasselberg", "Hasselberg")])
def test_gemeinde_name(roh, name):
    assert gemeinde_name(roh) == name


def test_kreis_bezeichnung():
    assert kreis_bezeichnung("Dithmarschen", "Kreis") == "Kreis Dithmarschen"
    assert kreis_bezeichnung("Kiel, Landeshauptstadt", "Kreisfreie Stadt") == "Kreisfreie Stadt Kiel"
    assert kreis_bezeichnung("Kreis Plön", "Kreis") == "Kreis Plön"


# ---------------------------------------------------------------- GV-ISys


def test_gv_csv():
    gv = lade_gv(GV_CSV, GvSpalten(), "01")
    assert gv.einwohner["01002000"] == 247717 and gv.einwohner["01051130"] == 1999
    assert gv.namen["01002000"] == "Kiel"
    assert gv.kreise["01002"] == "Kreisfreie Stadt Kiel"
    assert gv.kreise["01051"] == "Kreis Dithmarschen"
    assert gv.kreise["01058"] == "Kreis Rendsburg-Eckernförde"
    assert gv.kreisfrei == {"01001", "01002", "01003"}
    assert "02000000" not in gv.einwohner  # anderes Bundesland


def _gv_xlsx(pfad):
    """Wie der Destatis-Auszug: Titelzeilen, zweizeiliger Kopf, Zahlen als Text bzw. Zahl."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Onlineprodukt_Gemeinden"
    ws.append(["Gemeindeverzeichnis – Alle politisch selbständigen Gemeinden"])
    ws.append([])
    ws.append(["Satz-art", "Text-kenn-zeichen", "Amtlicher Regionalschlüssel (ARS)", None, None, None, None,
               "Gemeindename", "Fläche km²", "Bevölkerung", None, None, "Post-leit-zahl"])
    ws.append([None, None, "Land", "RB", "Kreis", "VB", "Gem", None, None, "insgesamt", "männlich", "weiblich", None])
    ws.append([])
    ws.append(["40", "41", "01", "0", "02", None, None, "Kiel, Landeshauptstadt", 118.65, 247717, None, None, None])
    ws.append(["60", "61", "01", "0", "02", "0000", "000", "Kiel, Landeshauptstadt", 118.65, 247717, 120100, 127617, "24103"])
    ws.append(["40", "43", "01", "0", "51", None, None, "Dithmarschen", 1428.17, 134000, None, None, None])
    ws.append([60, 63, 1, 0, 51, 51, 44, "Heide, Stadt", 31.97, "21 540", None, None, "25746"])
    wb.save(pfad)
    return pfad


def test_gv_xlsx_mit_mehrzeiligem_kopf(tmp_path):
    gv = lade_gv(_gv_xlsx(tmp_path / "gv.xlsx"), GvSpalten(), "01")
    assert gv.einwohner == {"01002000": 247717, "01051044": 21540}
    assert gv.kreise == {"01002": "Kreisfreie Stadt Kiel", "01051": "Kreis Dithmarschen"}


def test_gv_csv_mit_ags_ohne_satzart(tmp_path):
    p = tmp_path / "gv.csv"
    p.write_text("AGS,Name,Einwohner,Kreisname\n01002000,\"Kiel, Landeshauptstadt\",247717,Kreisfreie Stadt Kiel\n"
                 "01051044,\"Heide, Stadt\",21540,Kreis Dithmarschen\n", encoding="utf-8")
    spalten = GvSpalten(satzart=None, textkennzeichen=None, ags="ags", name="name", einwohner="einwohner", kreisname="kreisname")
    gv = lade_gv(p, spalten, "01")
    assert gv.einwohner == {"01002000": 247717, "01051044": 21540}
    assert gv.kreis_je_ags["01051044"] == "Kreis Dithmarschen"


def test_gv_fehlende_spalte_mit_hinweis(tmp_path):
    with pytest.raises(OrteFehler, match="einwohner.*Gefundene Spalten.*gemeindename.*settings.yaml"):
        lade_gv(GV_CSV, GvSpalten(einwohner="bewohner"), "01")


def test_gv_quelle(tmp_path):
    with pytest.raises(OrteFehler, match="fehlt.*Destatis"):
        gv_quelle_laden(str(tmp_path / "gibtsnicht.xlsx"), tmp_path)
    aufrufe = []

    def handler(req):
        aufrufe.append(str(req.url))
        return httpx.Response(200, content=GV_CSV.read_bytes())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = gv_quelle_laden("https://example.org/daten/gv.csv", tmp_path, client)
    assert p == tmp_path / "gv.csv" and p.read_bytes() == GV_CSV.read_bytes()
    gv_quelle_laden("https://example.org/daten/gv.csv", tmp_path, client)
    assert len(aufrufe) == 1  # schon geladen


# ---------------------------------------------------------------- Zusammenführen


@pytest.fixture
def ergebnis():
    return orte_erzeugen(gemeinden_aus_localities([e for s in SEITEN for e in s]), lade_gv(GV_CSV, GvSpalten(), "01"),
                         StadtteilConfig())


def test_orte_je_gemeinde_mit_einwohnern_und_kreis(ergebnis):
    g = {o.name: o for o in ergebnis.gemeinden}
    assert len(g) == 8
    kiel = g["Kiel"]
    assert (kiel.plz, kiel.plz_weitere, kiel.kreis, kiel.einwohner, kiel.ags) == (
        "24103", ("24105", "24159"), "Kreisfreie Stadt Kiel", 247717, "01002000")
    assert g["Heide"].kreis == "Kreis Dithmarschen" and g["Heide"].einwohner == 21540


def test_gleiche_namen_eindeutig(ergebnis):
    namen = {o.name for o in ergebnis.gemeinden}
    assert {"Hasselberg (Rendsburg-Eckernförde)", "Hasselberg (Schleswig-Flensburg)"} <= namen
    assert any("mehrfach" in w for w in ergebnis.warnungen)


def test_warnungen_fuer_abweichende_quellen(ergebnis):
    assert any("Neuenkirchen" in w and "nicht in OpenPLZ" in w for w in ergebnis.warnungen)


def test_zusatz_suchorte_nur_grosse_kreisfreie_staedte(ergebnis):
    z = ergebnis.zusatz
    assert {o.gemeinde for o in z} == {"Kiel", "Lübeck"}  # Flensburg < 100.000
    kiel = [o for o in z if o.gemeinde == "Kiel"]
    assert [o.name for o in kiel] == ["Kiel (24103)", "Kiel (24105)", "Kiel (24159)"]
    assert [o.stadtteil for o in kiel] == [None, None, "Kiel-Holtenau"]
    assert all(o.einwohner is None and o.kreis == "Kreisfreie Stadt Kiel" for o in kiel)
    # Zusatzorte folgen direkt ihrer Gemeinde
    namen = [o.name for o in ergebnis.orte]
    assert namen[namen.index("Kiel") + 1] == "Kiel (24103)"


def test_zusatz_abschaltbar():
    erg = orte_erzeugen(gemeinden_aus_localities([e for s in SEITEN for e in s]), lade_gv(GV_CSV, GvSpalten(), "01"),
                        StadtteilConfig(aktiv=False))
    assert erg.zusatz == []


def test_ohne_gv():
    erg = orte_erzeugen(gemeinden_aus_localities([e for s in SEITEN for e in s]), None, StadtteilConfig())
    kiel = next(o for o in erg.gemeinden if o.name == "Kiel")
    assert (kiel.einwohner, kiel.kreis) == (None, "Kreisfreie Stadt Kiel")
    assert erg.zusatz == []  # ohne Einwohnerzahl keine Stadtteil-Suche


def test_csv_roundtrip(ergebnis, tmp_path):
    p = schreibe_orte(ergebnis.orte, tmp_path / "orte.csv")
    assert p.read_text(encoding="utf-8").splitlines()[0] == "ort;plz;kreis;einwohner;ags;plz_weitere;gemeinde;stadtteil"
    geladen = load_orte(p)
    assert geladen == ergebnis.orte
    assert len(load_orte(p, mit_zusatz=False)) == 8


def test_startliste_bleibt_lesbar(settings):
    """Alte Startliste (80 Orte, nur ort;plz;kreis;einwohner) bleibt lesbar – die echte Liste ist inzwischen ersetzt."""
    orte = load_orte(FIXTURES / "orte" / "startliste_alt.csv")
    assert len(orte) == 80 and all(o.ags is None and not o.ist_zusatz for o in orte)
    assert load_orte(settings.pfad(settings.bundesland("SH").orte_datei))  # aktuelle Liste lesbar


def test_orte_filtern_mit_zusatzorten(ergebnis):
    auswahl = orte_filtern(ergebnis.orte, ["Kiel"])
    assert [o.name for o in auswahl] == ["Kiel", "Kiel (24103)", "Kiel (24105)", "Kiel (24159)"]


def test_vergleich(ergebnis):
    alt = [Ort("Kiel", "24103"), Ort("Plön", "24306"), Ort("Heide", "25746")]
    v = vergleichen(alt, ergebnis.orte)
    assert (v.alt, v.neu, v.entfallen, v.zusatz) == (3, 8, ["Plön"], 5)
    assert "Lübeck" in v.hinzu and v.mit_einwohnern == 8


def test_stufen_verteilung(ergebnis):
    from recherche.config import load_settings
    from recherche.orte import stufen_verteilung
    from recherche.search.pipeline import Suchplan

    v = stufen_verteilung(ergebnis.orte, Suchplan.aus_settings(load_settings()))
    # keine Gemeinde der Testdaten liegt zwischen 2.000 und 19.999 → Stufe fehlt
    assert list(v) == ["unter 2.000 Einwohner", "ab 20.000 Einwohner", "Zusatz-Suchorte"]
    assert v["unter 2.000 Einwohner"] == (2, 2, 1)  # Wesselburenerkoog (1.999), Hasselberg/SL (420)
    assert v["ab 20.000 Einwohner"] == (6, 24, 3)  # inkl. Hasselberg/RD mit genau 20.000
    assert v["Zusatz-Suchorte"] == (5, 11, 2)


def test_gleiche_namen_im_selben_kreis_mit_namenszusatz():
    """Garding (Stadt) und Garding, Kirchspiel liegen beide in Nordfriesland – der Kreis unterscheidet nicht."""
    locs = [{"postalCode": "25836", "name": "Garding", "municipality": {"key": ags, "name": name},
             "district": {"name": "Nordfriesland", "type": "Kreis"}}
            for ags, name in (("01054036", "Garding, Stadt"), ("01054035", "Garding, Kirchspiel"))]
    erg = orte_erzeugen(gemeinden_aus_localities(locs), None)
    assert sorted(o.name for o in erg.gemeinden) == ["Garding (Kirchspiel)", "Garding (Stadt)"]
