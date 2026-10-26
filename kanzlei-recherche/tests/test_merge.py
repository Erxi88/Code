from datetime import datetime

from recherche.extract.base import LlmErgebnis, LlmPerson, RegelErgebnis
from recherche.extract.merge import (
    ansprechpartner,
    discovery_werte,
    gleiche_person,
    ist_berufstraeger,
    teamgroesse,
    zusammenfuehren,
)  # fmt: skip
from recherche.models import Fund, Kandidat, Konfidenz, Seite, Seitentyp, TeamKlasse
from recherche.normalize import PlzPruefung

K = Kandidat(id=7, bundesland="SH", domain="hansen.example", website="https://hansen.example/",
             name="Startseite – Hansen & Petersen")  # Websuche-Titel
WEB = Fund(kandidat_id=7, bundesland="SH", quelle="brave", quell_id="u", name="Startseite – Hansen & Petersen",
           website="https://hansen.example/")
PLACES = Fund(kandidat_id=7, bundesland="SH", quelle="places", quell_id="p1", name="Hansen & Petersen Steuerberater",
              strasse="Holstenstr. 42", plz="24103", ort="Kiel", telefon="0431 9987610", roh_url="https://maps.example/p1")
REGEL = RegelErgebnis(strasse="Holstenstraße 42", plz="24103", ort="Kiel", telefon="+49 431 9987610", email="info@hansen.example")
P = [LlmPerson(name="Jens Hansen", titel="Dipl.-Kfm.", funktion="Steuerberater, Partner"),
     LlmPerson(name="Maren Petersen", funktion="Steuerberaterin, Partnerin"),
     LlmPerson(name="Ole Brandt", titel="M.Sc.", funktion="Steuerberater"),
     LlmPerson(name="Sabine Krause", funktion="Steuerfachangestellte"),
     LlmPerson(name="Tim Jensen", funktion="Auszubildender")]
LLM = LlmErgebnis(kanzleiname="Hansen & Petersen Steuerberater PartG mbB", rechtsform="PartG mbB",
                  verantwortliche_person="Maren Petersen", personen=P, strasse="Falsche Str. 1", plz="24105", ort="Kiel",
                  telefon="0431 000000", email="llm@hansen.example")
DATUM = datetime(2026, 10, 26, 10, 0)
SEITEN = {
    Seitentyp.START: Seite(url="https://hansen.example/", domain="hansen.example", text="Start",
                           final_url="https://www.hansen.example/", abgerufen_am=DATUM),
    Seitentyp.IMPRESSUM: Seite(url="https://www.hansen.example/impressum", domain="hansen.example", text="Impressum",
                               abgerufen_am=DATUM),
    Seitentyp.TEAM: Seite(url="https://www.hansen.example/team", domain="hansen.example", abgerufen_am=DATUM,
                          text="Unser Team\nJens Hansen\nMaren Petersen\nOle Brandt\nSabine Krause\nTim Jensen"),
}
OK = lambda plz, ort: PlzPruefung(True, "Schleswig-Holstein")  # noqa: E731


def test_websuche_titel_ist_kein_discovery_name():
    assert discovery_werte([WEB])["name"] is None
    assert discovery_werte([WEB, PLACES])["name"] == "Hansen & Petersen Steuerberater"


def test_ohne_stufe_b_kommt_alles_von_der_website():
    k = zusammenfuehren(K, [WEB], SEITEN, REGEL, LLM, OK)
    assert k.kanzleiname == "Hansen & Petersen Steuerberater PartG mbB"  # LLM, nicht Websuche-Titel
    assert (k.strasse, k.plz, k.ort, k.telefon, k.email) == ("Holstenstraße 42", "24103", "Kiel", "+49 431 9987610",
                                                              "info@hansen.example")  # Regeln vor LLM
    assert k.abweichung is None
    assert k.website == "https://www.hansen.example/" and k.website_vorhanden
    assert k.bundesland == "Schleswig-Holstein" and k.plz_verifiziert
    assert k.konfidenz == Konfidenz.HOCH
    assert k.abrufdatum == DATUM.date()
    assert k.quell_urls == ["https://www.hansen.example/impressum", "https://www.hansen.example/team"]


