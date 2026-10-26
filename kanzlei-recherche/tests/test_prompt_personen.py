"""Prompt-Aufbau, Personen-/Mitarbeiterzahl-Erkennung, Berufsträger- und Ansprechpartner-Regeln."""

import pytest

from recherche.config import LlmConfig
from recherche.crawl import finde_unterseiten, html_zu_text
from recherche.extract.base import LlmErgebnis, LlmPerson
from recherche.extract.merge import (
    ansprechpartner,
    ist_berufstraeger,
    personen_vereinigen,
    teamgroesse,
    vertretungsberechtigte,
)
from recherche.extract.personen import erkenne_personen, mitarbeiterzahl, name_zerlegen
from recherche.extract.prompt import baue_prompt, schluesselstellen, text_budget
from recherche.models import Seite, Seitentyp, TeamKlasse
from recherche.normalize import normalize_rechtsform, rechtsform_aus_text


def _seite(typ, url, html):
    return Seite(kandidat_id=1, domain="x.example", url=url, typ=typ, html=html, text=html_zu_text(html))


NAV = "<nav><a href='/'>Start</a><a href='/team'>Team</a><a href='/impressum'>Impressum</a></nav>"
FUSS = "<footer><p>Kanzlei X · Hauptstraße 1 · 24103 Kiel</p></footer>"


def test_text_budget_auto_und_fest():
    assert text_budget(LlmConfig(num_ctx=16384)) == 41052
    assert text_budget(LlmConfig(max_text_chars=5000)) == 5000


def test_rahmen_nur_einmal_und_bausteine_raus():
    impressum = (NAV + "<p>Vertretungsberechtigte Partner: Anna Kruse und Lars Behrens</p>"
                 "<h2>Haftung für Inhalte</h2>" + "<p>Langer Haftungstext.</p>" * 50 + FUSS)
    seiten = {
        Seitentyp.START: _seite(Seitentyp.START, "https://x.example/", NAV + "<p>Willkommen</p>" + FUSS),
        Seitentyp.IMPRESSUM: _seite(Seitentyp.IMPRESSUM, "https://x.example/impressum", impressum),
        Seitentyp.TEAM: _seite(Seitentyp.TEAM, "https://x.example/team", NAV + "<p>Unser Team</p>" + FUSS),
    }
    p = baue_prompt(seiten, 40000)
    assert p.text.count("Hauptstraße 1") == 1 and "### RAHMEN" in p.text
    assert "Langer Haftungstext" not in p.text
    assert p.text.index("Schlüsselstellen") < p.text.index("### TEAM")
    assert not p.gekuerzt and p.kuerzung_text() is None


def test_rahmen_braucht_drei_viertel_der_seiten():
    zeile = "<p>Anna Kruse, Steuerberaterin</p>"
    seiten = {t: _seite(t, f"https://x.example/{t.value}", NAV + (zeile if t in (Seitentyp.IMPRESSUM, Seitentyp.TEAM) else "") + "<p>" + t.value + "</p>")
              for t in (Seitentyp.START, Seitentyp.IMPRESSUM, Seitentyp.TEAM, Seitentyp.KONTAKT)}
    p = baue_prompt(seiten, 40000)
    assert "Anna Kruse" in p.text.split("### RAHMEN")[0]  # nur auf 2 von 4 Seiten: kein Rahmen
    assert "Impressum" in p.text.split("### RAHMEN")[1]


def test_kuerzung_wird_protokolliert():
    seiten = {Seitentyp.START: _seite(Seitentyp.START, "https://x.example/", "<p>" + "Text " * 2000 + "</p>")}
    p = baue_prompt(seiten, 1000)
    assert p.gekuerzt and p.kuerzung_text().startswith("start ")
    assert len(p.text) <= 1100


def test_schluesselstellen_fenster():
    zeilen = [f"Zeile {i}" for i in range(30)]
    zeilen[15] = "Vertreten durch: Jens Hansen"
    assert schluesselstellen(zeilen, 2) == zeilen[13:18]


@pytest.mark.parametrize("text,erwartet", [
    ("Dr. Anna Kruse", ("Anna Kruse", "Dr.")),
    ("Ole Brandt, M.Sc.", ("Ole Brandt", "M.Sc.")),
    ("Berufsrechtliche Angaben", None),
    ("Steuerberater", None),
])
def test_name_zerlegen(text, erwartet):
    assert name_zerlegen(text) == erwartet


def test_erkenne_personen_layouts():
    text = "Anna Kruse\nSteuerberaterin, Partnerin\nLars Behrens – Steuerberater\nSteuerfachangestellte\nMia Lorenz"
    gefunden = {p.name: p.funktion for p in erkenne_personen(text)}
    assert gefunden["Anna Kruse"].startswith("Steuerberaterin")
    assert gefunden["Lars Behrens"] == "Steuerberater"
    assert gefunden["Mia Lorenz"] == "Steuerfachangestellte"


@pytest.mark.parametrize("text,zahl,klasse", [
    ("Unsere 28 Expert:innen beraten Sie", 28, None),
    ("ein Team von 28 engagierten Menschen", 28, None),
    ("unser 15-köpfiges Team", 15, None),
    ("12 MitarbeiterInnen", 12, None),
    ("über 20 Mitarbeitende", None, TeamKlasse.aus_zahl(21)),
    ("rund 8 Kolleg*innen", None, TeamKlasse.aus_zahl(8)),
    ("seit 1987 in Kiel", None, None),
])
def test_mitarbeiterzahl(text, zahl, klasse):
    m = mitarbeiterzahl(["Startseite", text])
    if zahl is None and klasse is None:
        assert m is None
    else:
        assert (m.zahl, m.klasse) == (zahl, klasse)


