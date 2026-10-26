"""Stufe 1: regelbasierte Extraktion (PLZ, Ort, Straße, Telefon, E-Mail) aus dem Impressum.

Platzhalter – die Regex-Regeln folgen in der Extraktions-Sitzung.
"""

from __future__ import annotations

from .base import RegelErgebnis


def extract_impressum(text: str) -> RegelErgebnis:
    raise NotImplementedError("Regelbasierte Extraktion folgt in der Extraktions-Sitzung")
