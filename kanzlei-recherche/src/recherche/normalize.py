"""Nachverarbeitung: Telefon, PLZ/Ort, Bundesland, Dubletten.

Telefon ins +49-Format, PLZ/Ort-Prüfung gegen OpenPLZ (mit Bundesland).
Die Dubletten-Logik liegt in search/dedupe.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import httpx
import yaml

_TRENNER = re.compile(r"[\s/\-–.()]+")


def normalize_telefon(roh: str | None) -> str | None:
    """Deutsche Rufnummer → '+49 <Vorwahl> <Rufnummer>' (Vorwahl ohne führende 0).

    Die Vorwahl kommt aus dem Vorwahlverzeichnis von `phonenumbers`, wenn die Nummer dort gültig ist
    ('0 41 53 / 5 97 40' → '+49 4153 59740', nicht '+49 41 5359740'). Laut Verzeichnis ungültige Nummern →
    None (Prüfkatalog 2, „Telefon ungültig"). Nur ohne das Paket: Vorwahl an der ersten Trennstelle der Eingabe
    ('0431 / 12 34 56' → '+49 431 123456').
    Unplausible Eingaben → None.
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
    # gültige deutsche Rufnummer: Ortsnetz 2–9, Mobil 15/16/17, Service 18 – sonst Artefakt
    # (Zahlenkolonnen, SVG-Pfade: „+49 140625…")
    if not (ziffern[0] in "23456789" or ziffern[:2] in ("15", "16", "17", "18")):
        return None
    if getrennt := _vorwahl_trennen(ziffern, nur_gueltig=True):
        return getrennt  # Vorwahlverzeichnis vor der Schreibweise der Seite („0 41 53 / 5 97 40")
    if _phonenumbers_da():
        return None  # Prüfkatalog 2: nur Nummern mit is_valid_number – kein Rätselraten
    if len(gruppen) >= 2 and len(gruppen[0]) >= 2:
        return f"{land} {gruppen[0]} {''.join(gruppen[1:])}"
    if getrennt := _vorwahl_trennen(ziffern):
        return getrennt
    return f"{land}{ziffern}"


@lru_cache(maxsize=1)
def _phonenumbers_da() -> bool:
    try:
        import phonenumbers  # noqa: F401
    except ImportError:
        return False
    return True


def ist_service(nummer: str | None) -> bool:
    """0800/0180x/0137/0900 – Service- und Sonderrufnummern (nach Festnetz und Mobil)."""
    return bool(nummer) and bool(re.match(r"^\+49 ?(?:800|180|137|900|700)", nummer))


def _vorwahl_trennen(ziffern: str, nur_gueltig: bool = False) -> str | None:
    """Nationale Rufnummer ohne 0 → '+49 <Vorwahl> <Rufnummer>' über das Vorwahlverzeichnis (phonenumbers,
    INTERNATIONAL). `nur_gueltig`: nur Nummern, die laut Verzeichnis gültig sind (sonst zählt die Schreibweise)."""
    try:
        import phonenumbers
    except ImportError:  # optional – ohne Paket bleibt die Nummer ungetrennt
        return None
    try:
        nummer = phonenumbers.parse("+49" + ziffern)
    except phonenumbers.NumberParseException:
        return None
    if not (phonenumbers.is_valid_number(nummer) if nur_gueltig else phonenumbers.is_possible_number(nummer)):
        return None
    teile = phonenumbers.format_number(nummer, phonenumbers.PhoneNumberFormat.INTERNATIONAL).split()
    if len(teile) < 3 or teile[0] != "+49":
        return None
    return f"+49 {teile[1]} {''.join(teile[2:])}"


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
    ort_kanonisch: str | None = None  # „Hamburg" statt „Hamburg-Rissen", wenn OpenPLZ nur „Hamburg" führt


# Stadtteilzusatz: „Hamburg-Rissen", „Kiel - Holtenau", „Kiel OT Holtenau", „Norderstedt/Garstedt"
_STADTTEIL_ZUSATZ = re.compile(r"\s*(?:\s-\s|–|—|-|/|\bOT\b|\bOrtsteil\b)\s*")


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
        if any(_ortsname(n) == soll or _ortsname(n).startswith(soll + " ") for n in namen):
            # Schreibweise laut OpenPLZ („AHRENSBURG" → „Ahrensburg"), wenn der Ortsname genau passt
            genau = next((o["name"] for o in orte if o.get("name") and _ortsname(o["name"]) == soll), None)
            return PlzPruefung(True, bundesland, namen, ort_kanonisch=genau if genau and genau != ort.strip() else None)
        # „Kiel-Holtenau"/„Hamburg Rissen" bei OpenPLZ-Ort „Kiel"/„Hamburg": verifiziert, kanonisch ausgeben
        for o in orte:
            if o.get("name") and soll.startswith(_ortsname(o["name"]) + " "):
                return PlzPruefung(True, bundesland, namen, ort_kanonisch=o["name"])
        # „X-Y" → „X", wenn OpenPLZ zur PLZ den Ort „X" führt (aber nicht „X-Y" – sonst passte es schon)
        teile = _STADTTEIL_ZUSATZ.split(ort.strip(), maxsplit=1)
        if len(teile) == 2 and teile[0]:
            kern = _ortsname(teile[0])
            for o in orte:
                if o.get("name") and _ortsname(o["name"]) == kern:
                    return PlzPruefung(True, bundesland, namen, ort_kanonisch=o["name"])
        # „Wyk/Insel Föhr" → „Wyk auf Föhr": genau ein OpenPLZ-Ort zur PLZ mit demselben ersten Wort
        erstes = soll.split()[0] if soll else ""
        gleich = {o["name"] for o in orte if o.get("name") and _ortsname(o["name"]).split()[:1] == [erstes]}
        if len(erstes) >= 3 and len(gleich) == 1:
            return PlzPruefung(True, bundesland, namen, ort_kanonisch=gleich.pop())
        return PlzPruefung(False, bundesland, namen, f"Ort {ort!r} passt nicht zu PLZ {plz}")


# ---------------------------------------------------------------------- Rechtsform

_RECHTSFORMEN_DATEI = Path(__file__).resolve().parents[2] / "config" / "rechtsformen.yaml"


@lru_cache(maxsize=4)
def _rechtsformen(pfad: str = str(_RECHTSFORMEN_DATEI)) -> tuple[tuple[str, tuple[re.Pattern, ...]], ...]:
    eintraege = yaml.safe_load(Path(pfad).read_text(encoding="utf-8-sig")) or []
    return tuple((e["form"], tuple(re.compile(v, re.I) for v in e.get("varianten", []))) for e in eintraege)


def normalize_rechtsform(text: str | None) -> str | None:
    """Kanonische Rechtsform („Partnerschaftsgesellschaft mbB" → „PartG mbB"); unbekannte Angaben bleiben."""
    if not text or not text.strip():
        return None
    for form, muster in _rechtsformen():
        if any(m.search(text) for m in muster):
            return form
    return text.strip()


def rechtsform_aus_text(text: str | None) -> str | None:
    """Wie normalize_rechtsform, aber None, wenn keine bekannte Rechtsform vorkommt (z. B. im Kanzleinamen)."""
    if not text:
        return None
    for form, muster in _rechtsformen():
        if any(m.search(text) for m in muster):
            return form
    return None
