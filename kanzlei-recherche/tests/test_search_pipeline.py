import pytest
from fakes import FakeOrtsAdapter, FakeQueryAdapter, hit

from recherche.models import DublettenArt, SchrittStatus
from recherche.search.base import BudgetErschoepft, KontingentErschoepft, SearchError
from recherche.search.pipeline import (
    Ort,
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
VORLAGEN = ["Steuerberater {ort}", "Steuerkanzlei {ort}"]
BLOCK = {"gelbeseiten.de", "facebook.com"}


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
    assert ist_blockiert("de-de.facebook.com", BLOCK)
    assert not ist_blockiert("notfacebook.com", BLOCK)


def test_queries_fuer_ort():
    vorlagen = ["Steuerberater {ort}", "Steuerberatung {plz} {ort}", "Steuerberater {ort}", "Kanzlei {kreis}"]
    assert queries_fuer_ort(vorlagen, KIEL) == [
        "Steuerberater Kiel", "Steuerberatung 24103 Kiel", "Kanzlei Kreisfreie Stadt Kiel",
    ]  # fmt: skip
    assert queries_fuer_ort(vorlagen, Ort("Kiel", "")) == ["Steuerberater Kiel"]


def test_config_dateien(settings):
    orte = load_orte(settings.pfad(settings.bundesland("SH").orte_datei))
    assert 60 <= len(orte) <= 80
    assert len({o.name for o in orte}) == len(orte)
    assert all(o.plz.isdigit() and len(o.plz) == 5 and o.kreis for o in orte)
    assert all(o.einwohner is None for o in orte)  # Spalte vorgesehen, Werte folgen (orte/GV-ISys)
    assert {"Kiel", "Flensburg", "Lübeck", "Neumünster", "Husum"} <= {o.name for o in orte}
    assert len(load_vorlagen(settings.pfad(settings.pfade.queries))) >= 2


def test_orte_mit_einwohnern(tmp_path):
    p = tmp_path / "orte.csv"
    p.write_text("ort;plz;kreis;einwohner\nKiel;24103;Kreisfreie Stadt Kiel;247000\nHusum;25813;Kreis Nordfriesland;\n",
                 encoding="utf-8")
    assert load_orte(p) == [Ort("Kiel", "24103", "Kreisfreie Stadt Kiel", 247000), Ort("Husum", "25813", "Kreis Nordfriesland")]


def test_orte_filtern():
    orte = [KIEL, Ort("Husum", "25813")]
    assert orte_filtern(orte, None) == orte
    assert orte_filtern(orte, ["husum", " Kiel "]) == [Ort("Husum", "25813"), KIEL]
    assert orte_filtern(orte, ["Sylt"]) == [Ort("Sylt", "")]


def _web(fehler_bei=None):
    return FakeQueryAdapter("brave", {
        "Steuerberater Kiel": ["https://www.a.example/", "https://gelbeseiten.de/x", "https://b.example/impressum"],
        "Steuerkanzlei Kiel": ["https://a.example/team", "https://c.example/"],
    }, fehler_bei=fehler_bei, kosten_je_1000=5.0)  # fmt: skip


def test_websuche_speichert_kandidaten_funde_und_protokoll(storage):
    stat = run_search(storage, _web(), [KIEL], VORLAGEN, BLOCK, "SH", "lauf1", 20)
    assert (stat.queries, stat.treffer, stat.neue_kandidaten, stat.fehler) == (2, 5, 3, 0)
    assert (stat.anfragen, stat.kosten) == (2, 0.01)
    kandidaten = storage.kandidaten("SH")
    assert [k.domain for k in kandidaten] == ["a.example", "b.example", "c.example"]
    assert all(k.crawl_status == SchrittStatus.OFFEN and k.suchort == "Kiel" for k in kandidaten)
    # Portal-Treffer werden verworfen, zweite URL derselben Domain wird zusätzlicher Fund
    funde = storage.funde("SH")
    assert [f.quell_id for f in funde] == ["https://www.a.example/", "https://b.example/impressum",
                                          "https://a.example/team", "https://c.example/"]
    assert funde[2].kandidat_id == funde[0].kandidat_id
    protokoll = storage.lauf_protokoll("SH")
    assert [(e.suchbegriff, e.trefferzahl, e.neue_kandidaten, e.anfragen, e.kosten) for e in protokoll] == [
        ("Steuerberater Kiel", 3, 2, 1, 0.005), ("Steuerkanzlei Kiel", 2, 1, 1, 0.005),
    ]  # fmt: skip


def test_wiederaufnahme_nach_fehler(storage):
    erster = _web(fehler_bei={"Steuerkanzlei Kiel": SearchError("brave: HTTP 500")})
    stat1 = run_search(storage, erster, [KIEL], VORLAGEN, set(), "SH", "lauf1", 20)
    assert (stat1.queries, stat1.fehler) == (1, 1)
    assert storage.kandidat_nach_domain("c.example") is None
    zweiter = _web()
    stat2 = run_search(storage, zweiter, [KIEL], VORLAGEN, set(), "SH", "lauf2", 20)
    assert zweiter.aufrufe == ["Steuerkanzlei Kiel"]
    assert (stat2.queries, stat2.uebersprungen, stat2.neue_kandidaten) == (1, 1, 1)
    dritter = _web()
    run_search(storage, dritter, [KIEL], VORLAGEN, set(), "SH", "lauf3", 20)
    assert dritter.aufrufe == []


@pytest.mark.parametrize("fehler", [KontingentErschoepft("brave: Monatskontingent erschöpft"),
                                    BudgetErschoepft("brave: Budget erreicht")])
def test_limit_beendet_lauf_sauber_und_wiederaufnehmbar(storage, fehler):
    husum = Ort("Husum", "25813")
    erster = _web(fehler_bei={"Steuerkanzlei Kiel": fehler})
    stat = run_search(storage, erster, [KIEL, husum], VORLAGEN, set(), "SH", "lauf1", 20)
    assert stat.abbruch == str(fehler)
    assert erster.aufrufe == ["Steuerberater Kiel", "Steuerkanzlei Kiel"]  # Husum nicht mehr versucht
    assert stat.fehler == 0
    zweiter = _web()
    run_search(storage, zweiter, [KIEL, husum], VORLAGEN, set(), "SH", "lauf2", 20)
    assert zweiter.aufrufe == ["Steuerkanzlei Kiel", "Steuerberater Husum", "Steuerkanzlei Husum"]


def test_budget_ueber_adapter(storage):
    adapter = _web()
    adapter.max_anfragen = 1
    stat = run_search(storage, adapter, [KIEL], VORLAGEN, set(), "SH", "lauf1", 20)
    assert stat.abbruch and "Budget" in stat.abbruch
    assert (stat.queries, stat.anfragen) == (1, 1)


def test_lueckenfueller_fuehrt_zusammen(storage):
    run_search(storage, _web(), [KIEL], VORLAGEN, BLOCK, "SH", "lauf1", 20)
    a = storage.kandidat_nach_domain("a.example")
    places = FakeOrtsAdapter("places", {"Kiel": [
        # gleiche Domain → ergänzt Adresse und Telefon
        hit("places", "https://a.example/", quell_id="p1", name="A Steuerberatung", strasse="Holstenstraße 1",
            plz="24103", ort="Kiel", telefon="0431 111111"),
        # ohne Website, gleiche Telefonnummer wie oben → derselbe Kandidat
        hit("places", None, quell_id="p2", name="A Steuerberatung Filiale", telefon="+49 431 111111"),
        # Website ist Facebook → Website verworfen, Kanzlei bleibt
        hit("places", "https://facebook.com/kanzlei.xaver", quell_id="p3", name="Kanzlei Xaver", strasse="Wall 5",
            plz="24103", ort="Kiel"),
        # gleiche Adresse wie p3, anderer Name → Verdacht, nicht zusammengeführt
        hit("places", None, quell_id="p4", name="Steuerbüro Möller", strasse="Wall 5", plz="24103", ort="Kiel"),
        # PLZ + ähnlicher Name wie p3 → unsicher zusammengeführt
        hit("places", None, quell_id="p5", name="Xaver Kanzlei Kiel", plz="24103", ort="Kiel"),
    ]})  # fmt: skip
    stat = run_search(storage, places, [KIEL], VORLAGEN, BLOCK, "SH", "lauf2", 20)
    assert places.aufrufe == ["Kiel"]
    assert (stat.treffer, stat.neue_kandidaten) == (5, 2)
    a = storage.get_kandidat(a.id)
    assert (a.strasse, a.plz, a.telefon) == ("Holstenstraße 1", "24103", "0431 111111")
    x = next(k for k in storage.kandidaten("SH") if k.name == "Kanzlei Xaver")
    assert x.website is None and x.domain is None
    assert {f.quell_id for f in storage.funde("SH", "places") if f.kandidat_id == a.id} == {"p1", "p2"}
    dubletten = {(d.kriterium, d.art) for d in storage.dubletten("SH")}
    assert dubletten == {("adresse", DublettenArt.VERDACHT), ("plz+name", DublettenArt.UNSICHER)}
    assert len(storage.kandidaten("SH")) == 5  # a, b, c, X, Möller
    # Protokoll als Schritt "fill" je Ort, wiederaufnehmbar
    assert storage.query_erledigt("SH", "places", "ort:Kiel", schritt="fill")
    zweiter = FakeOrtsAdapter("places", {})
    run_search(storage, zweiter, [KIEL], VORLAGEN, BLOCK, "SH", "lauf3", 20)
    assert zweiter.aufrufe == []


def test_bekannter_fund_wird_nicht_doppelt_verarbeitet(storage):
    run_search(storage, _web(), [KIEL], VORLAGEN, set(), "SH", "lauf1", 20)
    n = len(storage.funde("SH"))
    adapter = FakeQueryAdapter("brave", {"Steuerberater Husum": ["https://c.example/"]})
    stat = run_search(storage, adapter, [Ort("Husum", "25813")], ["Steuerberater {ort}"], set(), "SH", "lauf2", 20)
    assert stat.neue_kandidaten == 0
    assert len(storage.funde("SH")) == n


def test_lueckenfueller_ueberspringen_zusatz_suchorte(storage):
    zusatz = Ort("Kiel (24105)", "24105", gemeinde="Kiel")
    places = FakeOrtsAdapter("places", {})
    run_search(storage, places, [KIEL, zusatz], VORLAGEN, set(), "SH", "l1", 20)
    assert places.aufrufe == ["Kiel"]


def test_volle_trefferliste_sucht_stadtteile_nach(storage):
    """Pilot: „Steuerberater Kiel" bricht bei 60 ab, die Startliste hat keine Stadtteil-Einträge."""
    from fakes import FakeQueryAdapter

    from recherche.search.pipeline import Suchplan

    kiel = Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")
    heide = Ort("Heide", "25746", "Kreis Dithmarschen")
    voll = [f"https://kanzlei{i}.example/" for i in range(20)]
    adapter = FakeQueryAdapter("fake", {"Steuerberater Kiel": voll, "Steuerberater Heide": voll[:3],
                                        "Steuerberater 24105 Kiel": ["https://holtenau-stb.example/"]})
    aufgerufen = []

    def stadtteile(ort):
        aufgerufen.append(ort.name)
        return [Ort(f"Kiel ({p})", p, ort.kreis, gemeinde="Kiel") for p in ("24103", "24105")]

    plan = Suchplan(["Steuerberater {ort}"], ["Steuerberater {plz} {gemeinde}"])
    stat = run_search(storage, adapter, [kiel, heide], plan, set(), "SH", "l1", 20, stadtteile=stadtteile)
    assert aufgerufen == ["Kiel"] and stat.stadtteile == 2  # nur die volle Stadt, einmal
    assert "Steuerberater 24105 Kiel" in adapter.aufrufe
    assert any(k.domain == "holtenau-stb.example" for k in storage.kandidaten("SH"))
    # Wiederaufnahme / Pilot-Bestand: Stadt-Query schon erledigt, Stadtteile noch nicht → trotzdem nachsuchen
    aufgerufen.clear()
    stat2 = run_search(storage, FakeQueryAdapter("fake", {}), [kiel], plan, set(), "SH", "l2", 20,
                       stadtteile=lambda o: [Ort("Kiel (24159)", "24159", o.kreis, gemeinde="Kiel")])
    assert stat2.stadtteile == 1 and stat2.queries == 1


def test_stadtteile_nachsuchen_aus_openplz():
    from recherche.config import StadtteilConfig
    from recherche.orte import stadtteile_nachsuchen

    class FakeOpenPlz:
        aufrufe = 0

        def localities(self, land, fortschritt=None):
            self.aufrufe += 1
            m = {"key": "01002000", "name": "Kiel, Landeshauptstadt"}
            return [{"postalCode": p, "name": n, "municipality": m} for p, n in
                    (("24103", "Kiel"), ("24105", "Kiel"), ("24159", "Kiel-Holtenau"))]

    openplz = FakeOpenPlz()
    nachsuchen = stadtteile_nachsuchen(openplz, "01", StadtteilConfig())
    neue = nachsuchen(Ort("Kiel", "24103", "Kreisfreie Stadt Kiel"))
    assert [(o.name, o.gemeinde, o.stadtteil) for o in neue] == [
        ("Kiel (24103)", "Kiel", None), ("Kiel (24105)", "Kiel", None), ("Kiel (24159)", "Kiel", "Kiel-Holtenau")]
    assert nachsuchen(Ort("Wedel", "22880", "Kreis Pinneberg")) == []  # nicht kreisfrei
    assert nachsuchen(Ort("Kiel", "24103", "Kreisfreie Stadt Kiel", 5000)) == []  # zu klein laut Einwohnern
    assert openplz.aufrufe == 1  # einmal geladen


def test_fremdort_filter_bei_kleingemeinden(storage, settings):
    """Brave füllt bei „Steuerberater Tetenbüll" mit bundesweiten Kanzleien auf."""
    from fakes import FakeQueryAdapter, hit

    from recherche.search.pipeline import Fremdorte

    fremd = Fremdorte.laden(settings.pfad(settings.pfade.grossstaedte), "SH", 2000)
    klein = Ort("Tetenbüll", "25882", "Kreis Nordfriesland", 600)
    gross = Ort("Husum", "25813", "Kreis Nordfriesland", 23000)
    treffer = [
        hit("brave", "https://bonn-stb.example/", name="Steuerberater Bonn – Kanzlei Rhein", snippet="Ihre Kanzlei in Bonn"),
        hit("brave", "https://nf-stb.example/", name="Steuerberater Tetenbüll", snippet="Kanzlei bei Bonn-Tetenbüll"),
        hit("brave", "https://kiel-stb.example/", name="Steuerberater Kiel"),  # Kiel liegt in SH → bleibt
        hit("brave", "https://hagen-mueller.example/", name="Steuerberater Hagen Müller"),  # „Hagen" nicht in der Liste
    ]
    adapter = FakeQueryAdapter("fake", {"Steuerberater Tetenbüll": treffer, "Steuerberater Husum": treffer})
    stat = run_search(storage, adapter, [klein, gross], ["Steuerberater {ort}"], set(), "SH", "l1", 20, fremdorte=fremd)
    domains = {k.domain for k in storage.kandidaten("SH")}
    assert stat.fremdort == 1 and "nf-stb.example" in domains and "kiel-stb.example" in domains
    assert "bonn-stb.example" in domains  # bei Husum (über 2.000 Einwohner) wird nicht gefiltert
    assert len(storage.funde_kandidat(next(k.id for k in storage.kandidaten("SH") if k.domain == "bonn-stb.example"))) == 1


def test_kleingemeinden_blaettern_nicht_weiter():
    from recherche.config import EinwohnerStufe, WeiterWennVollConfig
    from recherche.search.pipeline import suche_query

    class Seitenweise:
        blaettert, seitengroesse, aufrufe = True, 2, 0

        def seiten(self, query, max_results):
            for i in range(3):
                self.aufrufe += 1
                yield [hit_(f"https://k{i}{j}.example/") for j in range(2)]

    from fakes import hit as _hit

    def hit_(url):
        return _hit("brave", url)

    a = Seitenweise()
    hits, extra = suche_query(a, "q", EinwohnerStufe(seiten=1, weiter=False), 60, set(), WeiterWennVollConfig())
    assert (len(hits), extra, a.aufrufe) == (2, 0, 1)
    b = Seitenweise()
    assert suche_query(b, "q", EinwohnerStufe(seiten=1), 60, set(), WeiterWennVollConfig())[1] == 2


def test_suche_auswertung_je_einwohnerstufe(storage):
    from recherche.config import EinwohnerStufe
    from recherche.models import Kanzlei, LaufEintrag, Status
    from recherche.search.auswertung import auswerten
    from recherche.search.pipeline import Suchplan

    plan = Suchplan(["Steuerberater {ort}"], regel=[EinwohnerStufe(unter=2000, seiten=1),
                                                    EinwohnerStufe(unter=20000, seiten=2), EinwohnerStufe(seiten=3)])
    orte = [Ort("Tetenbüll", "25882", einwohner=600), Ort("Husum", "25813", einwohner=23000)]
    for ort, anfragen in (("Tetenbüll", 3), ("Husum", 3)):
        storage.log_lauf(LaufEintrag(lauf_id="l", schritt="search", bundesland="SH", adapter="brave", ort=ort,
                                     suchbegriff=f"Steuerberater {ort}", anfragen=anfragen, kosten=anfragen * 0.005))
    from recherche.models import Kandidat

    for i, (ort, status, land) in enumerate([("Tetenbüll", Status.AUSSERHALB, "Sachsen"),
                                              ("Tetenbüll", Status.NEU, "Schleswig-Holstein"),
                                              ("Husum", Status.NEU, "Schleswig-Holstein"),
                                              ("Husum", Status.KEINE_KANZLEI, "Schleswig-Holstein"),
                                              ("Husum", None, None)]):
        kid = storage.add_kandidat(Kandidat(bundesland="SH", domain=f"k{i}.example", suchort=ort, quelle="brave"))
        if status:
            storage.upsert_kanzlei(Kanzlei(kandidat_id=kid, domain=f"k{i}.example", lauf_bundesland="SH",
                                           status=status, bundesland=land))
    z = {s.stufe: s for s in auswerten(storage, "SH", orte, plan, "brave", "Schleswig-Holstein")}
    klein, gross = z["0–1.999 Einwohner"], z["ab 20.000 Einwohner"]
    assert (klein.anfragen, klein.ueber_regel, klein.kandidaten, klein.kanzleien, klein.ausserhalb) == (3, 2, 2, 1, 1)
    assert (gross.kanzleien, gross.keine_kanzlei, gross.nicht_gelesen, gross.ueber_regel) == (1, 1, 1, 0)
    assert klein.kosten_je_kanzlei == pytest.approx(0.015)


def test_suche_auswertung_mit_eigenen_grenzen(storage):
    from recherche.models import LaufEintrag
    from recherche.search.auswertung import auswerten
    from recherche.search.pipeline import Suchplan

    orte = [Ort("Klein", "25882", einwohner=300), Ort("Mittel", "25813", einwohner=1500)]
    for o in orte:
        storage.log_lauf(LaufEintrag(lauf_id="l", schritt="search", bundesland="SH", adapter="brave", ort=o.name,
                                     suchbegriff=f"Steuerberater {o.name}", anfragen=1))
    stufen = [z.stufe for z in auswerten(storage, "SH", orte, Suchplan([]), "brave", "Schleswig-Holstein", [500, 2000])]
    assert stufen == ["0–499 Einwohner", "500–1.999 Einwohner"]


def test_stufe_ohne_eigene_suche():
    from recherche.config import EinwohnerStufe
    from recherche.search.pipeline import Suchplan

    plan = Suchplan(["Steuerberater {ort}"], regel=[EinwohnerStufe(unter=500, varianten=0), EinwohnerStufe(varianten=1)])
    assert plan.queries(Ort("Winzig", "25882", einwohner=120)) == []
    assert plan.queries(Ort("Husum", "25813", einwohner=23000)) == ["Steuerberater Husum"]
