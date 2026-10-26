import json

import pytest
from conftest import FIXTURES
from fakes import FakeOrtsAdapter, FakeQueryAdapter, hit
from openpyxl import load_workbook
from rich.console import Console
from typer.testing import CliRunner

from recherche import cli
from recherche.config import load_settings
from recherche.models import Fund, Kandidat
from recherche.storage import Storage

runner = CliRunner()


@pytest.fixture(autouse=True)
def _breite_konsole(monkeypatch):
    monkeypatch.setattr(cli, "console", Console(width=300))


@pytest.fixture
def fakes(monkeypatch):
    brave = FakeQueryAdapter("brave", {
        "Steuerberater Kiel": ["https://a.example/", "https://www.facebook.com/x"],
        "Steuerberater Husum": ["https://h.example/"],
    }, kosten_je_1000=5.0)  # fmt: skip
    ddgs = FakeQueryAdapter("ddgs", {"Steuerberater Kiel": ["https://a.example/", "https://g.example/"]})
    places = FakeOrtsAdapter("places", {"Kiel": [hit("places", None, quell_id="p1", name="Kanzlei Ohneweb",
                                                     strasse="Wall 5", plz="24103", ort="Kiel")]})
    google = FakeQueryAdapter("google_cse", {}, grund="GOOGLE_CSE_API_KEY fehlt in .env", aktiv=False)
    alle = {"brave": brave, "ddgs": ddgs, "places": places, "google_cse": google}
    monkeypatch.setattr(cli, "build_adapter", lambda name, s, cache=None: alle[name])
    monkeypatch.setattr(cli, "aktive_adapter", lambda s, cache=None: [brave, ddgs, places, google])
    return alle


def _args(befehl, db_pfad, *rest):
    return [befehl, "-b", "SH", "--db", str(db_pfad), *rest]


def test_hilfe_listet_alle_befehle():
    r = runner.invoke(cli.app, ["--help"])
    assert r.exit_code == 0
    for befehl in ("search", "fill", "coverage", "crawl", "extract", "export", "run", "search-compare", "orte"):
        assert befehl in r.output


def test_export(befuellt, db_pfad, tmp_path):
    befuellt.close()
    out = tmp_path / "x.xlsx"
    r = runner.invoke(cli.app, _args("export", db_pfad, "--out", str(out)))
    assert r.exit_code == 0, r.output
    assert "20 Kanzleien" in r.output
    assert load_workbook(out)["Kanzleien"].max_row == 21


def test_unbekanntes_bundesland(db_pfad):
    r = runner.invoke(cli.app, ["export", "--bundesland", "XX", "--db", str(db_pfad)])
    assert r.exit_code == 2
    assert "nicht in settings.yaml" in r.output


def test_platzhalter_laufen(db_pfad):
    for befehl in ("crawl", "extract"):
        r = runner.invoke(cli.app, _args(befehl, db_pfad))
        assert r.exit_code == 0, r.output
        assert "noch nicht implementiert" in r.output
    r = runner.invoke(cli.app, ["orte", "-b", "SH"])
    assert r.exit_code == 0 and "80 Orten (0 mit Einwohnerzahl)" in r.output


def test_search_stufe_a_mit_kostenlog_und_wiederaufnahme(fakes, db_pfad):
    args = _args("search", db_pfad, "--orte", "Kiel,Husum")
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    assert "Websuche (Stufe A) mit brave" in r.output
    assert "Kostenlog brave: 8 Anfragen ≈ 0.04" in r.output  # 2 Orte x 4 Vorlagen
    with Storage(db_pfad) as st:
        assert [k.domain for k in st.kandidaten("SH")] == ["a.example", "h.example"]
    n = len(fakes["brave"].aufrufe)
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0
    assert len(fakes["brave"].aufrufe) == n
    assert "8 schon erledigt" in r.output


def test_search_abbruch_bei_limit(fakes, db_pfad):
    from recherche.search.base import KontingentErschoepft

    fakes["brave"].fehler_bei = {"Steuerberater Kiel": KontingentErschoepft("brave: Monatskontingent erschöpft")}
    r = runner.invoke(cli.app, _args("search", db_pfad, "--orte", "Kiel"))
    assert r.exit_code == 0
    assert "Lauf vorzeitig beendet" in r.output and "Monatskontingent" in r.output


def test_search_adapter_nicht_bereit(fakes, db_pfad):
    r = runner.invoke(cli.app, _args("search", db_pfad, "--adapter", "google_cse"))
    assert r.exit_code == 2
    assert "GOOGLE_CSE_API_KEY fehlt" in r.output


def test_search_lehnt_lueckenfueller_ab(fakes, db_pfad):
    r = runner.invoke(cli.app, _args("search", db_pfad, "--adapter", "places"))
    assert r.exit_code == 2 and "recherche fill --quelle places" in r.output


def test_fill_verlangt_ortsauswahl(fakes, db_pfad):
    r = runner.invoke(cli.app, _args("fill", db_pfad, "--quelle", "places"))
    assert r.exit_code == 2 and "--orte" in r.output and "--alle" in r.output
    r = runner.invoke(cli.app, _args("fill", db_pfad, "--quelle", "places", "--orte", "Kiel", "--alle"))
    assert r.exit_code == 2


def test_fill_mit_orten(fakes, db_pfad):
    r = runner.invoke(cli.app, _args("fill", db_pfad, "--quelle", "places", "--orte", "Kiel"))
    assert r.exit_code == 0, r.output
    assert "Lückenfüller (Stufe B) mit places in 1 Orten" in r.output
    assert fakes["places"].aufrufe == ["Kiel"]
    with Storage(db_pfad) as st:
        assert st.kandidaten("SH")[0].name == "Kanzlei Ohneweb"


