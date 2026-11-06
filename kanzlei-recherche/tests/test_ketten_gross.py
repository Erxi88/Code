"""Großgesellschaften in ketten.yaml (06.11.2026): Link-Liste (PwC u. a.), Standortdaten aus JSON (ECOVIS im Quelltext,
EY als Datei), Karten (HLB) – je Standort eine Zeile im Land seiner PLZ, kein Doppel zum Verzeichnis-Eintrag an derselben
Adresse, Abgleich gegen das amtliche Verzeichnis. Fiktive Daten im Aufbau der echten Seiten, kein Netz."""

import json

from recherche.ketten import KettenConfig
from recherche.ketten_crawl import (
    GLEICHE_ADRESSE,
    NICHT_IM_VERZEICHNIS,
    NUR_IM_VERZEICHNIS,
    ecovis_standorte,
    ey_standorte,
    hlb_standorte,
    kette_im_namen,
    ketten_abgleich,
    standort_links,
    standortdaten_anlegen,
    standorte_anlegen,
)
from recherche.models import Fund, Kandidat
from recherche.search.pipeline import Ort
from recherche.stbv_lauf import LandNachOrt

ECOVIS = ('<script>var offices = [{"name":"ECOVIS Muster Steuerberatungsgesellschaft mbH, Niederlassung L\\u00fcbeck",'
          '"address":{"street":"Hafenstra\\u00dfe 1","postalCode":"23554","town":"L\\u00fcbeck","lat":"53.8","lng":"10.6"},'
          '"website":"https:\\/\\/www.ecovis.com\\/luebeck-muster","email":"luebeck@ecovis.example","phone":"+49 451 111 11"},'
          '{"name":"ECOVIS Istanbul","address":{"street":"Cadde 1","postalCode":"34394","town":"Istanbul","lat":"41",'
          '"lng":"28"},"website":"https:\\/\\/www.ecovis.com\\/istanbul","email":"ist@ecovis.example","phone":"+90 212 1"},'
          '{"name":"ECOVIS Nord Steuerberater","address":{"street":"Markt 5","postalCode":"20095","town":"Hamburg",'
          '"lat":"53.5","lng":"10"},"website":"https:\\/\\/www.ecovis.com\\/hamburg-nord","email":"","phone":"+49 40 2 22"}];'
          '</script>')
EY = json.dumps({"countries": [
    {"countryCode": "DE", "cities": [{"name": "Kiel", "offices": [
        {"officeName": "EY", "officeAddress": "<p>Wall 9</p>\n", "officePostalCode": "24103", "cityName": "Kiel",
         "phoneInformationDirect": ["+49 431 333 0"]},
        {"officeName": "EY wavespace Kiel", "officeAddress": "Wall 10", "officePostalCode": "24103", "cityName": "Kiel"}]}]},
    {"countryCode": "AT", "cities": [{"name": "Wien", "offices": [{"officeName": "EY", "officePostalCode": "1220"}]}]}]})
HLB = ("<div><h4>Flensburg</h4><h5>Muster, Lund &amp; Partner</h5><p>Address:</p><p>Europastraße 33 a</p>"
       "<p>24976 Handewitt, Germany</p><p>Telefon</p><p>: +49 (0)461 9 99 99 99</p><p>Website</p><p>:</p>"
       "<p>www.muster-lund.example</p><p>Stellenangebote</p></div>")
PWC = ('<a href="/de/standorte/kiel.html">Kiel</a> <a href="/de/standorte/hamburg.html">Hamburg</a>'
       '<a href="/de/pwcs-trade-office.html">Trade</a> <a href="/de/standorte.html">Alle</a>')

CFG = KettenConfig.laden_aus({"ketten": {
    "pwc": {"name": "PwC Muster", "zentrale": "pwc.example", "uebersicht": "https://www.pwc.example/de/standorte.html",
            "link_muster": r"https://www\.pwc\.example/de/standorte/[a-z0-9-]+\.html$", "standortmuster": "pfad"},
    "ecovis": {"name": "ECOVIS", "zentrale": "ecovis.com", "uebersicht": "https://de.ecovis.com/standorte/",
               "format": "ecovis_json", "link_muster": r"https://www\.ecovis\.com/[a-z0-9-]+$", "standortmuster": "pfad"},
    "ey": {"name": "EY", "zentrale": "ey.com", "format": "ey_json", "standortmuster": "pfad",
           "daten_url": "https://www.ey.com/offices.json"},
    "hlb": {"name": "HLB Deutschland", "zentrale": "hlb-deutschland.example", "format": "hlb_karten",
            "standortmuster": "domain"}}})
KETTE = {k.schluessel: k for k in CFG.ketten}
LAND = LandNachOrt({"SH": [Ort("Kiel", "24103"), Ort("Lübeck", "23554"), Ort("Handewitt", "24976")],
                    "HH": [Ort("Hamburg", "20095")]})


