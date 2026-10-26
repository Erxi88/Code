import pytest
from fakes import hit

from recherche.config import (
    EinwohnerStufe,
    QuelleConfig,
    WeiterWennVollConfig,
    load_settings,
)
from recherche.search.base import QueryAdapter
from recherche.search.pipeline import (
    Ort,
    Suchplan,
    anteil_kanzlei_domains,
    run_search,
    suche_query,
)

VORLAGEN = ["Steuerberater {ort}", "Steuerkanzlei {ort}", "Steuerberatung {plz} {ort}", "Steuerberater {ort} Impressum"]
STADTTEIL = ["Steuerberater {plz} {gemeinde}", "Steuerberater {stadtteil}", "Steuerkanzlei {plz} {gemeinde}"]
REGEL = [EinwohnerStufe(unter=2000, varianten=1, seiten=1), EinwohnerStufe(unter=20000, varianten=2, seiten=2),
         EinwohnerStufe(seiten=3)]
BLOCK = {"gelbeseiten.de", "dasoertliche.de"}


@pytest.fixture
def plan():
    return Suchplan(VORLAGEN, STADTTEIL, REGEL, EinwohnerStufe(seiten=3), EinwohnerStufe(seiten=2), WeiterWennVollConfig())


@pytest.mark.parametrize(("einwohner", "varianten", "seiten"), [
    (300, 1, 1), (1999, 1, 1), (2000, 2, 2), (19999, 2, 2), (20000, 4, 3), (247000, 4, 3), (None, 4, 3),
])  # fmt: skip
def test_stufen_und_grenzen(plan, einwohner, varianten, seiten):
    ort = Ort("Ort", "24000", einwohner=einwohner)
    assert len(plan.queries(ort)) == varianten
    assert plan.stufe(ort).seiten == seiten


def test_erste_varianten_in_reihenfolge(plan):
    assert plan.queries(Ort("Lindau", "24214", einwohner=900)) == ["Steuerberater Lindau"]
    assert plan.queries(Ort("Heide", "25746", einwohner=21540))[-1] == "Steuerberater Heide Impressum"


def test_zusatz_suchorte_nutzen_stadtteil_vorlagen(plan):
    mit = Ort("Kiel (24159)", "24159", gemeinde="Kiel", stadtteil="Kiel-Holtenau")
    ohne = Ort("Kiel (24105)", "24105", gemeinde="Kiel")
    assert plan.queries(mit) == ["Steuerberater 24159 Kiel", "Steuerberater Kiel-Holtenau", "Steuerkanzlei 24159 Kiel"]
    assert plan.queries(ohne) == ["Steuerberater 24105 Kiel", "Steuerkanzlei 24105 Kiel"]  # ohne {stadtteil}
    assert plan.stufe(ohne).seiten == 2


def test_ohne_regel_wie_bisher():
    p = Suchplan(VORLAGEN)
    assert len(p.queries(Ort("Dorf", "24000", einwohner=10))) == 4
    assert p.stufe(Ort("Dorf", "24000")).seiten is None


def test_settings_regel():
    plan = Suchplan.aus_settings(load_settings())
    assert [(s.unter, s.varianten, s.seiten) for s in plan.regel] == [(2000, 1, 1), (20000, 2, 2), (None, None, 3)]
    assert plan.weiter.aktiv and plan.weiter.anteil_kanzlei_domains == 0.5
    assert plan.stadtteil_vorlagen[0] == "Steuerberater {plz} {gemeinde}"


# ---------------------------------------------------------------- Seitenbegrenzung


def kanzleien(n, start=0):
    return [hit("brave", f"https://kanzlei{i}.example/") for i in range(start, start + n)]


def portale(n):
    return [hit("brave", f"https://www.gelbeseiten.de/x{i}") for i in range(n)]


class Blaetternd(QueryAdapter):
    """Fake mit echtem Paging: zählt, wie viele Seiten wirklich abgerufen werden."""

    blaettert = True
    name = "brave"

    def __init__(self, seiten):
        super().__init__(QuelleConfig(aktiv=True))
        self._seiten = seiten
        self.abgerufen = 0

    def seiten(self, query, max_results):
        anzahl = 0
        for s in self._seiten:
            if anzahl >= max_results:
                return
            self.abgerufen += 1
            anzahl += len(s)
            yield s
            if len(s) < 20:
                return

    def search(self, query, max_results):
        return [h for s in self.seiten(query, max_results) for h in s]


WEITER = WeiterWennVollConfig()


def test_eine_seite_bei_kleinem_ort():
    a = Blaetternd([kanzleien(5)])
    hits, extra = suche_query(a, "q", EinwohnerStufe(seiten=1), 60, BLOCK, WEITER)
    assert (len(hits), extra, a.abgerufen) == (5, 0, 1)


