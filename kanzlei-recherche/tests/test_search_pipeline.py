import pytest
from fakes import FakeAdapter

from recherche.models import SchrittStatus
from recherche.search.base import SearchHit
from recherche.search.pipeline import (
    Ort,
    filter_hits,
    ist_blockiert,
    load_blocklist,
    load_orte,
    load_vorlagen,
    normalize_domain,
    orte_filtern,
    queries_fuer_ort,
    run_search,
)  # fmt: skip

KIEL = Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")


@pytest.mark.parametrize(
    ("url", "erwartet"),
    [
        ("https://www.Hansen-Steuerberatung.example/team?x=1", "hansen-steuerberatung.example"),
        ("http://hansen-steuerberatung.example:8080/", "hansen-steuerberatung.example"),
        ("hansen-steuerberatung.example", "hansen-steuerberatung.example"),
        ("https://kanzlei.hansen.example./", "kanzlei.hansen.example"),
        ("https://kanzlei-mölln.example/", "xn--kanzlei-mlln-djb.example"),
        ("mailto:info@a.example", None),
        ("ftp://a.example/datei", None),
        ("https://localhost/", None),
        ("", None),
    ],
)
def test_normalize_domain(url, erwartet):
    assert normalize_domain(url) == erwartet


def test_blocklist_datei(settings):
    bl = load_blocklist(settings.pfad(settings.pfade.blocklist))
    assert {"gelbeseiten.de", "facebook.com", "dasoertliche.de", "linkedin.com"} <= bl
    assert not any(e.startswith("#") or e.startswith("www.") for e in bl)


def test_blocklist_mit_subdomains():
    bl = {"facebook.com", "gelbeseiten.de"}
    assert ist_blockiert("facebook.com", bl)
    assert ist_blockiert("de-de.facebook.com", bl)
    assert not ist_blockiert("notfacebook.com", bl)
    assert not ist_blockiert("hansen.example", bl)


def test_filter_hits_blocklist_und_dubletten(ddgs_antworten):
    hits = [SearchHit(url=r["href"], titel=r["title"], quelle="ddgs") for r in ddgs_antworten["Steuerberater Kiel"]]
    bl = {"dasoertliche.de", "gelbeseiten.de", "facebook.com"}
    ergebnis = filter_hits(hits, bl)
    assert [d for d, _ in ergebnis] == [
        "hansen-steuerberatung.example",
        "foerde-steuer.example",
        "kanzlei-holtenau.example",
    ]
    # erster Treffer je Domain bleibt erhalten
    assert ergebnis[0][1].url == "https://www.hansen-steuerberatung.example/"


def test_filter_hits_ueber_mehrere_queries_und_adapter():
    bekannt: set[str] = set()
    a = filter_hits([SearchHit(url="https://x.example/", quelle="ddgs")], set(), bekannt)
    b = filter_hits(
        [SearchHit(url="https://www.x.example/kontakt", quelle="google"),
         SearchHit(url="https://y.example/", quelle="google")],
        set(), bekannt,
    )  # fmt: skip
    assert [d for d, _ in a] == ["x.example"]
    assert [d for d, _ in b] == ["y.example"]


def test_queries_fuer_ort():
    vorlagen = ["Steuerberater {ort}", "Steuerberatung {plz} {ort}", "Steuerberater {ort}", "Kanzlei {kreis}"]
    assert queries_fuer_ort(vorlagen, KIEL) == [
        "Steuerberater Kiel", "Steuerberatung 24103 Kiel", "Kanzlei Kreisfreie Stadt Kiel",
    ]  # fmt: skip
    # Ort ohne PLZ: Vorlagen mit {plz} entfallen
    assert queries_fuer_ort(vorlagen, Ort("Kiel", "")) == ["Steuerberater Kiel"]


def test_config_dateien(settings):
    orte = load_orte(settings.pfad(settings.bundesland("SH").orte_datei))
    assert 60 <= len(orte) <= 80
    assert len({o.name for o in orte}) == len(orte)  # eine Zeile je Ort
    assert all(o.plz.isdigit() and len(o.plz) == 5 and o.kreis for o in orte)
    assert {"Kiel", "Flensburg", "Lübeck", "Neumünster", "Husum"} <= {o.name for o in orte}
    assert len(load_vorlagen(settings.pfad(settings.pfade.queries))) >= 2


def test_orte_filtern():
    orte = [KIEL, Ort("Husum", "25813")]
    assert orte_filtern(orte, None) == orte
    assert orte_filtern(orte, ["husum", " Kiel "]) == [Ort("Husum", "25813"), KIEL]
    assert orte_filtern(orte, ["Sylt"]) == [Ort("Sylt", "")]


def _adapter(fehler_bei=None):
    return FakeAdapter(
        "fake",
        {
            "Steuerberater Kiel": ["https://www.a.example/", "https://gelbeseiten.de/x", "https://b.example/impressum"],
            "Steuerkanzlei Kiel": ["https://a.example/team", "https://c.example/"],
        },
        fehler_bei=fehler_bei,
    )


def test_run_search_speichert_kandidaten_und_protokoll(storage):
    stat = run_search(storage, _adapter(), [KIEL], ["Steuerberater {ort}", "Steuerkanzlei {ort}"],
                      {"gelbeseiten.de"}, "SH", "lauf1", 20)
    assert (stat.queries, stat.treffer, stat.neue_domains, stat.fehler) == (2, 5, 3, 0)
    kandidaten = storage.kandidaten("SH")
    assert [k.domain for k in kandidaten] == ["a.example", "b.example", "c.example"]
    assert all(k.crawl_status == SchrittStatus.OFFEN for k in kandidaten)
    a = storage.get_kandidat("a.example")
    assert (a.suchbegriff, a.ort, a.quelle) == ("Steuerberater Kiel", "Kiel", "fake")
    protokoll = storage.lauf_protokoll("SH")
    assert [(e.suchbegriff, e.trefferzahl, e.neue_domains) for e in protokoll] == [
        ("Steuerberater Kiel", 3, 2), ("Steuerkanzlei Kiel", 2, 1),
    ]  # fmt: skip


def test_run_search_wiederaufnahme(storage):
    vorlagen = ["Steuerberater {ort}", "Steuerkanzlei {ort}"]
    erster = _adapter(fehler_bei={"Steuerkanzlei Kiel"})
    stat1 = run_search(storage, erster, [KIEL], vorlagen, set(), "SH", "lauf1", 20)
    assert (stat1.queries, stat1.fehler) == (1, 1)
    assert storage.get_kandidat("c.example") is None

    zweiter = _adapter()
    stat2 = run_search(storage, zweiter, [KIEL], vorlagen, set(), "SH", "lauf2", 20)
    # erledigte Query wird übersprungen, fehlgeschlagene wiederholt
    assert zweiter.aufrufe == ["Steuerkanzlei Kiel"]
    assert (stat2.queries, stat2.uebersprungen, stat2.neue_domains) == (1, 1, 1)
    assert storage.get_kandidat("c.example") is not None

    dritter = _adapter()
    run_search(storage, dritter, [KIEL], vorlagen, set(), "SH", "lauf3", 20)
    assert dritter.aufrufe == []
