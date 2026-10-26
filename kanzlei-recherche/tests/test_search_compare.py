import csv

from fakes import FakeOrtsAdapter, FakeQueryAdapter, hit

from recherche.search.base import KontingentErschoepft
from recherche.search.compare import compare_adapters
from recherche.search.pipeline import Ort

ORTE = [Ort("Kiel", "24103"), Ort("Husum", "25813")]
VORLAGEN = ["Steuerberater {ort}", "Steuerkanzlei {ort}"]


def _adapters():
    brave = FakeQueryAdapter("brave", {
        "Steuerberater Kiel": ["https://a.example/", "https://b.example/", "https://gelbeseiten.de/kiel"],
        "Steuerkanzlei Kiel": ["https://www.a.example/team"],
        "Steuerberater Husum": ["https://h1.example/"],
    })  # fmt: skip
    ddgs = FakeQueryAdapter("ddgs", {
        "Steuerberater Kiel": ["https://b.example/", "https://c.example/"],
        "Steuerberater Husum": ["https://h1.example/", "https://h2.example/"],
    })  # fmt: skip
    places = FakeOrtsAdapter("places", {
        "Kiel": [
            hit("places", "https://a.example/", quell_id="p1", name="A", telefon="0431 1111"),
            # ohne Website, gleiche Telefonnummer → dieselbe Kanzlei wie a.example
            hit("places", None, quell_id="p2", name="A Filiale", telefon="+49 431 1111"),
            hit("places", None, quell_id="p3", name="Kanzlei Ohneweb", strasse="Wall 5", plz="24103"),
        ],
    })  # fmt: skip
    google = FakeQueryAdapter("google_cse", {}, grund="GOOGLE_CSE_API_KEY fehlt in .env")
    return brave, ddgs, places, google


def test_vergleich_aller_adapter():
    brave, ddgs, places, google = _adapters()
    erg = compare_adapters([brave, ddgs, places, google], ORTE, VORLAGEN, {"gelbeseiten.de"}, 20)
    assert erg.uebersprungen == {"google_cse": "GOOGLE_CSE_API_KEY fehlt in .env"}
    assert google.aufrufe == []
    assert list(erg.ergebnisse) == ["brave", "ddgs", "places"]
    n = lambda ids: set(erg.namen(ids))  # noqa: E731
    assert n(erg.kanzleien("brave", "Kiel")) == {"a.example", "b.example"}
    assert n(erg.kanzleien("places")) == {"a.example", "Kanzlei Ohneweb, Wall 5, 24103"}
    assert n(erg.alle()) == {"a.example", "b.example", "c.example", "h1.example", "h2.example",
                             "Kanzlei Ohneweb, Wall 5, 24103"}
    assert n(erg.ueberschneidung("Kiel")) == set()
    assert n(erg.exklusiv("brave")) == set()
    assert n(erg.exklusiv("ddgs")) == {"c.example", "h2.example"}
    assert n(erg.exklusiv("places")) == {"Kanzlei Ohneweb, Wall 5, 24103"}
    paare = {(p.a, p.b): p for p in erg.paare()}
    assert set(paare) == {("brave", "ddgs"), ("brave", "places"), ("ddgs", "places")}
    assert n(paare[("brave", "places")].beide) == {"a.example"}
    assert n(paare[("brave", "ddgs")].nur_a) == {"a.example"}
    assert erg.ergebnisse["brave"].queries == 4
    assert erg.ergebnisse["places"].queries == 2
    assert erg.ergebnisse["brave"].anfragen == 4


def test_limit_beendet_nur_diesen_adapter():
    brave = FakeQueryAdapter("brave", {"Steuerberater Kiel": ["https://a.example/"]},
                             fehler_bei={"Steuerkanzlei Kiel": KontingentErschoepft("leer")})
    ddgs = FakeQueryAdapter("ddgs", {"Steuerberater Husum": ["https://h.example/"]})
    erg = compare_adapters([brave, ddgs], ORTE, VORLAGEN, set(), 20)
    assert erg.ergebnisse["brave"].abbruch == "leer"
    assert "Steuerberater Husum" not in brave.aufrufe
    assert len(ddgs.aufrufe) == 4


def test_csv(tmp_path):
    brave, ddgs, places, _ = _adapters()
    erg = compare_adapters([brave, ddgs, places], ORTE, VORLAGEN, {"gelbeseiten.de"}, 20)
    pfad = erg.als_csv(tmp_path / "vergleich.csv")
    zeilen = list(csv.reader(pfad.open(encoding="utf-8"), delimiter=";"))
    assert zeilen[0] == ["ort", "kanzlei", "domain", "brave", "ddgs", "places"]
    assert ["Kiel", "a.example", "a.example", "x", "", "x"] in zeilen
    assert ["Kiel", "Kanzlei Ohneweb, Wall 5, 24103", "", "", "", "x"] in zeilen
    assert ["Husum", "h2.example", "h2.example", "", "x", ""] in zeilen


def test_vergleich_mit_suchplan_und_zusatzorten():
    from recherche.config import EinwohnerStufe
    from recherche.search.pipeline import Suchplan

    plan = Suchplan(VORLAGEN, ["Steuerberater {plz} {gemeinde}"], [EinwohnerStufe(unter=2000, varianten=1, seiten=1)])
    orte = [Ort("Dorf", "24000", einwohner=500), Ort("Kiel (24105)", "24105", gemeinde="Kiel")]
    brave = FakeQueryAdapter("brave", {"Steuerberater Dorf": ["https://d.example/"],
                                       "Steuerberater 24105 Kiel": ["https://k.example/"]})
    places = FakeOrtsAdapter("places", {})
    erg = compare_adapters([brave, places], orte, plan, set(), 20)
    assert brave.aufrufe == ["Steuerberater Dorf", "Steuerberater 24105 Kiel"]
    assert places.aufrufe == ["Dorf"]  # Lückenfüller nur je Gemeinde
    assert set(erg.namen(erg.alle())) == {"d.example", "k.example"}
