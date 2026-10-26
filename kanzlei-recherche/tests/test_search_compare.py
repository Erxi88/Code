import csv

from fakes import FakeAdapter

from recherche.search.compare import compare_adapters
from recherche.search.pipeline import Ort

ORTE = [Ort("Kiel", "24103"), Ort("Husum", "25813")]
VORLAGEN = ["Steuerberater {ort}", "Steuerkanzlei {ort}"]


def _adapters():
    a = FakeAdapter("ddgs", {
        "Steuerberater Kiel": ["https://a.example/", "https://b.example/", "https://gelbeseiten.de/kiel"],
        "Steuerkanzlei Kiel": ["https://www.a.example/team"],
        "Steuerberater Husum": ["https://h1.example/"],
    })  # fmt: skip
    b = FakeAdapter("google", {
        "Steuerberater Kiel": ["https://b.example/", "https://c.example/"],
        "Steuerberater Husum": ["https://h1.example/", "https://h2.example/"],
    })  # fmt: skip
    c = FakeAdapter("brave", {}, grund="BRAVE_API_KEY fehlt in .env")
    return a, b, c


def test_vergleich():
    a, b, c = _adapters()
    erg = compare_adapters([a, b, c], ORTE, VORLAGEN, {"gelbeseiten.de"}, 20)
    assert erg.uebersprungen == {"brave": "BRAVE_API_KEY fehlt in .env"}
    assert c.aufrufe == []
    assert list(erg.ergebnisse) == ["ddgs", "google"]
    assert erg.domains("ddgs", "Kiel") == {"a.example", "b.example"}
    assert erg.domains("google") == {"b.example", "c.example", "h1.example", "h2.example"}
    assert erg.ueberschneidung() == {"b.example", "h1.example"}
    assert erg.ueberschneidung("Husum") == {"h1.example"}
    assert erg.alle_domains() == {"a.example", "b.example", "c.example", "h1.example", "h2.example"}
    (paar,) = erg.paare()
    assert (paar.a, paar.b) == ("ddgs", "google")
    assert paar.beide == {"b.example", "h1.example"}
    assert paar.nur_a == {"a.example"}
    assert paar.nur_b == {"c.example", "h2.example"}
    assert erg.ergebnisse["ddgs"].queries == 4
    assert erg.ergebnisse["ddgs"].treffer == 5


def test_fehler_werden_gezaehlt():
    a = FakeAdapter("ddgs", {"Steuerberater Kiel": ["https://a.example/"]}, fehler_bei={"Steuerkanzlei Kiel"})
    erg = compare_adapters([a], ORTE[:1], VORLAGEN, set(), 20)
    assert erg.ergebnisse["ddgs"].fehler == 1
    assert erg.domains("ddgs") == {"a.example"}
    assert erg.paare() == []


def test_csv(tmp_path):
    a, b, _ = _adapters()
    erg = compare_adapters([a, b], ORTE, VORLAGEN, {"gelbeseiten.de"}, 20)
    pfad = erg.als_csv(tmp_path / "vergleich.csv")
    zeilen = list(csv.reader(pfad.open(encoding="utf-8"), delimiter=";"))
    assert zeilen[0] == ["ort", "domain", "ddgs", "google"]
    assert ["Kiel", "a.example", "x", ""] in zeilen
    assert ["Kiel", "b.example", "x", "x"] in zeilen
    assert ["Husum", "h2.example", "", "x"] in zeilen
    assert len(zeilen) == 1 + 3 + 2