def test_llm_fuellt_nur_was_regeln_nicht_finden():
    k = zusammenfuehren(K, [], SEITEN, RegelErgebnis(), LLM)
    assert (k.strasse, k.plz, k.telefon, k.email) == ("Falsche Str. 1", "24105", "+49 431 000000", "llm@hansen.example")


def test_discovery_wird_nie_ueberschrieben_abweichung_vermerkt():
    places = PLACES.model_copy(update={"telefon": "0431 111111", "name": "Steuerkanzlei Müller", "strasse": "Wall 5",
                                       "plz": "24103"})
    k = zusammenfuehren(K, [WEB, places], SEITEN, REGEL, LLM, OK)
    assert (k.kanzleiname, k.strasse, k.telefon) == ("Steuerkanzlei Müller", "Wall 5", "+49 431 111111")
    assert "Name: places „Steuerkanzlei Müller“ / Website „Hansen & Petersen Steuerberater PartG mbB“" in k.abweichung
    assert "Adresse: places Wall 5, 24103 Kiel / Website Holstenstraße 42, 24103 Kiel" in k.abweichung
    assert "Telefon: places +49 431 111111 / Website +49 431 9987610" in k.abweichung
    assert "https://maps.example/p1" in k.quell_urls


def test_gleiche_angaben_andere_schreibweise_keine_abweichung():
    k = zusammenfuehren(K, [WEB, PLACES], SEITEN, REGEL, LLM, OK)
    assert k.abweichung is None
    assert k.strasse == "Holstenstr. 42"  # Discovery-Schreibweise bleibt


def test_ansprechpartner_nur_berufstraeger_und_leitung():
    aps = ansprechpartner(LLM)
    assert [a.name for a in aps] == ["Maren Petersen", "Jens Hansen", "Ole Brandt"]  # Verantwortliche zuerst
    assert aps[1].titel == "Dipl.-Kfm."
    k = zusammenfuehren(K, [], SEITEN, REGEL, LLM)
    assert k.berufstraeger_anzahl == 3
    assert k.hauptansprechpartner.name == "Maren Petersen"


def test_verantwortliche_person_nur_im_impressum():
    llm = LLM.model_copy(update={"verantwortliche_person": "Dr. Karl Inhaber", "personen": P[3:]})
    aps = ansprechpartner(llm)
    assert [a.name for a in aps] == ["Dr. Karl Inhaber"]
    assert aps[0].funktion == "laut Impressum verantwortlich"
    assert not ist_berufstraeger(aps[0])


def test_leitung_ohne_berufstraeger_zaehlt_nicht_als_berufstraeger():
    llm = LlmErgebnis(personen=[LlmPerson(name="Anna Bauer", funktion="Geschäftsführerin"),
                                LlmPerson(name="Jan Ott", funktion="StB")])
    k = zusammenfuehren(K, [], {}, None, llm)
    assert [a.name for a in k.ansprechpartner] == ["Anna Bauer", "Jan Ott"]
    assert k.berufstraeger_anzahl == 1


def test_gleiche_person():
    assert gleiche_person("Dipl.-Kfm. Jens Hansen", "Jens Hansen")
    assert gleiche_person("Dr. rer. pol. Henrik Lorenzen", "Henrik Lorenzen")
    assert not gleiche_person("Jens Hansen", "Jan Hansen")
    assert not gleiche_person(None, "Jens Hansen")


def test_teamgroesse_genannte_zahl_vor_zaehlung():
    llm = LLM.model_copy(update={"mitarbeiterzahl": 18, "mitarbeiterzahl_beleg": "18 Mitarbeitende"})
    assert teamgroesse(llm, SEITEN[Seitentyp.TEAM]) == (18, None, "Text: „18 Mitarbeitende“")


