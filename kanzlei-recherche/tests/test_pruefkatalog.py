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


# ---------------------------------------------------------------------- 1B und Qualitätsbericht (Abschnitt 4)


def _k(**kw):
    from recherche.models import Kanzlei

    return Kanzlei(**{"lauf_bundesland": "SH", "domain": "lund.example", "kanzleiname": "Lund Beratung", "plz": "24103",
                      "ort": "Kiel", "bundesland": SH, "plz_verifiziert": True, **kw})


def _impressum(*zeilen):
    return {Seitentyp.IMPRESSUM: Seite(url="https://lund.example/impressum", domain="lund.example",
                                       seitentyp=Seitentyp.IMPRESSUM, text="\n".join(zeilen), html=_html(zeilen))}


@pytest.mark.parametrize(("kw", "zeilen", "funde", "rang", "konfidenz"), [
    ({}, ["Lund Beratung", "Steuerberaterkammer Schleswig-Holstein"], [], 1, "hoch"),
    ({"ansprechpartner": [{"name": "Anke Lund", "funktion": "Steuerberaterin"}]}, ["Lund Beratung"], [], 2, "hoch"),
    ({}, ["Lund Beratung", "Berufsbezeichnung: Steuerberater (verliehen in Deutschland)"], [], 2, "hoch"),
    ({"kanzleiname": "Lund Steuerberatungsgesellschaft mbH"}, ["Lund"], [], 3, "hoch"),
    ({"kanzleiname": "Steuerkanzlei Lund"}, ["Steuerkanzlei Lund"], [], 4, "mittel"),
    ({}, ["Lund Beratung"], [{"quelle": "brave", "snippet": "Ihr Steuerberater in Kiel"}], 5, "niedrig"),
    ({"plz_verifiziert": None}, ["Lund Beratung", "Steuerberaterkammer Schleswig-Holstein"], [], 1, "mittel"),
    ({"abweichung": "Telefon ungültig: 0431 1 (Website)"}, ["Steuerberaterkammer"], [], 1, "mittel"),  # Q4-Deckel
])  # fmt: skip
def test_konfidenz_nach_1b(kw, zeilen, funde, rang, konfidenz):
    from recherche.extract.kanzlei import signal_1b
    from recherche.models import Ansprechpartner, Fund

    kw = {**kw, "ansprechpartner": [Ansprechpartner(**p) for p in kw.get("ansprechpartner", [])]}
    f = [Fund(bundesland="SH", quell_id=str(i), lauf_id="l", **x) for i, x in enumerate(funde)]
    k = pruefen(_k(**kw), _impressum(*zeilen), funde=f, ziel_land=SH)
    assert signal_1b(k, "\n".join(zeilen), f)[0] == rang and k.konfidenz.value == konfidenz, k.abweichung
    assert ("1B Rang" in (k.abweichung or "")) is (rang >= 4)


def test_ohne_website_aus_dem_amtlichen_verzeichnis_ist_hoch():
    from recherche.extract.kanzlei import anwenden
    from recherche.models import Fund

    f = [Fund(bundesland="SH", quelle="stb_verzeichnis", quell_id="x", lauf_id="l", snippet="Amtliches …")]
    k = anwenden(_k(domain=None, website_vorhanden=False), None, False, SH, funde=f)
    assert k.konfidenz.value == "hoch" and k.status.value == "neu"


def test_zu_pruefen_q5_q6_q12_und_1b():
    from recherche.extract.ansprechpartner import setze_ortsnamen
    from recherche.models import Konfidenz, Status
    from recherche.qualitaet import zu_pruefen_grund

    setze_ortsnamen(["Kiel"])
    hoch = Konfidenz.HOCH
    assert zu_pruefen_grund(_k(konfidenz=hoch)) is None
    assert zu_pruefen_grund(_k(konfidenz=Konfidenz.NIEDRIG)) == "Q12 Konfidenz niedrig"
    assert zu_pruefen_grund(_k(konfidenz=Konfidenz.NIEDRIG, status=Status.GEPRUEFT)) is None  # gepflegt bleibt
    assert zu_pruefen_grund(_k(konfidenz=hoch, kanzleiname="Kontakt")).startswith("Q6 Navigationswort")
    assert zu_pruefen_grund(_k(konfidenz=hoch, kanzleiname="Kiel")) == "Q6 nur Ortsname „Kiel“"
    assert zu_pruefen_grund(_k(konfidenz=hoch, kanzleiname="Die Amtsdirektorin")).startswith("Q6 Rolle")
    rang4 = "1B Rang 4: nur Kanzleiwort im Namen (keine Kammer, keine Berufsbezeichnung)"
    assert zu_pruefen_grund(_k(konfidenz=Konfidenz.MITTEL, abweichung=rang4)).startswith("1B Rang 4")
    abgeleitet = f"{rang4}; Name abgeleitet aus Ansprechpartner Ina Lund – kein Kanzleiname gefunden"
    assert zu_pruefen_grund(_k(konfidenz=Konfidenz.MITTEL, abweichung=abgeleitet)).startswith("Q5 Name abgeleitet")