def test_fill_lehnt_websuche_ab(fakes, db_pfad):
    r = runner.invoke(cli.app, _args("fill", db_pfad, "--quelle", "brave", "--alle"))
    assert r.exit_code == 2 and "recherche search --adapter brave" in r.output


def test_fill_deaktivierte_quelle(db_pfad):
    r = runner.invoke(cli.app, _args("fill", db_pfad, "--quelle", "stb_verzeichnis", "--alle"))
    assert r.exit_code == 2 and "deaktiviert" in r.output


def _coverage_config(tmp_path):
    """settings.yaml-Kopie mit Test-Ortsliste und -Referenz."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    s = load_settings()
    text = (s.basis_dir / "config" / "settings.yaml").read_text(encoding="utf-8")
    text = text.replace("orte_datei: config/orte_SH.csv", f"orte_datei: {FIXTURES / 'coverage' / 'orte.csv'}")
    text = text.replace("referenz: config/referenz_kammern.yaml", f"referenz: {FIXTURES / 'coverage' / 'referenz.yaml'}")
    text = text.replace("queries: config/queries.yaml", f"queries: {s.basis_dir / 'config' / 'queries.yaml'}")
    text = text.replace("blocklist: config/domain_blocklist.txt", f"blocklist: {s.basis_dir / 'config' / 'domain_blocklist.txt'}")
    (cfg / "settings.yaml").write_text(text, encoding="utf-8")
    return cfg / "settings.yaml"


def _coverage_daten(db_pfad):
    with Storage(db_pfad) as st:
        for i, k in enumerate(json.loads((FIXTURES / "coverage" / "kandidaten.json").read_text())["kandidaten"]):
            quellen = k.pop("quellen")
            kid = st.add_kandidat(Kandidat(bundesland="SH", **k))
            for q in quellen:
                st.add_fund(Fund(kandidat_id=kid, bundesland="SH", quelle=q, quell_id=f"{q}-{i}"))


def test_coverage(db_pfad, tmp_path):
    _coverage_daten(db_pfad)
    out = tmp_path / "cov.xlsx"
    r = runner.invoke(cli.app, _args("coverage", db_pfad, "--config", str(_coverage_config(tmp_path)), "--out", str(out)))
    assert r.exit_code == 0, r.output
    assert "19 Kanzleien gefunden, erwartet 50" in r.output
    assert "nur in einer Quelle: 16 (84%)" in r.output
    assert "Kreisfreie Stadt Kiel" in r.output and "Orte mit der größten Lücke" in r.output
    assert "Orte für `fill --luecken` (7): Kiel, Eckernförde" in r.output
    assert out.exists()


def test_coverage_ohne_referenz(db_pfad):
    r = runner.invoke(cli.app, _args("coverage", db_pfad))
    assert r.exit_code == 0
    assert "Keine (vollständigen) Referenzzahlen" in r.output


def test_fill_luecken(fakes, db_pfad, tmp_path):
    _coverage_daten(db_pfad)
    r = runner.invoke(cli.app, _args("fill", db_pfad, "--quelle", "places", "--luecken",
                                     "--config", str(_coverage_config(tmp_path))))
    assert r.exit_code == 0, r.output
    assert fakes["places"].aufrufe == ["Kiel", "Eckernförde", "Rendsburg", "Kronshagen", "Niebüll", "Husum", "Sylt"]


def test_fill_luecken_ohne_referenz(fakes, db_pfad):
    r = runner.invoke(cli.app, _args("fill", db_pfad, "--quelle", "places", "--luecken"))
    assert r.exit_code == 2 and "Keine Orte unter der Referenz" in r.output


def test_run(fakes, db_pfad, tmp_path):
    out = tmp_path / "run.xlsx"
    r = runner.invoke(cli.app, _args("run", db_pfad, "--orte", "Kiel", "--out", str(out)))
    assert r.exit_code == 0, r.output
    wb = load_workbook(out)
    assert wb["Lauf-Protokoll"].max_row > 1
    assert wb["Kanzleien"].max_row == 1  # Extraktion noch Platzhalter


def test_run_mit_ddgs(fakes, db_pfad, tmp_path):
    r = runner.invoke(cli.app, _args("run", db_pfad, "--orte", "Kiel", "--adapter", "ddgs", "--out", str(tmp_path / "r.xlsx")))
    assert r.exit_code == 0, r.output
    assert "Websuche (Stufe A) mit ddgs" in r.output
    assert fakes["brave"].aufrufe == []


def test_search_compare_alle_aktiven(fakes, db_pfad, tmp_path):
    out = tmp_path / "vergleich.csv"
    r = runner.invoke(cli.app, _args("search-compare", db_pfad, "--orte", "Kiel,Husum", "--out", str(out)))
    assert r.exit_code == 0, r.output
    assert "übersprungen: google_cse – GOOGLE_CSE_API_KEY" in r.output
    assert "Überschneidung je Paar" in r.output
    assert "nur in ddgs: g.example" in r.output
    assert "nur in places: Kanzlei Ohneweb, Wall 5, 24103 Kiel" in r.output
    assert out.exists()
    with Storage(db_pfad) as st:
        assert st.kandidaten("SH") == []  # Vergleich schreibt keine Kandidaten


def test_search_compare_auswahl(fakes, db_pfad):
    r = runner.invoke(cli.app, _args("search-compare", db_pfad, "--orte", "Kiel", "--adapter", "brave,ddgs"))
    assert r.exit_code == 0, r.output
    assert "places" not in r.output.split("Je Adapter")[1].split("Überschneidung")[0]