def test_fachangestellte_ist_kein_berufstraeger():
    assert not ist_berufstraeger(LlmPerson(name="A B", funktion="Rechtsanwaltsfachangestellte"))
    assert not ist_berufstraeger(LlmPerson(name="A B", funktion="Steuerfachwirtin"))
    assert not ist_berufstraeger(LlmPerson(name="A B", funktion="Steuerberater-Anwärter"))
    assert ist_berufstraeger(LlmPerson(name="A B", funktion="Rechtsanwältin"))
    assert ist_berufstraeger(LlmPerson(name="A B", titel="StB", funktion="Partner"))


def test_personen_vereinigen_behaelt_berufsbezeichnung():
    """Hansen-Fall: erst „Partner" aus dem Impressum, dann „Steuerberater, Partner" vom Team."""
    p = personen_vereinigen([LlmPerson(name="Jens Hansen", funktion="Partner"),
                             LlmPerson(name="Dipl.-Kfm. Jens Hansen", funktion="Steuerberater, Partner")])
    assert len(p) == 1 and ist_berufstraeger(p[0])


IMPRESSUM = """Angaben gemäß § 5 TMG
Nordlicht Steuerberatung PartG mbB
Vertretungsberechtigte Partner: Dr. Anna Kruse und Lars Behrens
Berufsbezeichnung: Steuerberater (verliehen in Deutschland)
Zuständige Kammer: Steuerberaterkammer Schleswig-Holstein"""


def test_vertretungsberechtigte_und_berufsbezeichnung():
    assert vertretungsberechtigte(IMPRESSUM) == (["Anna Kruse", "Lars Behrens"], "Steuerberater")
    zwei = IMPRESSUM.replace("Steuerberater (", "Steuerberater, Wirtschaftsprüfer (")
    assert vertretungsberechtigte(zwei)[1] is None  # nicht eindeutig


def test_ansprechpartner_reihenfolge_und_berufstraeger():
    llm = LlmErgebnis(verantwortliche_person="Lars Behrens",
                      personen=[LlmPerson(name="Lars Behrens", funktion="Partner"), LlmPerson(name="Anna Kruse", funktion="Partnerin")])
    erkannt = erkenne_personen("Mia Lorenz\nSteuerberaterin\nTom Sand\nRechtsanwaltsfachangestellter")
    ap = ansprechpartner(llm, erkannt, IMPRESSUM)
    assert [a.name for a in ap] == ["Anna Kruse", "Lars Behrens", "Mia Lorenz"]
    assert sum(ist_berufstraeger(a) for a in ap) == 3


def test_teamgroesse_regelzahl_und_keine_zwei_personen():
    """Förde-Fall: zwei Partner abgebildet, keine Zahl im Text → nicht „2", wenn der Text mehr sagt."""
    llm = LlmErgebnis(personen=[LlmPerson(name="A Beta"), LlmPerson(name="C Delta")])
    assert teamgroesse(llm, None, angabe=mitarbeiterzahl(["28 Expert:innen"]))[:2] == (28, None)
    zahl, klasse, _ = teamgroesse(llm, None, angabe=mitarbeiterzahl(["über 20 Mitarbeitende"]))
    assert zahl is None and klasse == TeamKlasse.aus_zahl(21)


def test_rechtsform_kanonisch():
    assert normalize_rechtsform("Partnerschaftsgesellschaft mbB") == "PartG mbB"
    assert normalize_rechtsform("Gesellschaft mit beschränkter Haftung") == "GmbH"
    assert rechtsform_aus_text("Hansen & Petersen Steuerberater PartGmbB") == "PartG mbB"
    assert rechtsform_aus_text("Steuerbüro Schütt") is None


def test_ort_kanonisch():
    from fakes import fake_pruefer

    pr = fake_pruefer({"22559": "Hamburg", "24558": "Henstedt-Ulzburg"})
    assert pr.pruefe("22559", "Hamburg-Rissen").ort_kanonisch == "Hamburg"
    h = pr.pruefe("24558", "Henstedt-Ulzburg")
    assert h.verifiziert and h.ort_kanonisch is None


def test_linkerkennung_impressum_fremder_pfad_und_team2():
    html = ("<a href='/information/license'>Impressum</a><a href='/berufstraeger'>Berufsträger</a>"
            "<a href='/ueber-uns'>Über uns</a><a href='/kooperationspartner'>Kooperationspartner</a>")
    u = finde_unterseiten(html, "https://m.example/", "m.example")
    assert u[Seitentyp.IMPRESSUM] == "https://m.example/information/license"
    assert u[Seitentyp.TEAM] == "https://m.example/berufstraeger"
    assert u[Seitentyp.TEAM2] == "https://m.example/ueber-uns"


def test_cookie_banner_entfernt():
    html = "<div id='cookie-consent'><p>Wir verwenden Cookies. Alle akzeptieren</p></div><p>Kanzlei Kruse</p>"
    assert html_zu_text(html).strip() == "Kanzlei Kruse"
