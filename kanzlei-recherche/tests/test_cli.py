import pytest
from fakes import FakeAdapter
from openpyxl import load_workbook
from rich.console import Console
from typer.testing import CliRunner

from recherche import cli
from recherche.storage import Storage

runner = CliRunner()


@pytest.fixture(autouse=True)
def _breite_konsole(monkeypatch):
    monkeypatch.setattr(cli, "console", Console(width=300))


def _fake_registry(monkeypatch):
    ddgs = FakeAdapter("ddgs", {
        "Steuerberater Kiel": ["https://a.example/", "https://www.facebook.com/x"],
        "Steuerberater Husum": ["https://h.example/"],
    })  # fmt: skip
    google = FakeAdapter("google", {"Steuerberater Kiel": ["https://a.example/", "https://g.example/"]})
    brave = FakeAdapter("brave", {}, grund="BRAVE_API_KEY fehlt in .env")
    monkeypatch.setattr(cli, "build_adapter", lambda name, s: {"ddgs": ddgs, "google": google}[name])
    monkeypatch.setattr(cli, "vergleichs_adapter", lambda s: [ddgs, google, brave])
    return ddgs


def test_hilfe_listet_alle_befehle():
    r = runner.invoke(cli.app, ["--help"])
    assert r.exit_code == 0
    for befehl in ("search", "crawl", "extract", "export", "run", "search-compare", "orte"):
        assert befehl in r.output


def test_export(befuellt, db_pfad, tmp_path):
    befuellt.close()
    out = tmp_path / "x.xlsx"
    r = runner.invoke(cli.app, ["export", "--bundesland", "SH", "--db", str(db_pfad), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "20 Kanzleien" in r.output
    assert load_workbook(out)["Kanzleien"].max_row == 21


def test_unbekanntes_bundesland(db_pfad):
    r = runner.invoke(cli.app, ["export", "--bundesland", "XX", "--db", str(db_pfad)])
    assert r.exit_code == 2
    assert "nicht in settings.yaml" in r.output


def test_platzhalter_laufen(db_pfad):
    for befehl in ("crawl", "extract"):
        r = runner.invoke(cli.app, [befehl, "-b", "SH", "--db", str(db_pfad)])
        assert r.exit_code == 0, r.output
        assert "noch nicht implementiert" in r.output
    r = runner.invoke(cli.app, ["orte", "-b", "SH"])
    assert r.exit_code == 0 and "80 Orten" in r.output


def test_search_mit_fake_adapter_wiederaufnehmbar(monkeypatch, db_pfad):
    ddgs = _fake_registry(monkeypatch)
    args = ["search", "-b", "SH", "--db", str(db_pfad), "--orte", "Kiel,Husum"]
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    with Storage(db_pfad) as st:
        assert [k.domain for k in st.kandidaten("SH")] == ["a.example", "h.example"]
    n = len(ddgs.aufrufe)
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0
    assert len(ddgs.aufrufe) == n  # alles schon erledigt
    assert "schon erledigt" in r.output


def test_search_adapter_nicht_bereit(monkeypatch, db_pfad):
    monkeypatch.setattr(cli, "build_adapter", lambda n, s: FakeAdapter("brave", {}, grund="BRAVE_API_KEY fehlt"))
    r = runner.invoke(cli.app, ["search", "--db", str(db_pfad), "--adapter", "brave"])
    assert r.exit_code == 2
    assert "BRAVE_API_KEY fehlt" in r.output


def test_run(monkeypatch, db_pfad, tmp_path):
    _fake_registry(monkeypatch)
    out = tmp_path / "run.xlsx"
    r = runner.invoke(cli.app, ["run", "-b", "SH", "--db", str(db_pfad), "--orte", "Kiel", "--out", str(out)])
    assert r.exit_code == 0, r.output
    wb = load_workbook(out)
    assert wb["Lauf-Protokoll"].max_row > 1
    assert wb["Kanzleien"].max_row == 1  # Extraktion noch Platzhalter


def test_search_compare(monkeypatch, db_pfad, tmp_path):
    _fake_registry(monkeypatch)
    out = tmp_path / "vergleich.csv"
    r = runner.invoke(cli.app, ["search-compare", "--orte", "Kiel,Husum", "--db", str(db_pfad), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "übersprungen" in r.output and "BRAVE_API_KEY" in r.output
    assert "ddgs vs. google" in r.output
    assert "nur in ddgs" in r.output and "h.example" in r.output
    assert "nur in google" in r.output and "g.example" in r.output
    assert out.exists()
    # Vergleich schreibt nichts in die Datenbank
    with Storage(db_pfad) as st:
        assert st.kandidaten("SH") == []
