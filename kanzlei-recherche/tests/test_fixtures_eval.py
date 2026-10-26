import json
import shutil

import pytest
from fakes import FakeWeb, beispiel_seiten

from recherche.config import CrawlConfig, load_settings
from recherche.extract.rules import Ausschluss
from recherche.fixtures import (
    ERWARTET_VORLAGE,
    KERNFELDER,
    Fixture,
    aufgezeichnete_modelle,
    auswerten,
    feld_richtig,
    fixture_holen_sync,
    fixtures_finden,
)  # fmt: skip

S = load_settings()
BSP = S.pfad(S.pfade.fixtures_beispiele)
ECHT = S.pfad(S.pfade.fixtures_echt)
AUSSCHLUSS = Ausschluss.laden(S.pfad(S.pfade.impressum_ausschluss))


def test_beispiele_vollstaendig():
    fx = fixtures_finden([BSP])
    assert [f.domain for f in fx] == ["foerde-steuer.example", "hansen-steuerberatung.example", "steuerbuero-schuett.example"]
    assert all(f.fertig for f in fx)
    assert aufgezeichnete_modelle(fx) == ["qwen3.5:9b", "qwen3.6:27b"]
    assert set(fx[1].seiten) == {"start", "impressum", "kontakt", "team"}


def test_beispiele_alle_felder_richtig():
    erg = auswerten(fixtures_finden([BSP]), AUSSCHLUSS, "qwen3.5:9b")
    assert erg.fixtures == 3 and erg.fehler == []
    assert all(erg.quote(f) == 1.0 for f in KERNFELDER)
    assert erg.mittlere_laufzeit_s == pytest.approx((41.3 + 36.9 + 22.5) / 3)
    p, a, g = erg.mittlere_tokens
    assert (p, a, g) == pytest.approx(((3184 + 2210 + 1105) / 3, (402 + 318 + 214) / 3, (3184 + 2210 + 1105 + 402 + 318 + 214) / 3))


def test_anderes_modell_mit_fehler():
    erg = auswerten(fixtures_finden([BSP]), AUSSCHLUSS, "qwen3.6:27b")
    assert [(f.domain, f.feld) for f in erg.fehler] == [("steuerbuero-schuett.example", "rechtsform")]
    assert erg.quote("rechtsform") == pytest.approx(2 / 3)
    assert erg.mittlere_laufzeit_s == pytest.approx((12.8 + 10.4 + 6.1) / 3)


def test_ohne_aufzeichnung_nur_regelfelder():
    erg = auswerten(fixtures_finden([BSP]), AUSSCHLUSS, "gemma4:26b")
    assert erg.ohne_aufzeichnung == ["foerde-steuer.example", "hansen-steuerberatung.example", "steuerbuero-schuett.example"]
    assert "kanzleiname" not in erg.bewertet and "erster_ansprechpartner" not in erg.bewertet
    assert erg.quote("adresse") == erg.quote("telefon") == erg.quote("email") == 1.0
    assert erg.mittlere_laufzeit_s is None


def test_live_zeichnet_auf(tmp_path):
    shutil.copytree(BSP / "steuerbuero-schuett.example", tmp_path / "steuerbuero-schuett.example")
    fx = fixtures_finden([tmp_path])
    antwort = fx[0].aufzeichnung("qwen3.5:9b")

    class Fake:
        name, modell = "fake", "neues:modell"

        def extract(self, seiten):
            return antwort.model_copy(update={"modell": "neues:modell"})

    erg = auswerten(fx, AUSSCHLUSS, "neues:modell", Fake())
    assert erg.quote("kanzleiname") == 1.0
    assert (tmp_path / "steuerbuero-schuett.example" / "llm" / "neues_modell.json").exists()
    assert "neues:modell" in aufgezeichnete_modelle(fixtures_finden([tmp_path]))


def test_offene_fixtures_werden_nicht_bewertet(tmp_path):
    shutil.copytree(BSP / "foerde-steuer.example", tmp_path / "x")
    p = tmp_path / "x" / "erwartet.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "_status": "offen"}))
    assert auswerten(fixtures_finden([tmp_path]), AUSSCHLUSS, "qwen3.5:9b").fixtures == 0


