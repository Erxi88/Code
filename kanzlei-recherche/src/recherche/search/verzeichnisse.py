"""Stufe B: Branchenverzeichnisse und amtliches Steuerberaterverzeichnis – Platzhalter.

Jeder Adapter bekommt einen Ort (search_ort) und liefert alle Einträge der Rubrik
„Steuerberater" für diesen Ort im gemeinsamen Ergebnisformat (Name, Straße, PLZ, Ort,
Telefon, Website, Quelle, Quell-ID = Eintrags-ID des Verzeichnisses). Stark gedrosselt,
Antworten werden gecacht. Nutzungsbedingungen und robots.txt vor dem Bau prüfen.

Das amtliche Steuerberaterverzeichnis ist standardmäßig AUS, läuft PLZ für PLZ und sehr
langsam; es ergänzt nur, was in den anderen Quellen fehlt (ergibt sich aus der
Dubletten-Logik: bekannte Kanzleien bekommen nur einen weiteren Fund).
"""

from __future__ import annotations

from ..config import VerzeichnisConfig
from .base import Cache, OrtsPlatzhalter


class _Verzeichnis(OrtsPlatzhalter):
    def __init__(self, cfg: VerzeichnisConfig | None = None, cache: Cache | None = None, **_):
        super().__init__(cfg or VerzeichnisConfig(), cache)


class GelbeSeitenAdapter(_Verzeichnis):
    name = "gelbeseiten"


class DasOertlicheAdapter(_Verzeichnis):
    name = "dasoertliche"


class Elf880Adapter(_Verzeichnis):
    name = "elf880"


class StbVerzeichnisAdapter(_Verzeichnis):
    """Amtliches Steuerberaterverzeichnis: Suche je PLZ des Ortes."""

    name = "stb_verzeichnis"
