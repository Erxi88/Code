"""Auswahl der Discovery-Adapter anhand von settings.yaml (discovery.quellen.<name>)."""

from __future__ import annotations

from ..config import Settings
from .base import Cache, OrtsAdapter, QueryAdapter, SearchAdapter
from .brave import BraveAdapter
from .ddgs import DdgsAdapter
from .google import GoogleCseAdapter
from .places import PlacesAdapter
from .serpapi import SerpApiAdapter
from .verzeichnisse import (
    DasOertlicheAdapter,
    Elf880Adapter,
    GelbeSeitenAdapter,
    StbVerzeichnisAdapter,
)

ADAPTER_KLASSEN: dict[str, type[SearchAdapter]] = {
    # Stufe A – Websuche
    "brave": BraveAdapter,
    "google_cse": GoogleCseAdapter,
    "serpapi": SerpApiAdapter,
    "ddgs": DdgsAdapter,
    # Stufe B – Lückenfüller (in dieser Reihenfolge)
    "places": PlacesAdapter,
    "gelbeseiten": GelbeSeitenAdapter,
    "dasoertliche": DasOertlicheAdapter,
    "elf880": Elf880Adapter,
    "stb_verzeichnis": StbVerzeichnisAdapter,
}


def build_adapter(name: str, settings: Settings, cache: Cache | None = None) -> SearchAdapter:
    try:
        klasse = ADAPTER_KLASSEN[name]
    except KeyError:
        bekannt = ", ".join(ADAPTER_KLASSEN)
        raise ValueError(f"Unbekannter Such-Adapter {name!r} (bekannt: {bekannt})") from None
    adapter = klasse(settings.discovery.quellen.get(name), cache=cache)
    adapter.max_anfragen = settings.discovery.max_anfragen_je_lauf
    return adapter


def stufe_a_adapter(settings: Settings, cache: Cache | None = None) -> QueryAdapter:
    adapter = build_adapter(settings.discovery.stufe_a.adapter, settings, cache)
    if not isinstance(adapter, QueryAdapter):
        raise ValueError(f"{adapter.name} ist kein Websuche-Adapter und kann nicht Stufe A tragen")
    return adapter


def aktive_adapter(
    settings: Settings, cache: Cache | None = None, stufe: str | None = None
) -> list[SearchAdapter]:
    """Alle in settings.yaml aktivierten Adapter (optional nur einer Stufe), in Registry-Reihenfolge."""
    result = []
    for name in ADAPTER_KLASSEN:
        if not settings.discovery.quellen.get(name).aktiv:
            continue
        adapter = build_adapter(name, settings, cache)
        if stufe is None or adapter.stufe == stufe:
            result.append(adapter)
    return result


def ist_orts_adapter(adapter: SearchAdapter) -> bool:
    return isinstance(adapter, OrtsAdapter)
