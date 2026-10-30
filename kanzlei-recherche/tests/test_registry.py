import pytest

from recherche.config import VerzeichnisConfig
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
    assert a.max_anfragen == settings.discovery.max_anfragen_je_lauf  # Budget je Lauf aus settings.yaml


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
    s = settings.model_copy(deep=True)
    for name in ("brave", "ddgs", "google_cse", "serpapi", "places", "gelbeseiten", "dasoertliche", "elf880", "stb_verzeichnis"):
        getattr(s.discovery.quellen, name).aktiv = name in ("brave", "ddgs", "stb_verzeichnis")
    assert [a.name for a in aktive_adapter(s)] == ["brave", "ddgs", "stb_verzeichnis"]
    assert [a.name for a in aktive_adapter(s, stufe="B")] == ["stb_verzeichnis"]
    s.discovery.quellen.stb_verzeichnis.aktiv = False
    s.discovery.quellen.places.aktiv = True
    s.discovery.quellen.ddgs.aktiv = False
    assert [a.name for a in aktive_adapter(s)] == ["brave", "places"]


def test_stb_verzeichnis_ohne_settings_aus():
    from recherche.config import QuellenConfig

    q = QuellenConfig()  # Code-Standard; eingeschaltet wird in settings.yaml
    assert q.stb_verzeichnis.aktiv is False and not q.stb_verzeichnis.nur_luecken and q.gelbeseiten.nur_ergaenzen


@pytest.mark.parametrize(("name", "key"), [
    ("brave", "BRAVE_API_KEY"), ("google_cse", "GOOGLE_CSE_API_KEY"), ("serpapi", "SERPAPI_KEY"),
    ("places", "GOOGLE_PLACES_API_KEY"),
])  # fmt: skip
def test_fehlende_keys(settings, name, key):
    assert key in build_adapter(name, settings).nicht_konfiguriert_grund()


@pytest.mark.parametrize("name", ["serpapi"])
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


@pytest.mark.parametrize("name", ["gelbeseiten", "dasoertliche", "elf880", "stb_verzeichnis"])
def test_verzeichnisse_konfiguriert(settings, name):
    a = build_adapter(name, settings)
    assert a.nicht_konfiguriert_grund() is None  # url_vorlage aus den Fixture-Seiten
    assert "url_vorlage fehlt" in type(a)(VerzeichnisConfig()).nicht_konfiguriert_grund()
    q = settings.discovery.quellen.get(name)
    assert q.nur_ergaenzen == (name != "stb_verzeichnis")  # Branchenverzeichnisse legen nie neue Kandidaten an
    assert q.nur_luecken == (name != "stb_verzeichnis")  # nur die Grundliste darf --alle
    assert settings.discovery.quellen.stb_verzeichnis.je_plz and settings.discovery.quellen.stb_verzeichnis.pause_s == 30
