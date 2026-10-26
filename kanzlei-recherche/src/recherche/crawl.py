"""Crawling der Kandidaten-Domains – Platzhalter bis zur Crawl-Sitzung.

Vorgesehen: Startseite laden, Links sammeln, Impressum/Kontakt/Team über Linktext
und Pfad erkennen; max. 1 Request/s je Domain, viele Domains parallel (httpx async),
Timeout 15 s, 2 Retries, robots.txt beachten; jede Rohseite einmal in SQLite cachen.
"""

from __future__ import annotations

from .models import Seitentyp

# Erkennungsmuster für Unterseiten (Pfad-Fragmente und Linktexte, kleingeschrieben)
SEITEN_MUSTER: dict[Seitentyp, dict[str, tuple[str, ...]]] = {
    Seitentyp.IMPRESSUM: {"pfad": ("/impressum", "/imprint"), "text": ("impressum",)},
    Seitentyp.KONTAKT: {"pfad": ("/kontakt", "/contact"), "text": ("kontakt", "anfahrt")},
    Seitentyp.TEAM: {
        "pfad": ("/team", "/ueber-uns", "/uber-uns", "/kanzlei", "/mitarbeiter"),
        "text": ("unser team", "team", "mitarbeiter", "über uns", "die kanzlei"),
    },
}