def test_standortdaten_der_drei_formate():
    eco = ecovis_standorte(ECOVIS)
    assert [(d.name, d.strasse, d.plz, d.ort, d.telefon, d.email, d.website) for d in eco] == [
        ("ECOVIS Muster Steuerberatungsgesellschaft mbH, Niederlassung Lübeck", "Hafenstraße 1", "23554", "Lübeck",
         "+49 451 111 11", "luebeck@ecovis.example", "https://www.ecovis.com/luebeck-muster"),
        ("ECOVIS Nord Steuerberater", "Markt 5", "20095", "Hamburg", "+49 40 2 22", None,
         "https://www.ecovis.com/hamburg-nord")]  # Istanbul raus
    ey = ey_standorte(EY)
    assert [(d.name, d.strasse, d.plz, d.telefon, d.website) for d in ey] == [
        ("EY Kiel", "Wall 9", "24103", "+49 431 333 0", None)]  # ohne wavespace, ohne Ausland
    hlb = hlb_standorte(HLB)
    assert [(d.name, d.strasse, d.plz, d.ort, d.telefon, d.website) for d in hlb] == [
        ("Muster, Lund & Partner", "Europastraße 33 a", "24976", "Handewitt", "+49 (0)461 9 99 99 99",
         "https://www.muster-lund.example/")]
    assert [u.rsplit("/", 1)[1] for u in standort_links(PWC, KETTE["pwc"].uebersicht, KETTE["pwc"])] == [
        "kiel.html", "hamburg.html"]
    assert (kette_im_namen("PricewaterhouseCoopers GmbH WPG"), kette_im_namen("Treuhand Hannover GmbH StBG"),
            kette_im_namen("Steuerbüro Lund")) == ("pwc", "treuhand-hannover", None)


def test_standorte_im_land_ihrer_plz_ohne_doppel_und_abgleich(storage):
    # amtlicher Eintrag an der Adresse des EY-Büros und ein ECOVIS-Eintrag ohne Standortzeile
    ey_amtlich = storage.add_kandidat(Kandidat(bundesland="SH", name="Ernst & Young GmbH Wirtschaftsprüfungsgesellschaft",
                                               strasse="Wall 9", plz="24103", ort="Kiel", quelle="stb_verzeichnis"))
    eco_nur = storage.add_kandidat(Kandidat(bundesland="SH", name="ECOVIS Muster Steuerberatungsgesellschaft mbH",
                                            strasse="Holstenstraße 3", plz="24103", ort="Kiel", quelle="stb_verzeichnis"))
    pwc_amtlich = storage.add_kandidat(Kandidat(bundesland="SH", name="PwC Muster Kiel", strasse="Damm 4", plz="24103",
                                                ort="Kiel", quelle="stb_verzeichnis"))
    for i, kid in enumerate((ey_amtlich, eco_nur, pwc_amtlich)):
        storage.add_fund(Fund(kandidat_id=kid, bundesland="SH", quelle="stb_verzeichnis", quell_id=f"a{i}"))

    s_eco = standortdaten_anlegen(storage, "SH", KETTE["ecovis"], ecovis_standorte(ECOVIS), "l1", LAND)
    assert s_eco.je_land == {"SH": 1, "HH": 1} and s_eco.neu == 3  # 2 Standorte + Zentrale
    hh = storage.kandidaten("HH")
    assert [(k.name, k.domain, k.website, k.telefon) for k in hh] == [
        ("ECOVIS Nord Steuerberater", None, "https://www.ecovis.com/hamburg-nord", "+49 40 2 22")]
    s_ey = standortdaten_anlegen(storage, "SH", KETTE["ey"], ey_standorte(EY), "l1", LAND)
    assert (s_ey.neu, s_ey.bekannt) == (1, 1)  # EY Kiel = amtlicher Eintrag an derselben Adresse; neu nur die Zentrale
    assert any(f.quelle == "kette" for f in storage.funde_kandidat(ey_amtlich))
    standortdaten_anlegen(storage, "SH", KETTE["hlb"], hlb_standorte(HLB), "l1", LAND)
    assert storage.kandidat_nach_domain("muster-lund.example").plz == "24976"
    standorte_anlegen(storage, "SH", KETTE["pwc"], standort_links(PWC, KETTE["pwc"].uebersicht, KETTE["pwc"]), "l1")
    # wiederholbar: keine neuen Kandidaten
    vorher = len(storage.kandidaten("SH"))
    assert standortdaten_anlegen(storage, "SH", KETTE["ecovis"], ecovis_standorte(ECOVIS), "l2", LAND).neu == 0
    assert len(storage.kandidaten("SH")) == vorher

    z = {(a.kette, a.ergebnis) for a in ketten_abgleich(storage, "SH", CFG.ketten, LAND)}
    assert ("ey", GLEICHE_ADRESSE) in z  # EY Kiel im Verzeichnis
    assert ("ecovis", NICHT_IM_VERZEICHNIS) in z  # ECOVIS Lübeck fehlt im Verzeichnis
    assert ("ecovis", NUR_IM_VERZEICHNIS) in z  # ECOVIS Kiel nur im Verzeichnis
    assert ("pwc", "Adresse noch unbekannt – erst crawl/extract") in z
    pwc_kiel = next(a for a in ketten_abgleich(storage, "SH", CFG.ketten, LAND) if a.standort.endswith("kiel.html"))
    assert "im Ort: PwC Muster Kiel" in pwc_kiel.verzeichnis  # Verzeichnis-Eintrag im Ort aus dem Pfad
    assert not any(a.adresse.endswith("Hamburg") for a in ketten_abgleich(storage, "SH", CFG.ketten, LAND))
