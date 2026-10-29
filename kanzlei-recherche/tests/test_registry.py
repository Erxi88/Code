import pytest

from recherche.search.base import OrtsAdapter, QueryAdapter
from recherche.search.brave import BraveAdapter
from recherche.search.ddgs import DdgsAdapter
from recherche.search.registry import (
    ADAPTER_KLASSEN,
    aktive_adapter,
    build_adapter,
    stufe_a_adapter,
)


def test_stufe_a_standard_ist_brave(settings):
    a = stufe_a_adapter(settings)
    assert isinstance(a, BraveAdapter)
    assert (a.cfg.per_page, a.cfg.country, a.cfg.search_lang, a.cfg.kosten_je_1000) == (20, "DE", "de", 5.0)
    assert a.max_anfragen == 1000


def test_stufe_a_umschaltbar(settings):
    s = settings.model_copy(deep=True)
    s.discovery.stufe_a.adapter = "ddgs"
    assert isinstance(stufe_a_adapter(s), DdgsAdapter)
    s.discovery.stufe_a.adapter = "places"
    with pytest.raises(ValueError, match="kein Websuche-Adapter"):
        stufe_a_adapter(s)


def test_unbekannter_adapter(settings):
    with pytest.raises(ValueError, match="Unbekannter Such-Adapter"):
        build_adapter("bing", settings)


def test_alle_quellen_konfiguriert(settings):
    assert list(ADAPTER_KLASSEN) == settings.discovery.quellen.namen()
    for name in ADAPTER_KLASSEN:
        a = build_adapter(name, settings)
        assert a.name == name
        erwartete_stufe = "A" if name in ("brave", "google_cse", "serpapi", "ddgs") else "B"
        assert a.stufe == erwartete_stufe
        assert isinstance(a, QueryAdapter if erwartete_stufe == "A" else OrtsAdapter)


def test_aktive_adapter_aus_settings(settings):
    assert [a.name for a in aktive_adapter(settings)] == ["brave", "ddgs"]
    assert [a.name for a in aktive_adapter(settings, stufe="B")] == []
    s = settings.model_copy(deep=True)
    s.discovery.quellen.places.aktiv = True
    s.discovery.quellen.ddgs.aktiv = False
    assert [a.name for a in aktive_adapter(s)] == ["brave", "places"]


def test_stb_verzeichnis_standardmaessig_aus(settings):
    assert settings.discovery.quellen.stb_verzeichnis.aktiv is False


@pytest.mark.parametrize(("name", "key"), [
    ("brave", "BRAVE_API_KEY"), ("google_cse", "GOOGLE_CSE_API_KEY"), ("serpapi", "SERPAPI_KEY"),
    ("places", "GOOGLE_PLACES_API_KEY"),
])  # fmt: skip
def test_fehlende_keys(settings, name, key):
    assert key in build_adapter(name, settings).nicht_konfiguriert_grund()


@pytest.mark.parametrize("name", ["serpapi", "gelbeseiten", "dasoertliche", "elf880", "stb_verzeichnis"])
def test_platzhalter(settings, monkeypatch, name):
    for key in ("SERPAPI_KEY", "GOOGLE_PLACES_API_KEY"):
        monkeypatch.setenv(key, "k")
    a = build_adapter(name, settings)
    assert "Platzhalter" in a.nicht_konfiguriert_grund()
    with pytest.raises(NotImplementedError):
        a.search("q", 10) if isinstance(a, QueryAdapter) else a.search_ort(None)


def test_ddgs_ohne_key_bereit(settings):
    assert build_adapter("ddgs", settings).ist_konfiguriert


def test_places_stillgelegt(settings, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    a = build_adapter("places", settings)
    assert not a.aktiv and "Platzhalter" in a.nicht_konfiguriert_grund()
