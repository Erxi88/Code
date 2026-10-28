"""Größenklassen aus settings.yaml: Zuordnung aus Zahl oder Untergrenze, Umrechnen alter Klassen."""

import pytest

from recherche import groesse
from recherche.extract.base import LlmErgebnis, llm_schema
from recherche.groesse import Groessenklassen, neu_einordnen


def test_namen_aus_grenzen():
    assert Groessenklassen().namen == ["1", "2-5", "6-10", "11-20", "21-50", "51-100", ">100"]
    assert Groessenklassen([5, 15, 50]).namen == ["1-5", "6-15", "16-50", ">50"]  # frühere Einteilung


def test_einstellung_aus_settings(settings):
    assert settings.extract.groessenklassen == [1, 5, 10, 20, 50, 100]
    assert groesse.aktiv().namen[-1] == ">100"


@pytest.mark.parametrize("alt, neu", [
    ("1-5", None), ("6-15", None), ("16-50", None), (">50", None),  # Bereich über mehrere neue Klassen
    ("21-50", "21-50"), ("1", "1"), ("3-4", "2-5"), (">200", ">100"), ("unsinn", None),
])  # fmt: skip
def test_umrechnen(alt, neu):
    assert Groessenklassen().umrechnen(alt) == neu


@pytest.mark.parametrize("zahl, klasse, basis, erwartet", [
    (18, "16-50", "18 Personen auf Teamseite", ("11-20", None)),
    (None, "16-50", "Text: „über 35 Mitarbeiter“", ("21-50", None)),
    (None, "16-50", 'Text: "über 20 Mitarbeiterinnen und Mitarbeiter"', ("21-50", None)),
    (None, "6-15", "Einschätzung aus dem Text", (None, "Größenklasse „6-15“ (alte Einteilung) nicht umrechenbar")),
    (None, "11-20", "Einschätzung aus dem Text", ("11-20", None)),
    (None, None, None, (None, None)),
])  # fmt: skip
def test_neu_einordnen(zahl, klasse, basis, erwartet):
    assert neu_einordnen(zahl, klasse, basis) == erwartet


def test_llm_schema_und_alte_aufzeichnungen():
    enum = llm_schema()["properties"]["teamgroesse_klasse"]["anyOf"][0]["enum"]
    assert enum == ["1", "2-5", "6-10", "11-20", "21-50", "51-100", ">100"]
    assert LlmErgebnis(teamgroesse_klasse="21-50").teamgroesse_klasse == "21-50"
    assert LlmErgebnis(teamgroesse_klasse="16-50").teamgroesse_klasse is None  # alte Einteilung, nicht eindeutig


def test_andere_grenzen_gelten_ueberall():
    try:
        groesse.setze_grenzen([5, 15, 50])
        from recherche.models import Kanzlei

        assert Kanzlei(lauf_bundesland="SH", teamgroesse=7).teamgroesse_klasse == "6-15"
    finally:
        groesse.setze_grenzen(groesse.STANDARD_GRENZEN)