@pytest.mark.parametrize(("feld", "ist", "soll", "richtig"), [
    ("kanzleiname", "Hansen + Petersen Steuerberater", "Hansen & Petersen Steuerberater", True),
    ("strasse", "Holstenstr. 42", "Holstenstraße 42", True),
    ("telefon", "+49 431 9987610", "0431 / 99 87 610", True),
    ("ort", "Glücksburg (Ostsee)", "Glücksburg", True),
    ("erster_ansprechpartner", "Dr. Henrik Lorenzen", "Henrik Lorenzen", True),
    ("email", None, None, True),
    ("email", "a@b.example", None, False),
    ("rechtsform", None, "GmbH", False),
])  # fmt: skip
def test_feldvergleich(feld, ist, soll, richtig):
    assert feld_richtig(feld, {feld: ist}, {feld: soll}) is richtig


def test_feld_ohne_sollwert_wird_nicht_bewertet():
    assert feld_richtig("email", {"email": "x"}, {}) is None


def test_fixture_add(tmp_path):
    seiten = {**beispiel_seiten("foerde-steuer.example"), "https://foerde-steuer.example/robots.txt": (404, "")}
    web = FakeWeb(seiten)
    cfg = CrawlConfig(requests_per_second_per_domain=1000.0)
    pfad, grund = fixture_holen_sync("foerde-steuer.example", cfg, tmp_path, client=web.client())
    assert grund is None
    assert sorted(p.name for p in pfad.iterdir()) == ["erwartet.json", "impressum.html", "kontakt.html", "meta.json",
                                                       "start.html", "team.html"]
    assert json.loads((pfad / "erwartet.json").read_text()) == ERWARTET_VORLAGE
    meta = json.loads((pfad / "meta.json").read_text())
    assert meta["urls"]["impressum"] == "https://foerde-steuer.example/rechtliches/impressum.html"
    # vorhandene erwartet.json wird nie überschrieben
    (pfad / "erwartet.json").write_text('{"_status": "fertig"}')
    fixture_holen_sync("foerde-steuer.example", cfg, tmp_path, client=FakeWeb(seiten).client())
    assert json.loads((pfad / "erwartet.json").read_text()) == {"_status": "fertig"}
    assert Fixture.laden(pfad).seiten["impressum"].text.startswith("Home")


def test_echt_ist_in_gitignore():
    import subprocess

    probe = ECHT / "probe.example" / "impressum.html"
    out = subprocess.run(["git", "check-ignore", "-q", str(probe)], cwd=S.basis_dir)
    assert out.returncode == 0


# ---------------------------------------------------------------- echte Fixtures (nur lokal)

ECHTE = [f for f in fixtures_finden([ECHT]) if f.fertig]


@pytest.mark.skipif(len(ECHTE) < 15, reason=f"{len(ECHTE)} von 15 echten Fixtures mit fertiger erwartet.json (nur lokal)")
@pytest.mark.parametrize("feld", ["adresse", "telefon"])
def test_echte_fixtures_regelfelder(feld):
    erg = auswerten(ECHTE, AUSSCHLUSS, None)
    assert erg.richtig.get(feld, 0) >= erg.bewertet[feld] - 2, [f for f in erg.fehler if f.feld == feld]


@pytest.mark.skipif(len(ECHTE) < 15, reason="echte Fixtures fehlen (nur lokal)")
@pytest.mark.parametrize("feld", ["kanzleiname", "erster_ansprechpartner"])
def test_echte_fixtures_llm_felder(feld):
    erg = auswerten(ECHTE, AUSSCHLUSS, S.llm.modell)
    if erg.bewertet.get(feld, 0) < 15:
        pytest.skip(f"Aufzeichnungen für {S.llm.modell} fehlen – `recherche fixture-eval --live`")
    assert erg.richtig.get(feld, 0) >= erg.bewertet[feld] - 2, [f for f in erg.fehler if f.feld == feld]
