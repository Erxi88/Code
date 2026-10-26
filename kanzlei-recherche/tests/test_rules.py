import pytest

from recherche.extract.rules import (
    Ausschluss,
    adressen,
    beste_email,
    emails,
    entschleiern,
    extract_impressum,
    rechtsform_aus_name,
    telefone,
)  # fmt: skip


@pytest.fixture(scope="module")
def ausschluss(request):
    from recherche.config import load_settings

    s = load_settings()
    return Ausschluss.laden(s.pfad(s.pfade.impressum_ausschluss))


IMPRESSUM = """Impressum
Angaben gemäß § 5 DDG
Hansen & Petersen Steuerberater PartG mbB
Holstenstraße 42
24103 Kiel
Telefon: 0431 / 99 87 610
Telefax: 0431 / 99 87 611
E-Mail: info (at) hansen-steuerberatung.example
Zuständige Kammer:
Steuerberaterkammer Schleswig-Holstein
Hopfenstraße 2d
24114 Kiel
Tel. 0431 57049-0
Webdesign: Pixelwerk GmbH, Am Markt 3, 24534 Neumünster, Tel 04321 123456"""


def test_impressum_mit_kammer_und_webdesign(ausschluss):
    r = extract_impressum(IMPRESSUM, ausschluss, domain="hansen-steuerberatung.example")
    assert (r.strasse, r.plz, r.ort) == ("Holstenstraße 42", "24103", "Kiel")
    assert r.telefon == "+49 431 9987610"
    assert r.email == "info@hansen-steuerberatung.example"


def test_kammer_adresse_auch_ohne_abschnitt_ausgeschlossen(ausschluss):
    text = "Kanzlei\nHopfenstraße 2d\n24114 Kiel\nWall 5\n24103 Kiel"
    r = extract_impressum(text, ausschluss)
    assert (r.strasse, r.plz) == ("Wall 5", "24103")


def test_ohne_ausschluss_wuerde_erste_adresse_genommen():
    text = "Zuständige Kammer: Steuerberaterkammer\nHopfenstraße 2d\n24114 Kiel"
    assert extract_impressum(text, Ausschluss()).plz == "24114"


@pytest.mark.parametrize(("zeile", "erwartet"), [
    ("Großflecken 3 · 24534 Neumünster · Tel. 04321 / 55 90 12", ("Großflecken 3", "24534", "Neumünster")),
    ("Große Straße 15, 24937 Flensburg", ("Große Straße 15", "24937", "Flensburg")),
    ("Lange Str. 26-28 | D-24306 Plön", ("Lange Str. 26-28", "24306", "Plön")),
    ("Am Markt 1a, 25813 Husum Deutschland", ("Am Markt 1a", "25813", "Husum")),
    ("24960 Glücksburg (Ostsee)", (None, "24960", "Glücksburg (Ostsee)")),
    ("Postfach 1234, 24103 Kiel", (None, "24103", "Kiel")),
])  # fmt: skip
def test_adressen_einzeilig(zeile, erwartet):
    a = adressen([zeile])[0]
    assert (a.strasse, a.plz, a.ort) == erwartet


def test_adresse_mit_strasse_zwei_zeilen_darueber():
    a = adressen(["Hopfenweg 7", "Gebäude B", "24103 Kiel"])[0]
    assert a.strasse == "Hopfenweg 7"


def test_keine_falschen_plz():
    assert adressen(["USt-IdNr. DE123456789", "HRB 12345 KI", "Kontonummer 1234567890", "seit 19981 Jahren"]) == []


@pytest.mark.parametrize(("zeile", "erwartet"), [
    ("Telefon: 0431 / 99 87 610", "+49 431 9987610"),
    ("Tel. +49 (0) 461 4400871", "+49 461 4400871"),
    ("T +49 461 4400871 | F +49 461 4400872", "+49 461 4400871"),
    ("Fon (04522) 501933", "+49 4522 501933"),
    ("Phone: 0049 40 52990331", "+49 40 52990331"),
])  # fmt: skip
def test_telefon(zeile, erwartet):
    assert telefone([zeile])[0][0] == erwartet


def test_kein_fax_und_keine_jahreszahl():
    assert telefone(["Fax: 0431 / 99 87 611", "Telefax 0431 1234567", "gegründet 1998", "Gesamt 12345 Euro"]) == []


