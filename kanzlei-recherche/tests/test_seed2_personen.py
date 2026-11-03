"""Stichprobe Seed 2 (03.11.2026): Ansprechpartner – Initialen im Vornamen, Organisationseinheiten statt Personen."""

from recherche.extract.ansprechpartner import bereinigen
from recherche.extract.merge import vertretung
from recherche.extract.personen import name_zerlegen
from recherche.models import Ansprechpartner


def test_initiale_gehoert_zum_vornamen():
    """kanzleidelta.de: „Jörg G. Eick" wurde zu „G. Eick"."""
    assert name_zerlegen("Ole K. Lund") == ("Ole K. Lund", None)
    assert name_zerlegen("W. Drews") == ("W. Drews", None)
    assert name_zerlegen("Ole Lund K.") is None  # Initiale nie als Nachname
    assert vertretung("Impressum\nKanzlei Delta\nInhaber: Ole K. Lund").namen == ["Ole K. Lund"]


def test_bueros_und_buchstellen_sind_keine_ansprechpartner():
    """stb-tempel.de „Büro Neumünster", hgr-heide.de „Landwirtschaftliche Buchstelle" → verworfen, nächste Person."""
    liste, hinweise = bereinigen([Ansprechpartner(name="Büro Neumünster", funktion="Steuerberater"),
                                  Ansprechpartner(name="Landwirtschaftliche Buchstelle"),
                                  Ansprechpartner(name="Ole Lund", funktion="Steuerberater, Leitung Büro Kiel")])
    assert [p.name for p in liste] == ["Ole Lund"]  # „Büro" in der Funktion verwirft nicht
    assert len(hinweise) == 2 and all(h.startswith("Ansprechpartner verworfen: ") for h in hinweise)
    assert name_zerlegen("Beratungsstelle Heide") is None and name_zerlegen("Zweigstelle Kiel") is None
