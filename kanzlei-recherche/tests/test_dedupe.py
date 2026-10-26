import pytest

from recherche.models import DublettenArt
from recherche.search.dedupe import (
    Index,
    Schluessel,
    adresse_key,
    name_tokens,
    namen_aehnlich,
    telefon_key,
    zusammenfuehren,
)  # fmt: skip


@pytest.mark.parametrize(
    ("strasse", "plz", "erwartet"),
    [
        ("Holstenstraße 42", "24103", "24103|holstenstr|42"),
        ("Holstenstr. 42", "24103", "24103|holstenstr|42"),
        ("holstenstrasse 42 a", "24103", "24103|holstenstr|42a"),
        ("Lange Str. 26-28", "24306", "24306|langestr|26"),
        ("Kieler Straße 50b", "24340", "24340|kielerstr|50b"),
        ("Großflecken 3", "24534", "24534|grossflecken|3"),
        ("Am Markt", "24103", None),  # ohne Hausnummer
        ("Holstenstraße 42", None, None),  # ohne PLZ
        (None, "24103", None),
    ],
)
def test_adresse_key(strasse, plz, erwartet):
    assert adresse_key(strasse, plz) == erwartet


def test_telefon_key_vereinheitlicht_schreibweisen():
    assert telefon_key("0431 / 99 87 610") == telefon_key("+49 431 9987610") == "494319987610"
    assert telefon_key("+49 (0)431-9987610") == "494319987610"
    assert telefon_key(None) is None
    assert telefon_key("kein Telefon") is None


def test_name_tokens_ohne_stopworte_und_ort():
    assert name_tokens("Hansen & Petersen Steuerberater PartG mbB") == {"hansen", "petersen"}
    assert name_tokens("Steuerkanzlei Jensen", ort="Lübeck") == {"jensen"}
    assert name_tokens("Steuerberatung Wedel", ort="Wedel") == frozenset()
    assert name_tokens("Förde Steuerberatungsgesellschaft mbH") == {"foerde"}
    assert name_tokens(None) == frozenset()


def test_namen_aehnlich():
    assert namen_aehnlich({"hansen", "petersen"}, {"hansen", "petersen", "kollegen"})
    assert namen_aehnlich({"jensen"}, {"jensen", "kirsten"})
    assert not namen_aehnlich({"hansen"}, {"petersen"})
    assert not namen_aehnlich(frozenset(), {"hansen"})


def _s(**kw) -> Schluessel:
    return Schluessel.aus_feldern(kw.get("domain"), kw.get("telefon"), kw.get("strasse"), kw.get("plz"),
                                  kw.get("name"), kw.get("ort"))


@pytest.fixture
def index():
    i = Index()
    i.setze(1, _s(domain="hansen.example", telefon="0431 9987610", strasse="Holstenstraße 42",
                  plz="24103", name="Hansen & Petersen Steuerberater", ort="Kiel"))
    i.setze(2, _s(strasse="Bahnhofstraße 30", plz="22880", name="Steuerberatung am Roland", ort="Wedel"))
    i.setze(3, _s(domain="jensen.example", plz="23552", name="Steuerkanzlei Jensen", ort="Lübeck"))
    return i


def test_abgleich_domain(index):
    a = index.finde(_s(domain="hansen.example"))
    assert (a.id, a.kriterium, a.art) == (1, "domain", DublettenArt.ZUSAMMENGEFUEHRT)


def test_abgleich_telefon_ohne_website(index):
    a = index.finde(_s(telefon="+49 431 9987610", name="Ganz anderer Eintrag"))
    assert (a.id, a.kriterium) == (1, "telefon")


def test_abgleich_adresse_mit_passendem_namen(index):
    a = index.finde(_s(strasse="Holstenstr. 42", plz="24103", name="Kanzlei Hansen"))
    assert (a.id, a.kriterium, a.art) == (1, "adresse", DublettenArt.ZUSAMMENGEFUEHRT)


def test_abgleich_adresse_ohne_namen(index):
    a = index.finde(_s(strasse="Bahnhofstr. 30", plz="22880"))
    assert (a.id, a.kriterium) == (2, "adresse")


def test_gleiche_adresse_anderer_name_nur_verdacht(index):
    a = index.finde(_s(strasse="Holstenstraße 42", plz="24103", name="Steuerbüro Möller"))
    assert a.id is None
    assert a.verdacht == [1]


def test_abgleich_plz_und_name_unsicher(index):
    a = index.finde(_s(plz="23552", name="Jensen Steuerberatung", strasse="Breite Straße 7"))
    assert (a.id, a.kriterium, a.art) == (3, "plz+name", DublettenArt.UNSICHER)


def test_kein_abgleich(index):
    assert index.finde(_s(plz="23552", name="Steuerkanzlei Möller")).id is None
    assert index.finde(_s(name="Hansen & Petersen")).id is None  # Name ohne PLZ reicht nicht


def test_setze_aktualisiert_schluessel(index):
    neu = zusammenfuehren(index.eintraege[2], _s(telefon="04103 912270", domain="wedel-steuer.example"))
    index.setze(2, neu)
    assert index.finde(_s(telefon="04103-912270")).id == 2
    assert index.finde(_s(domain="wedel-steuer.example")).id == 2
    assert index.finde(_s(strasse="Bahnhofstraße 30", plz="22880")).id == 2