def test_volle_seite_mit_kanzleien_holt_naechste_seite():
    a = Blaetternd([kanzleien(14) + portale(6), kanzleien(20, 100), portale(12) + kanzleien(8, 200)])
    hits, extra = suche_query(a, "q", EinwohnerStufe(seiten=1), 60, BLOCK, WEITER)
    # Seite 1 voll, 70 % Kanzleien → Seite 2; Seite 2 voll, 100 % → Seite 3; Seite 3 mehrheitlich Portale → Ende
    assert (a.abgerufen, extra, len(hits)) == (3, 2, 60)


def test_volle_seite_mit_portalen_endet():
    a = Blaetternd([kanzleien(9) + portale(11), kanzleien(20)])
    _, extra = suche_query(a, "q", EinwohnerStufe(seiten=1), 60, BLOCK, WEITER)
    assert (a.abgerufen, extra) == (1, 0)


def test_nicht_volle_seite_endet():
    a = Blaetternd([kanzleien(19), kanzleien(20)])
    suche_query(a, "q", EinwohnerStufe(seiten=1), 60, BLOCK, WEITER)
    assert a.abgerufen == 1


def test_weiter_abschaltbar_und_obergrenze():
    a = Blaetternd([kanzleien(20), kanzleien(20, 50), kanzleien(20, 90), kanzleien(20, 130)])
    suche_query(a, "q", EinwohnerStufe(seiten=1), 60, BLOCK, WeiterWennVollConfig(aktiv=False))
    assert a.abgerufen == 1
    b = Blaetternd([kanzleien(20), kanzleien(20, 50), kanzleien(20, 90), kanzleien(20, 130)])
    hits, _ = suche_query(b, "q", EinwohnerStufe(seiten=1), 60, BLOCK, WEITER)
    assert (b.abgerufen, len(hits)) == (3, 60)  # max_results_per_query bleibt harte Grenze


def test_seiten_unbegrenzt_und_ohne_paging():
    a = Blaetternd([kanzleien(20), kanzleien(20, 50), kanzleien(3, 90)])
    assert len(suche_query(a, "q", EinwohnerStufe(seiten=None), 60, BLOCK, WEITER)[0]) == 43

    class OhnePaging(QueryAdapter):
        name = "ddgs"

        def __init__(self):
            super().__init__(QuelleConfig(aktiv=True))
            self.max = None

        def search(self, query, max_results):
            self.max = max_results
            return []

    o = OhnePaging()
    suche_query(o, "q", EinwohnerStufe(seiten=2), 60, BLOCK, WEITER)
    assert o.max == 40  # Seitenregel als Trefferzahl


def test_anteil_kanzlei_domains():
    assert anteil_kanzlei_domains(kanzleien(3) + portale(1), BLOCK) == 0.75
    assert anteil_kanzlei_domains([], BLOCK) == 0.0


def test_run_search_mit_plan(storage, plan):
    a = Blaetternd([kanzleien(20), kanzleien(20, 50)])
    orte = [Ort("Lindau", "24214", einwohner=900), Ort("Meldorf", "25704", einwohner=7300)]
    stat = run_search(storage, a, orte, plan, BLOCK, "SH", "l1", 60)
    protokoll = [e.suchbegriff for e in storage.lauf_protokoll("SH")]
    assert protokoll == ["Steuerberater Lindau", "Steuerberater Meldorf", "Steuerkanzlei Meldorf"]
    assert stat.zusatzseiten == 1  # Lindau: 1 Seite erlaubt, aber voll mit Kanzleien → Seite 2


def test_brave_seitenweise_lazy(monkeypatch):
    from fakes import FakeHttp, fixture_json

    from recherche.config import BraveConfig
    from recherche.search.brave import BraveAdapter

    monkeypatch.setenv("BRAVE_API_KEY", "k")
    seite0, seite1 = (fixture_json("search", "brave", f"kiel_seite{i}.json") for i in (0, 1))

    def brave(antworten):
        http = FakeHttp(antworten)
        return BraveAdapter(BraveConfig(aktiv=True, pause_s=0), client=http.client(), sleep=lambda s: None), http

    # Seite 0: 20 Treffer, davon 18 ohne Portal laut BLOCK → trotz Begrenzung auf 1 Seite weiter
    b, http = brave([(200, seite0), (200, seite1)])
    hits, extra = suche_query(b, "Steuerberater Kiel", EinwohnerStufe(seiten=1), 60, BLOCK, WEITER)
    assert (len(http.anfragen), extra, len(hits)) == (2, 1, 25)
    # ohne Ausnahme wird die zweite Seite gar nicht erst abgerufen (keine Kosten)
    b2, http2 = brave([(200, seite0)])
    suche_query(b2, "Steuerberater Kiel", EinwohnerStufe(seiten=1), 60, BLOCK, WeiterWennVollConfig(aktiv=False))
    assert len(http2.anfragen) == 1