def test_teamgroesse_alle_personen_der_teamseite():
    assert teamgroesse(LLM, SEITEN[Seitentyp.TEAM]) == (5, None, "5 Personen auf Teamseite")
    k = zusammenfuehren(K, [], SEITEN, REGEL, LLM)
    assert (k.teamgroesse, k.teamgroesse_klasse) == (5, TeamKlasse.K1_5)


def test_teamgroesse_klasse_wenn_gezaehlt_kleiner():
    llm = LLM.model_copy(update={"teamgroesse_klasse": TeamKlasse.K16_50, "personen": P[:2]})
    assert teamgroesse(llm, SEITEN[Seitentyp.TEAM]) == (None, TeamKlasse.K16_50, "Einschätzung aus dem Text")


def test_teamgroesse_ohne_teamseite():
    assert teamgroesse(LLM, None) == (None, None, None)
    assert teamgroesse(None, SEITEN[Seitentyp.TEAM]) == (None, None, None)


def test_konfidenz_mittel_und_niedrig():
    nicht_pruefbar = lambda plz, ort: PlzPruefung(None, hinweis="OpenPLZ nicht erreichbar")  # noqa: E731
    assert zusammenfuehren(K, [], SEITEN, REGEL, LLM, nicht_pruefbar).konfidenz == Konfidenz.MITTEL
    assert zusammenfuehren(K, [], SEITEN, REGEL, LLM).konfidenz == Konfidenz.MITTEL
    assert zusammenfuehren(K, [], SEITEN, RegelErgebnis(plz="24103"), None).konfidenz == Konfidenz.NIEDRIG


def test_falsche_plz_wird_vermerkt():
    falsch = lambda plz, ort: PlzPruefung(False, "Schleswig-Holstein", hinweis="Ort 'Kiel' passt nicht zu PLZ 24103")  # noqa: E731
    k = zusammenfuehren(K, [], SEITEN, REGEL, LLM, falsch)
    assert k.konfidenz == Konfidenz.MITTEL and "PLZ/Ort" in k.abweichung


def test_kandidat_ohne_website_nur_discovery():
    ohne = Kandidat(id=9, bundesland="SH", name="Kanzlei Ohneweb", strasse="Wall 5", plz="24103", ort="Kiel")
    fund = Fund(kandidat_id=9, bundesland="SH", quelle="gelbeseiten", quell_id="g1", name="Kanzlei Ohneweb",
                strasse="Wall 5", plz="24103", ort="Kiel", telefon="0431 222222", gefunden_am=DATUM)
    k = zusammenfuehren(ohne, [fund], {}, None, None, OK)
    assert (k.kanzleiname, k.telefon, k.website, k.website_vorhanden) == ("Kanzlei Ohneweb", "+49 431 222222", None, False)
    assert k.domain is None and k.berufstraeger_anzahl is None
    assert k.konfidenz == Konfidenz.MITTEL  # kein Impressum → nie „hoch"
    assert k.abrufdatum == DATUM.date()


def test_discovery_ohne_strasse_wird_mit_website_adresse_vervollstaendigt():
    ohne_strasse = PLACES.model_copy(update={"strasse": None})
    k = zusammenfuehren(K, [ohne_strasse], SEITEN, REGEL, LLM, OK)
    assert (k.strasse, k.plz, k.ort) == ("Holstenstraße 42", "24103", "Kiel") and k.abweichung is None
    andere_plz = PLACES.model_copy(update={"strasse": None, "plz": "24105"})
    k = zusammenfuehren(K, [andere_plz], SEITEN, REGEL, LLM, OK)
    assert (k.strasse, k.plz) == (None, "24105") and "Adresse" in k.abweichung


def test_llm_strasse_wenn_regel_nur_plz_findet():
    regel = RegelErgebnis(plz="22761", ort="Hamburg", telefon="+49 40 123456")
    llm = LlmErgebnis(strasse="Luruper Chaussee 125 / Haus 8 Süd", plz="22761", ort="Hamburg")
    k = zusammenfuehren(K, [WEB], {}, regel, llm)
    assert (k.strasse, k.adresszusatz, k.plz) == ("Luruper Chaussee 125", "Haus 8 Süd", "22761")
