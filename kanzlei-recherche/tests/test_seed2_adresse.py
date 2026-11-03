"""Stichprobe Seed 2 (03.11.2026): Hausnummern-Spannen und Straßen mit Straßennummer."""

from recherche.extract.rules import adressen


def test_hausnummer_spanne_mit_gedankenstrich():
    """kvin-steuerberater.de: „Sandkamp 9 – 11"."""
    assert [(a.strasse, a.plz) for a in adressen(["Sandkamp 9 – 11", "24103 Kiel"])] == [("Sandkamp 9-11", "24103")]
    assert adressen(["Am Markt 3 – 5, 24103 Kiel"])[0].strasse == "Am Markt 3-5"
    assert adressen(["Markt 1 – 24103 Kiel"])[0].strasse == "Markt 1"  # Trenner zur PLZ bleibt Trenner


def test_strasse_mit_strassennummer():
    """steuer-mt.de: „An der B76 Nr. 1a"."""
    assert adressen(["An der B76 Nr. 1a", "24340 Eckernförde"])[0].strasse == "An der B76 Nr. 1a"
    assert adressen(["HRB 13705", "24103 Kiel"])[0].strasse is None
