"""`config/nachbarkreise.yaml` aus Wikidata (P47 „grenzt an", SPARQL-Ergebnis als XML) und GV-ISys erzeugen.

Wikidata liefert Paare von Kreisschlüsseln (P440, Variablen ak/bk), auch für aufgelöste Altkreise. Es bleiben nur
Schlüssel, die im GV-ISys als Kreis (Satzart 40) stehen; die Namen kommen aus dem GV-ISys im Format der Ortsliste
(„Kreis Dithmarschen", „Kreisfreie Stadt Kiel"). Jedes Paar steht bei beiden Kreisen (symmetrisch), auch über
Landesgrenzen (Herzogtum Lauenburg ↔ Ludwigslust-Parchim). „immer_zulaessig" (SH: 040) bleibt aus der alten Datei.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import LAND_SCHLUESSEL

_NS = {"s": "http://www.w3.org/2005/sparql-results#"}
_KOPF = """\
# Nachbarkreise je Bundesland für die Vorwahl-PLZ-Prüfung (Q4, src/recherche/vorwahl.py).
# Erzeugt mit `recherche nachbarkreise` aus Wikidata (P47, Kreisschlüssel P440) und GV-ISys (nur aktuelle Kreise,
# Namen aus GV-ISys). Ortsnetze enden nicht an Kreisgrenzen (Kronshagen → 0431 Kiel); eine Vorwahl aus einem
# Nachbarkreis passt. Symmetrisch, auch über Landesgrenzen. immer_zulaessig: Vorwahlen ohne Prüfung
# (Hamburg 040 für SH, Rücksprache 30.10.2026). SH gegen die geprüfte Handliste vom 30.10.2026 abgeglichen.
"""


def wikidata_paare(pfad: str | Path) -> set[frozenset[str]]:
    """Kreisschlüssel-Paare (ak, bk) aus dem SPARQL-XML; Paare eines Kreises mit sich selbst entfallen."""
    paare: set[frozenset[str]] = set()
    for r in ET.parse(pfad).getroot().iterfind(".//s:result", _NS):
        b = {x.get("name"): (x.findtext("s:literal", default="", namespaces=_NS) or "").strip()
             for x in r.iterfind("s:binding", _NS)}
        a, c = b.get("ak", ""), b.get("bk", "")
        if a and c and a != c:
            paare.add(frozenset((a, c)))
    return paare


@dataclass
class Ergebnis:
    daten: dict = field(default_factory=dict)  # Bundesland → {immer_zulaessig, nachbarn}
    altkreise: set[str] = field(default_factory=set)  # Schlüssel aus Wikidata, die im GV-ISys fehlen
    ohne_paar: dict[str, list[str]] = field(default_factory=dict)  # Bundesland → Kreise ohne Nachbarn
    abweichungen: dict[str, tuple[set, set]] = field(default_factory=dict)  # Land → (nur neu, nur alt)

    def als_yaml(self) -> str:
        return _KOPF + yaml.safe_dump(self.daten, allow_unicode=True, sort_keys=False, width=120)


def erzeugen(paare: set[frozenset[str]], kreise: dict[str, str], bisher: dict | None = None) -> Ergebnis:
    """`kreise`: aktueller Kreisschlüssel → Name (GV-ISys, alle Länder). `bisher`: alte YAML (Abgleich, Ausnahmen)."""
    bisher = bisher or {}
    erg = Ergebnis()
    nachbarn: dict[str, set[str]] = defaultdict(set)
    for p in paare:
        if not all(k in kreise for k in p):
            erg.altkreise |= {k for k in p if k not in kreise}
            continue
        a, b = sorted(p)
        nachbarn[a].add(b)
        nachbarn[b].add(a)
    for land, nr in LAND_SCHLUESSEL.items():
        eigene = sorted(k for k in kreise if k.startswith(nr))
        if not eigene:
            continue
        eintrag: dict = {}
        if ausnahmen := (bisher.get(land) or {}).get("immer_zulaessig"):
            eintrag["immer_zulaessig"] = ausnahmen
        eintrag["nachbarn"] = {kreise[k]: sorted(kreise[n] for n in nachbarn[k]) for k in eigene if nachbarn[k]}
        erg.daten[land] = eintrag
        if ohne := [kreise[k] for k in eigene if not nachbarn[k]]:
            erg.ohne_paar[land] = ohne
        if alt := (bisher.get(land) or {}).get("nachbarn"):
            def menge(liste: dict) -> set[frozenset[str]]:
                return {frozenset((a, b)) for a, ns in liste.items() for b in ns or []}

            neu_m, alt_m = menge(eintrag["nachbarn"]), menge(alt)
            erg.abweichungen[land] = (neu_m - alt_m, alt_m - neu_m)
    return erg
