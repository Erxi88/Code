"""fixture-klassen-umstellen: erwartet.json auf die neuen Größenklassen."""

import json
import shutil

from fakes import FIXTURES
from typer.testing import CliRunner

from recherche import cli
from recherche.fixtures import klassen_umstellen

BSP = FIXTURES / "kanzleiseiten" / "beispiele"


def _fixture(ordner, name, quelle, **soll):
    ziel = ordner / name
    shutil.copytree(BSP / quelle, ziel)
    d = json.loads((ziel / "erwartet.json").read_text(encoding="utf-8-sig"))
    d.update(soll)
    (ziel / "erwartet.json").write_text("﻿" + json.dumps(d, ensure_ascii=False), encoding="utf-8")  # PowerShell-BOM
    return ziel


def test_umstellen(tmp_path):
    _fixture(tmp_path, "a", "hansen-steuerberatung.example", teamgroesse=12, teamgroesse_klasse="6-15")
    _fixture(tmp_path, "b", "foerde-steuer.example", teamgroesse=None, teamgroesse_klasse="16-50")
    _fixture(tmp_path, "c", "nordlicht-steuerberatung.example", teamgroesse=11, teamgroesse_klasse=None)  # schon richtig
    _fixture(tmp_path, "d", "mittelstand-steuer.example", teamgroesse=None, teamgroesse_klasse="21-50")  # schon neu
    probe = klassen_umstellen(tmp_path, schreiben=False)
    assert [(u.domain, u.alt, u.neu) for u in probe] == [("a", "6-15", "11-20"), ("b", "16-50", None)]
    assert probe[1].vorschlag == "21-50" and "über 20" in probe[1].hinweis  # aus dem Teamtext, nicht übernommen
    assert not (tmp_path / "a" / "erwartet.json.bak").exists()
    klassen_umstellen(tmp_path)
    a = json.loads((tmp_path / "a" / "erwartet.json").read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "b" / "erwartet.json").read_text(encoding="utf-8"))
    assert (a["teamgroesse"], a["teamgroesse_klasse"], b["teamgroesse_klasse"]) == (12, "11-20", None)
    assert "6-15" in (tmp_path / "a" / "erwartet.json.bak").read_text(encoding="utf-8")
    assert klassen_umstellen(tmp_path) == []  # zweiter Lauf: nichts mehr zu tun


def test_cli(tmp_path):
    _fixture(tmp_path, "b", "foerde-steuer.example", teamgroesse=None, teamgroesse_klasse="16-50")
    r = CliRunner().invoke(cli.app, ["fixture-klassen-umstellen", "--ordner", str(tmp_path), "--probe"])
    assert r.exit_code == 0, r.output
    assert "unentscheidbar" in r.output and "Probe" in r.output