def test_telefon_aus_tel_link(ausschluss):
    r = extract_impressum("Impressum\nKanzlei Ohnetel", ausschluss, links=["tel:+494311234567"])
    assert r.telefon == "+494311234567"


def test_emails_entschleiert_und_aus_mailto():
    assert entschleiern("info [at] kanzlei (dot) example") == "info@kanzlei.example"
    assert emails("Mail: kanzlei{at}a.example", ["mailto:Info@A.example?subject=Hallo"]) == [
        "info@a.example", "kanzlei@a.example"]


def test_beste_email():
    mails = ["datenschutz@a.example", "kontakt@webagentur.example", "info@a.example"]
    assert beste_email(mails, "a.example", ("datenschutz",)) == "info@a.example"
    assert beste_email(["datenschutz@a.example"], "a.example", ("datenschutz",)) == "datenschutz@a.example"
    assert beste_email([], "a.example", ()) is None


def test_fehlendes_aus_weiteren_seiten(ausschluss):
    r = extract_impressum("Impressum\nFörde GmbH\nGroße Straße 15\n24937 Flensburg", ausschluss,
                          weitere_texte=[("Kontakt\nFormular", []), ("T +49 461 4400871", [])])
    assert (r.plz, r.telefon) == ("24937", "+49 461 4400871")


def test_nichts_gefunden_bleibt_null(ausschluss):
    r = extract_impressum("Impressum\nHier entsteht eine neue Website.", ausschluss)
    assert all(v is None for v in r.model_dump().values())


@pytest.mark.parametrize(("name", "form"), [
    ("Hansen & Petersen Steuerberater PartG mbB", "PartG mbB"),
    ("Förde Steuerberatungsgesellschaft mbH", "GmbH"),
    ("NordTax GmbH & Co. KG", "GmbH & Co. KG"),
    ("Möller & Kollegen GbR", "GbR"),
    ("Steuerbüro Schütt", None),
    (None, None),
])  # fmt: skip
def test_rechtsform_aus_name(name, form):
    assert rechtsform_aus_name(name) == form


@pytest.mark.parametrize(("text", "erwartet"), [
    ("Luruper Chaussee 125 / Haus 8 Süd\n22761 Hamburg", ("Luruper Chaussee 125", "Haus 8 Süd")),
    ("Luruper Chaussee 125\nHaus 8 Süd\n22761 Hamburg", ("Luruper Chaussee 125", "Haus 8 Süd")),
    ("Holstenstraße 42, 2. OG, 24103 Kiel", ("Holstenstraße 42", "2. OG")),
    ("Holstenstraße 42 - Hinterhaus\n24103 Kiel", ("Holstenstraße 42", "Hinterhaus")),
    ("c/o Bürogemeinschaft Nord\nWall 5\n24103 Kiel", ("Wall 5", "c/o Bürogemeinschaft Nord")),
    ("Wall 5\nGebäude B\nEingang Süd\n24103 Kiel", ("Wall 5", "Gebäude B, Eingang Süd")),
    ("Holstenstraße 42\n24103 Kiel", ("Holstenstraße 42", None)),
    ("Lange Str. 26-28, 24306 Plön", ("Lange Str. 26-28", None)),
    # Zeilenumbruch nach dem Schrägstrich (<br>), Klammer, mehrteiliger Zusatz
    ("OMM GmbH\nLuruper Chaussee 125 /\nHaus 8 Süd\n22761 Hamburg", ("Luruper Chaussee 125", "Haus 8 Süd")),
    ("Luruper Chaussee 125 (Haus 8 Süd)\n22761 Hamburg", ("Luruper Chaussee 125", "Haus 8 Süd")),
    ("Luruper Chaussee 125 · Haus 8 · Süd\n22761 Hamburg", ("Luruper Chaussee 125", "Haus 8, Süd")),
    ("Haus 8\nWall 5\n24103 Kiel", ("Wall 5", None)),
])  # fmt: skip
def test_adresszusatz(text, erwartet):
    r = extract_impressum("Kanzlei\n" + text, Ausschluss())
    assert (r.strasse, r.adresszusatz) == erwartet
