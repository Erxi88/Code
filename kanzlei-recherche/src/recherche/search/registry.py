"""Auswahl der Such-Adapter anhand von settings.yaml."""

from __future__ import annotations

from collections.abc import Callable

from ..config import Settings
from .base import SearchAdapter
from .brave import BraveAdapter
from .ddgs import DdgsAdapter
from .google import GoogleCseAdapter

ADAPTER_FABRIKEN: dict[str, Callable[[Settings], SearchAdapter]] = {
    "ddgs": lambda s: DdgsAdapter(s.search.ddgs),
    "google": lambda s: GoogleCseAdapter(s.search.google),
    "brave": lambda s: BraveAdapter(s.search.brave),
}


def build_adapter(name: str, settings: Settings) -> SearchAdapter:
    try:
        fabrik = ADAPTER_FABRIKEN[name]
    except KeyError:
        bekannt = ", ".join(sorted(ADAPTER_FABRIKEN))
        raise ValueError(f"Unbekannter Such-Adapter {name!r} (bekannt: {bekannt})") from None
    return fabrik(settings)


def aktiver_adapter(settings: Settings) -> SearchAdapter:
    return build_adapter(settings.search.adapter, settings)


def vergleichs_adapter(settings: Settings) -> list[SearchAdapter]:
    return [build_adapter(n, settings) for n in settings.search.compare_adapters]
