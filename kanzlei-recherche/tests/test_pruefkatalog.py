"""Prüfkatalog (docs/pruefkatalog.md), Abschnitt 5.1: Negativ-Fixtures für 1A und die Feldregeln aus 2.

Jeder Fall in tests/fixtures/negativ/*.yaml ist fiktiv: Impressum-Zeilen, die LLM-Antwort (von Hand) und das
erwartete Ergebnis. Durchlaufen wird die echte Kette ohne Ollama und ohne Netz: Regeln → Zusammenführung →
Kanzlei-Prüfung mit Bundesland-Filter.
"""

from pathlib import Path

import pytest
import yaml

from recherche.extract.base import LlmErgebnis, LlmPerson
from recherche.extract.kanzlei import grund_aus_abweichung, pruefen
from recherche.extract.lauf import regeln_fuer
from recherche.extract.merge import setze_ortsliste, zusammenfuehren
from recherche.models import Kandidat, Seite, Seitentyp
from recherche.normalize import PlzPruefung
from recherche.search.pipeline import Ort

FAELLE = sorted((Path(__file__).parent / "fixtures" / "negativ").glob("*.yaml"))
SH = "Schleswig-Holstein"
PLZ = {"24103": "Kiel", "24114": "Kiel", "25813": "Husum", "24768": "Rendsburg", "21483": "Lütau"}
BLOCKLIST = {"steuer-portal-blockiert.example"}


def _pruefe(plz, ort):
    if plz in PLZ:
        return PlzPruefung(True, SH, orte=[PLZ[plz]], ort_kanonisch=PLZ[plz])
    return PlzPruefung(False, None, hinweis=f"PLZ {plz} unbekannt")


@pytest.fixture
def ausschluss(settings):
    from recherche.extract.rules import Ausschluss

    return Ausschluss.laden(settings.pfad(settings.pfade.impressum_ausschluss))


@pytest.fixture(autouse=True)
def ortsliste():
    setze_ortsliste([Ort("Rendsburg", "24768"), Ort("Kiel", "24103", plz_weitere=("24105", "24114"))])
    yield
    setze_ortsliste([])


def _html(zeilen):
    return "<html><body>" + "".join(f"<p>{z}</p>" for z in zeilen) + "</body></html>"


def _lauf(fall: dict, ausschluss):
    domain = fall["domain"]
    seiten = {Seitentyp.IMPRESSUM: Seite(url=f"https://{domain}/impressum", domain=domain, seitentyp=Seitentyp.IMPRESSUM,
                                         http_status=200, text="\n".join(fall["impressum"]), html=_html(fall["impressum"]))}
    llm = LlmErgebnis(**{**fall.get("llm", {}),
                         "personen": [LlmPerson(**p) for p in fall.get("llm", {}).get("personen", [])]})
    kandidat = Kandidat(id=1, bundesland="SH", domain=domain, website=f"https://{domain}/")
    k = zusammenfuehren(kandidat, [], seiten, regeln_fuer(seiten, ausschluss, domain), llm, _pruefe, ausschluss)
    return pruefen(k, seiten, funde=[], ziel_land=SH, blocklist=BLOCKLIST)


def test_mindestumfang_der_negativ_fixtures():
    """Abschnitt 5.1: mindestens 15 Negativ-Fixtures, je Ausschlusskategorie aus 1A eine."""
    kategorien = {yaml.safe_load(p.read_text(encoding="utf-8"))["kategorie"].split(" (")[0] for p in FAELLE}
    assert len(FAELLE) >= 15
    for k in ("1A Lohnsteuerhilfeverein", "1A Rechtsanwaltskanzlei", "1A Buchhaltungsbüro", "1A Institution",
              "1A Behörde", "1A Verein/Partei", "1A Portal", "1A Branchenfremd", "1A Ausland"):
        assert k in kategorien, k


@pytest.mark.parametrize("pfad", FAELLE, ids=[p.stem for p in FAELLE])
def test_negativ_fixture(pfad, ausschluss):
    fall = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    soll = fall["erwartet"]
    k = _lauf(fall, ausschluss)
    assert k.status.value == soll["status"], k.abweichung
    if "grund" in soll:
        grund = grund_aus_abweichung(k.abweichung) if soll["status"] == "keine Kanzlei" else k.abweichung
        assert grund and soll["grund"] in grund, k.abweichung
    if "ansprechpartner" in soll:
        assert [p.name for p in k.ansprechpartner] == soll["ansprechpartner"]
    if "telefon" in soll:
        assert k.telefon == soll["telefon"]
    if "plz" in soll:
        assert k.plz == soll["plz"]
    for teil in soll.get("abweichung", []):
        assert teil in (k.abweichung or ""), k.abweichung


# ---------------------------------------------------------------------- Ansprechpartner (Abschnitt 2)


