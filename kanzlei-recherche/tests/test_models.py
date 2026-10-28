from recherche.models import Ansprechpartner, Kanzlei, Konfidenz, Status, TeamKlasse


def test_testdaten_vollstaendig(kanzleien):
    assert len(kanzleien) == 20
    assert {k.konfidenz for k in kanzleien} == set(Konfidenz)
    assert len({k.domain for k in kanzleien}) == 20
    assert all(k.domain.endswith(".example") for k in kanzleien)


def test_testdaten_sonderfaelle(kanzleien):
    # Teamgröße nur als Klasse
    assert any(k.teamgroesse is None and k.teamgroesse_klasse for k in kanzleien)
    # ganz ohne Teamgröße
    assert any(k.teamgroesse is None and k.teamgroesse_klasse is None for k in kanzleien)
    # fehlende Felder bleiben None
    assert any(k.kanzleiname is None for k in kanzleien)
    assert any(k.email is None for k in kanzleien)
    assert any(k.bundesland is None for k in kanzleien)
    assert any(not k.ansprechpartner for k in kanzleien)
    assert any(len(k.ansprechpartner) >= 3 for k in kanzleien)


def test_teamklasse_aus_zahl():
    erwartet = {1: "1", 2: "2-5", 5: "2-5", 6: "6-10", 10: "6-10", 11: "11-20", 20: "11-20", 21: "21-50",
                50: "21-50", 51: "51-100", 100: "51-100", 101: ">100"}
    assert {n: TeamKlasse.aus_zahl(n) for n in erwartet} == erwartet


def test_kanzlei_klasse_wird_aus_zahl_abgeleitet():
    k = Kanzlei(domain="a.example", lauf_bundesland="SH", teamgroesse=7)
    assert k.teamgroesse_klasse == "6-10"
    assert k.teamgroesse_anzeige == 7


def test_kanzlei_anzeige_klasse_ohne_zahl():
    k = Kanzlei(domain="a.example", lauf_bundesland="SH", teamgroesse_klasse=">100")
    assert k.teamgroesse_anzeige == ">100"


def test_kanzlei_standardwerte():
    k = Kanzlei(domain="a.example", lauf_bundesland="SH")
    assert k.status == Status.NEU
    assert k.konfidenz == Konfidenz.NIEDRIG
    assert k.hauptansprechpartner is None
    assert k.teamgroesse_anzeige is None


def test_hauptansprechpartner_ist_erster():
    k = Kanzlei(
        domain="a.example",
        lauf_bundesland="SH",
        ansprechpartner=[Ansprechpartner(name="A"), Ansprechpartner(name="B")],
    )
    assert k.hauptansprechpartner.name == "A"


def test_ansprechpartner_anzeige():
    assert Ansprechpartner(name="Jens Hansen", titel="Dipl.-Kfm.", funktion="StB").anzeige() == (
        "Dipl.-Kfm. Jens Hansen (StB)"
    )
    assert Ansprechpartner(name="Jens Hansen").anzeige() == "Jens Hansen"


def test_status_werte():
    assert [s.value for s in Status] == ["neu", "geprüft", "kontaktiert", "kein Interesse", "ungültig", "keine Kanzlei"]