def test_qualitaetsbericht_alle_pruefungen():
    from recherche.models import Ansprechpartner, Dublette, Konfidenz, Status
    from recherche.qualitaet import bericht

    hoch = Konfidenz.HOCH
    stb = [Ansprechpartner(name="Maik Lund", funktion="Steuerberater")]
    zeilen = [
        _k(domain="at.example", status=Status.AUSSERHALB, abweichung="Außerhalb: Ausland (.at)"),
        _k(domain="lsthv.example", status=Status.KEINE_KANZLEI, abweichung="Keine Kanzlei: Lohnsteuerhilfeverein"),
        _k(domain="a.example", konfidenz=hoch, abweichung="Ansprechpartner ohne Vorname: Schröder; Telefon ungültig: 0431 1",
           email="datenschutz@a.example", strasse="Markt 1"),
        _k(domain="b.example", konfidenz=hoch, kanzleiname="Steuerbüro B", strasse="Markt 1", abweichung="PLZ/Ort: Kiel ≠ Kie"),
        _k(domain="c.example", konfidenz=Konfidenz.NIEDRIG),
        _k(domain="d.example", konfidenz=hoch, teamgroesse=250, abweichung="Teamgröße unplausibel (250 Personen, 2 Berufsträger)"),
        _k(domain="zentrale.example", kanzleiname="Nordbund StBG", konfidenz=hoch, telefon="+49 431 59360", ansprechpartner=stb),
        _k(domain="heide.zentrale.example", kanzleiname="Nordbund Heide", konfidenz=hoch, telefon="+49 431 59360",
           ansprechpartner=stb, strasse="Wall 2", plz="25746"),
    ]
    dub = [Dublette(behalten="x.example", dublette="y.example", bundesland="SH", kriterium="telefon+ansprechpartner")]
    erg = bericht(zeilen, dub, SH, coverage_gefunden=9)
    n = erg.anzahl()
    assert {nr: n.get(nr, 0) for nr in ("Q1", "Q2", "Q3", "Q4", "Q7", "Q8", "Q9", "Q10", "Q11", "Q12", "Q13", "Q14")} == {
        "Q1": 1, "Q2": 1, "Q3": 1, "Q4": 1, "Q7": 1, "Q8": 1, "Q9": 1, "Q10": 1, "Q11": 1, "Q12": 1, "Q13": 1, "Q14": 1}
    assert list(erg.zu_pruefen.values()) == ["Q12 Konfidenz niedrig"]
    q14 = next(b.text for b in erg.befunde if b.nr == "Q14")
    assert q14 == "coverage 9, Hauptblatt 5 (Differenz 4)"


def test_qualitaet_befehl(tmp_path, db_pfad):
    from typer.testing import CliRunner

    from recherche import cli
    from recherche.models import Konfidenz
    from recherche.storage import Storage

    with Storage(db_pfad) as st:
        st.upsert_kanzlei(_k(konfidenz=Konfidenz.NIEDRIG))
        st.upsert_kanzlei(_k(domain="b.example", konfidenz=Konfidenz.HOCH, abweichung="Telefon ungültig: 1"))
    out = tmp_path / "q.csv"
    r = CliRunner().invoke(cli.app, ["qualitaet", "-b", "SH", "--db", str(db_pfad), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "Qualitätsbericht SH" in r.output and "Zu prüfen (aus dem Hauptblatt verschoben): 1" in r.output
    assert "Q12;Konfidenz niedrig;Sperre" in out.read_text(encoding="utf-8-sig")
