import pytest

from recherche.search.brave import BraveAdapter
from recherche.search.ddgs import DdgsAdapter
from recherche.search.google import GoogleCseAdapter
from recherche.search.registry import aktiver_adapter, build_adapter, vergleichs_adapter


def test_standard_ist_ddgs(settings):
    a = aktiver_adapter(settings)
    assert isinstance(a, DdgsAdapter)
    assert a.cfg.backend == "duckduckgo"


def test_adapter_per_config_waehlbar(settings):
    s = settings.model_copy(deep=True)
    s.search.adapter = "brave"
    assert isinstance(aktiver_adapter(s), BraveAdapter)


def test_unbekannter_adapter(settings):
    with pytest.raises(ValueError, match="Unbekannter Such-Adapter"):
        build_adapter("bing", settings)


def test_vergleichs_adapter(settings):
    assert [a.name for a in vergleichs_adapter(settings)] == ["ddgs", "google", "brave"]


def test_google_ohne_key_nicht_konfiguriert(settings):
    g = build_adapter("google", settings)
    assert not g.ist_konfiguriert
    assert "GOOGLE_CSE_API_KEY" in g.nicht_konfiguriert_grund()
    assert "GOOGLE_CSE_ID" in g.nicht_konfiguriert_grund()


def test_google_mit_key_noch_platzhalter(settings, monkeypatch):
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")
    g = build_adapter("google", settings)
    assert "noch nicht implementiert" in g.nicht_konfiguriert_grund()
    with pytest.raises(NotImplementedError):
        g.search("Steuerberater Kiel", 10)


def test_google_paging_bis_30(settings):
    g = GoogleCseAdapter(settings.search.google)
    assert g.seiten_starts(30) == [1, 11, 21]
    assert g.seiten_starts(100) == [1, 11, 21]
    assert g.seiten_starts(10) == [1]


def test_brave_ohne_key(settings, monkeypatch):
    b = build_adapter("brave", settings)
    assert "BRAVE_API_KEY" in b.nicht_konfiguriert_grund()
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    assert "noch nicht implementiert" in build_adapter("brave", settings).nicht_konfiguriert_grund()
    assert settings.search.brave.per_page == 20
