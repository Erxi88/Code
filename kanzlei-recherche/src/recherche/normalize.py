"""Nachverarbeitung: Telefon, PLZ/Ort, Bundesland, Dubletten.

Telefon ins +49-Format, PLZ/Ort-Prüfung gegen OpenPLZ (mit Bundesland).
Die Dubletten-Logik liegt in search/dedupe.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

_TRENNER = re.compile(r"[\s/\-–.()]+")


def normalize_telefon(roh: str | None) -> str | None:
    """Deutsche Rufnummer → '+49 <Vorwahl> <Rufnummer>' (Vorwahl ohne führende 0).

    Die Vorwahl wird an der ersten Trennstelle der Eingabe erkannt, z. B.
    '0431 / 12 34 56' → '+49 431 123456'. Ohne erkennbare Trennung bleibt die
    Nummer ungetrennt ('+49431123456'). Unplausible Eingaben → None.
    """
    if not roh:
        return None
    text = roh.strip()
    text = re.sub(r"^(tel\.?|telefon|fon|phone)\s*:?\s*", "", text, flags=re.I)
    text = text.replace("(0)", " ").lstrip("(")
    if not re.fullmatch(r"[+\d\s/\-–.()]+", text):
        return None
    if text.startswith("+"):
        land, rest = "+", text[1:]
    elif text.startswith("00"):
        land, rest = "+", text[2:]
    elif text.startswith("0"):
        land, rest = "+49", text[1:]
    else:
        return None
    gruppen = [g for g in _TRENNER.split(rest) if g]
    if land == "+":
        if not gruppen or not gruppen[0].startswith("49"):
            return None  # nur deutsche Nummern
        gruppen[0] = gruppen[0][2:]
        gruppen = [g for g in gruppen if g]
        land = "+49"
    ziffern = "".join(gruppen)
    if not 6 <= len(ziffern) <= 13:
        return None
    if len(gruppen) >= 2:
        return f"{land} {gruppen[0]} {''.join(gruppen[1:])}"
    return f"{land}{ziffern}"


# ---------------------------------------------------------------------- PLZ/Ort (OpenPLZ)

_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _ortsname(name: str) -> str:
    """'Kiel, Landeshauptstadt' / 'Glücksburg (Ostsee)' / 'Lauenburg/Elbe' → vergleichbare Form."""
    name = name.split(",", 1)[0]
    name = re.sub(r"\(.*?\)", "", name)
    name = name.lower().translate(_UMLAUTE)
    name = re.sub(r"\b(st\.|sankt)\s*", "sankt ", name)
    return " ".join(re.sub(r"[^a-z ]", " ", name).split())


@dataclass
class PlzPruefung:
    """Ergebnis der PLZ/Ort-Prüfung. verifiziert=None: nicht prüfbar (OpenPLZ nicht erreichbar)."""

    verifiziert: bool | None
    bundesland: str | None = None
    orte: list[str] = field(default_factory=list)  # laut OpenPLZ zur PLZ gehörende Orte
    hinweis: str | None = None


class PlzPruefer:
    """Prüft PLZ und Ort gegen die OpenPLZ-API und liefert das Bundesland (eine Anfrage je PLZ, gecacht)."""

    name = "openplz"

    def __init__(self, url: str = "https://openplzapi.org", cache=None, client: httpx.Client | None = None,
                 timeout_s: float = 10.0):
        self.url = url.rstrip("/")
        self.cache = cache
        self._client = client
        self.timeout_s = timeout_s
        self.anfragen = 0
        self._speicher: dict[str, list[dict] | None] = {}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)
        return self._client

    def _orte_zur_plz(self, plz: str) -> list[dict] | None:
        if plz in self._speicher:
            return self._speicher[plz]
        daten = self.cache.cache_get(self.name, plz) if self.cache is not None else None
        if daten is None:
            self.anfragen += 1
            try:
                resp = self.client.get(f"{self.url}/de/Localities", params={"postalCode": plz, "pageSize": 50})
                resp.raise_for_status()
                daten = {"orte": resp.json()}
            except (httpx.HTTPError, ValueError):
                self._speicher[plz] = None
                return None
            if self.cache is not None:
                self.cache.cache_put(self.name, plz, daten)
        self._speicher[plz] = daten["orte"]
        return daten["orte"]

    def pruefe(self, plz: str | None, ort: str | None) -> PlzPruefung:
        if not plz or not re.fullmatch(r"\d{5}", plz):
            return PlzPruefung(False if plz else None, hinweis="PLZ ungültig" if plz else None)
        orte = self._orte_zur_plz(plz)
        if orte is None:
            return PlzPruefung(None, hinweis="OpenPLZ nicht erreichbar")
        if not orte:
            return PlzPruefung(False, hinweis=f"PLZ {plz} unbekannt")
        namen = []
        for o in orte:
            for n in (o.get("name"), (o.get("municipality") or {}).get("name")):
                if n and n not in namen:
                    namen.append(n)
        bundesland = (orte[0].get("federalState") or {}).get("name")
        if not ort:
            return PlzPruefung(None, bundesland, namen, "Ort fehlt")
        soll = _ortsname(ort)
        passt = any(_ortsname(n) == soll or _ortsname(n).startswith(soll + " ") or soll.startswith(_ortsname(n) + " ")
                    for n in namen)
        return PlzPruefung(passt, bundesland, namen, None if passt else f"Ort {ort!r} passt nicht zu PLZ {plz}")