@pytest.mark.parametrize(("name", "titel", "funktion", "erwartet"), [
    ("Porträtfoto von Jan Hansen", None, "Steuerberater", ("Jan Hansen", None, "Steuerberater")),
    ("Foto von Eva Kittel", None, None, ("Eva Kittel", None, None)),
    ("Bild: Eva Kittel", None, None, ("Eva Kittel", None, None)),
    ("Jan Hansen Steuerberater", None, None, ("Jan Hansen", None, "Steuerberater")),
    ("Jan Hansen, StB", None, "Partner", ("Jan Hansen", None, "Partner, StB")),
    ("Jan Hansen Dipl.-Kfm.", None, None, ("Jan Hansen", "Dipl.-Kfm.", None)),
])  # fmt: skip
def test_ansprechpartner_praefixe_und_suffixe(name, titel, funktion, erwartet):
    from recherche.extract.ansprechpartner import bereinigen
    from recherche.models import Ansprechpartner

    (p,), hinweise = bereinigen([Ansprechpartner(name=name, titel=titel, funktion=funktion)])
    assert (p.name, p.titel, p.funktion) == erwartet and not hinweise


@pytest.mark.parametrize(("name", "funktion", "bleibt"), [
    ("Oliver Hagen", "Steuerberater", True),  # Ortsname als Nachname bleibt (Rücksprache 30.10.2026)
    ("August Petersen", None, True),  # Monatsname als Vorname bleibt
    ("Jan Lund Kiel", "Steuerberater", False),  # Ortsname als Zusatzwort
    ("Fachanwalt für Familienrecht Elmshorn", None, False),
    ("Ina Lund", "Amtsdirektorin", False),
    ("Archiv", None, False),
    ("Dezember Montag", None, False),
    ("Schröder", "Steuerberaterin", False),  # nur Nachname
    ("Dr. Schröder", None, False),  # erstes Wort ein Titel
    ("W. Drews", "Steuerberaterin", True),  # Initiale ist ein Vorname
])  # fmt: skip
def test_ansprechpartner_verwerfen(name, funktion, bleibt):
    from recherche.extract.ansprechpartner import bereinigen
    from recherche.models import Ansprechpartner

    liste, hinweise = bereinigen([Ansprechpartner(name=name, funktion=funktion)])
    assert bool(liste) is bleibt and bool(hinweise) is not bleibt


def test_rechtsanwaelte_nur_ohne_steuerberater_vorn():
    from recherche.extract.ansprechpartner import bereinigen
    from recherche.models import Ansprechpartner

    liste, _ = bereinigen([Ansprechpartner(name="Ole Prasse", funktion="Rechtsanwalt, Notar"),
                           Ansprechpartner(name="Ina Lund", funktion="Steuerberaterin")])
    assert [p.name for p in liste] == ["Ina Lund", "Ole Prasse"]
    liste, _ = bereinigen([Ansprechpartner(name="Ole Prasse", funktion="Rechtsanwalt")])
    assert [p.name for p in liste] == ["Ole Prasse"]  # ohne Steuerberater bleibt er vorn


# ---------------------------------------------------------------------- Telefon (Abschnitt 2)


def test_telefon_rangfolge_tel_link_schema_impressum_kontakt():
    from recherche.extract.rules import extract_impressum

    impressum = "Steuerkanzlei Lund\nMarkt 1\n24103 Kiel\nTel. 0431 1111111"
    kontakt = ("Kontakt\nTelefon 0431 2222222", [])
    # tel:-Link vor Schema.org vor Impressum-Text vor Kontaktseite
    assert extract_impressum(impressum, links=["tel:+494313333333"], weitere_texte=[kontakt],
                             schema_telefone=["+49 431 4444444"]).telefon == "+49 431 3333333"
    assert extract_impressum(impressum, weitere_texte=[kontakt], schema_telefone=["+49 431 4444444"]).telefon == "+49 431 4444444"
    assert extract_impressum(impressum, weitere_texte=[kontakt]).telefon == "+49 431 1111111"
    assert extract_impressum("Markt 1\n24103 Kiel", weitere_texte=[kontakt]).telefon == "+49 431 2222222"


def test_telefon_festnetz_vor_mobil_vor_service_und_kein_fax():
    from recherche.extract.rules import extract_impressum

    text = "Steuerkanzlei Lund\nMarkt 1\n24103 Kiel\nHotline 0800 1234567\nMobil: 0171 1234567\nF: 0431 7654321"
    erg = extract_impressum(text, links=["tel:08001234567", "tel:01711234567"])
    assert erg.telefon == "+49 171 1234567"  # kein Festnetz: Mobil vor 0800; „F:" ist Fax
    assert extract_impressum(text + "\nTel. 0431 5550000", links=["tel:01711234567"]).telefon == "+49 431 5550000"


def test_tel_link_aus_dem_kammer_abschnitt_zaehlt_nicht(ausschluss):
    from recherche.extract.rules import extract_impressum

    text = ("Steuerkanzlei Lund\nMarkt 1\n24103 Kiel\nTel. 0431 5550000\n"
            "Zuständige Kammer: Steuerberaterkammer Schleswig-Holstein\nHopfenstraße 2 d\n24114 Kiel\nTel. 0431 5703490")
    assert extract_impressum(text, ausschluss, links=["tel:+494315703490"]).telefon == "+49 431 5550000"


@pytest.mark.parametrize(("roh", "erwartet"), [
    ("0 41 53 / 5 97 40", "+49 4153 59740"),
    ("+49 (0) 41 53 59740", "+49 4153 59740"),
    ("0431 1", None),  # zu kurz
    ("0431 12", None),  # laut phonenumbers ungültig
])  # fmt: skip
def test_telefon_nur_gueltige_nummern(roh, erwartet):
    from recherche.normalize import normalize_telefon

    assert normalize_telefon(roh) == erwartet
