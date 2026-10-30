"""Q4 Vorwahl ↔ PLZ (Prüfkatalog 2, „Telefon"): passt die Ortsnetzkennzahl zur Region der Kanzlei?

Grundlage: BNetzA-Vorwahlverzeichnis (`config/vorwahlen/*.csv`, Ortsnetzkennzahl; Ortsnetzname; KennzeichenAktiv)
und die Ortsliste des Bundeslandes (Gemeinde → Kreis, GV-ISys). Kette: Vorwahl → Ortsnetzname → Gemeinde → Kreis.

Rücksprache 30.10.2026 (Ortsnetze enden nicht an Kreisgrenzen – Kronshagen telefoniert mit 0431 Kiel):
- passt, wenn der Kreis des Ortsnetzes gleich dem Kreis der PLZ oder ein **Nachbarkreis** ist
  (`config/nachbarkreise.yaml`, symmetrisch gelesen); Ausnahmen je Bundesland (SH: 040 Hamburg),
- Ortsnetzname ohne Gemeinde-Treffer im Bundesland (Westerland ↔ Sylt, Ortsnetz eines anderen Landes): Warnung
  nur, wenn die Vorwahlzone (erste Ziffer) nicht zu den Zonen des Bundeslandes passt – 02357 bei Kiel ja,
  04141 Stade bei Kiel nein (nicht entscheidbar),
- Mobil-, Service- und Sondernummern werden nicht geprüft.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VORWAHL_FALSCH = "Vorwahl passt nicht zur PLZ"
_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_ZUSATZ = re.compile(r"\s+(?:kr|kreis|bz|b|bei)\.?\s+(.+)$", re.IGNORECASE)
# Landschaftszusätze im Ortsnetznamen („Husum Nordsee", „Horst Holstein"): nur dann zählt das erste Wort allein
_LANDSCHAFT = {"nordsee", "ostsee", "holstein", "holst", "elbe", "niederelbe", "dithmarschen", "dithm", "probstei",
               "nordfriesland", "angeln", "schwansen", "eider", "stormarn", "lauenburg", "lauenb", "westholstein"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.casefold().translate(_UMLAUTE))


@dataclass
class VorwahlPruefer:
    ortsnetze: dict[str, str]  # Vorwahl ohne 0 → Ortsnetzname
    kreis_je_gemeinde: dict[str, list[str]]  # normierter Gemeindename → Kreise
    kreise_je_plz: dict[str, set[str]]  # PLZ → Kreise der Gemeinden mit dieser PLZ
    nachbarn: dict[str, set[str]] = field(default_factory=dict)
    ausnahmen: set[str] = field(default_factory=set)  # Vorwahlen, die immer passen (SH: 40)
    zonen: set[str] = field(default_factory=set)  # erste Vorwahlziffern der Ortsnetze im Bundesland

    @classmethod
    def laden(cls, onb: str | Path, orte: Iterable, nachbarkreise: str | Path | None = None,
              bundesland: str | None = None) -> VorwahlPruefer:
        ortsnetze = {}
        with open(onb, encoding="utf-8-sig", newline="") as f:
            for z in csv.DictReader(f, delimiter=";"):
                if str(z.get("KennzeichenAktiv", "1")).strip() == "1":
                    ortsnetze[z["Ortsnetzkennzahl"].strip()] = z["Ortsnetzname"].strip()
        kreis_je_gemeinde: dict[str, list[str]] = defaultdict(list)
        kreise_je_plz: dict[str, set[str]] = defaultdict(set)
        for o in orte:
            if getattr(o, "ist_zusatz", False) or not o.kreis:
                continue
            for name in {o.name.split(" (")[0], o.name.replace("(", " ").replace(")", " ")}:
                if o.kreis not in kreis_je_gemeinde[_norm(name)]:
                    kreis_je_gemeinde[_norm(name)].append(o.kreis)
            for p in o.alle_plz:
                kreise_je_plz[p].add(o.kreis)
        nachbarn: dict[str, set[str]] = defaultdict(set)
        ausnahmen: set[str] = set()
        if nachbarkreise and Path(nachbarkreise).exists():
            daten = yaml.safe_load(Path(nachbarkreise).read_text(encoding="utf-8-sig")) or {}
            land = daten.get(bundesland or "", {}) if bundesland else {}
            for kreis, liste in (land.get("nachbarn") or {}).items():
                for n in liste or []:
                    nachbarn[kreis].add(n)
                    nachbarn[n].add(kreis)
            ausnahmen = {str(v).lstrip("0") for v in land.get("immer_zulaessig") or []}
        p = cls(ortsnetze, dict(kreis_je_gemeinde), dict(kreise_je_plz), dict(nachbarn), ausnahmen)
        # Vorwahlzonen des Bundeslandes: erste Ziffern, unter denen viele Ortsnetze einer Gemeinde des Landes
        # entsprechen (gleichnamige Orte in anderen Ländern – Neuenkirchen – zählen so nicht)
        je_zone: dict[str, int] = defaultdict(int)
        for v in ortsnetze:
            if p._namens_kreise(v):
                je_zone[v[0]] += 1
        spitze = max(je_zone.values(), default=0)
        p.zonen = {z for z, n in je_zone.items() if n >= 0.2 * spitze}
        return p

    def _namens_kreise(self, vorwahl: str) -> list[str]:
        name = self.ortsnetze.get(vorwahl)
        if not name:
            return []
        hinweis = None
        if m := _ZUSATZ.search(name):  # „Quickborn Kr Pinneberg", „Aumühle b Hamburg"
            hinweis, name = m.group(1), name[: m.start()]
        woerter = name.split()
        kandidaten = [name]
        if len(woerter) > 1 and all(_norm(w) in _LANDSCHAFT for w in woerter[1:]):
            kandidaten.append(woerter[0])  # „Husum Nordsee" → Husum, nicht „Burg auf Fehmarn" → Burg
        for kandidat in kandidaten:
            kreise = self.kreis_je_gemeinde.get(_norm(kandidat), [])
            if hinweis and len(kreise) > 1:
                kreise = [k for k in kreise if _norm(hinweis) in _norm(k)] or kreise
            if kreise:
                return kreise
        return []

    def kreise_des_ortsnetzes(self, vorwahl: str) -> list[str]:
        """Kreise der Gemeinde, nach der das Ortsnetz heißt – nur in den Vorwahlzonen des Bundeslandes."""
        if self.zonen and vorwahl[:1] not in self.zonen:
            return []
        return self._namens_kreise(vorwahl)

    def pruefen(self, telefon: str | None, plz: str | None) -> str | None:
        """Hinweis „Vorwahl passt nicht zur PLZ: …" – None, wenn sie passt oder nicht entscheidbar ist."""
        if not telefon or not plz or not (m := re.match(r"^\+49 (\d+) ", telefon)):
            return None
        vorwahl = m.group(1)
        if vorwahl.startswith(("15", "16", "17", "18", "19", "700", "800", "900", "137")) or vorwahl in self.ausnahmen:
            return None
        plz_kreise = self.kreise_je_plz.get(plz)
        if not plz_kreise:
            return None  # PLZ nicht im Bundesland – darüber entscheidet der Bundesland-Filter
        netz = self.ortsnetze.get(vorwahl, "unbekanntes Ortsnetz")
        netz_kreise = self.kreise_des_ortsnetzes(vorwahl)
        if not netz_kreise:
            if self.zonen and vorwahl[0] not in self.zonen:
                return f"{VORWAHL_FALSCH}: 0{vorwahl} ({netz}) – PLZ {plz} ({', '.join(sorted(plz_kreise))})"
            return None
        erlaubt = set(plz_kreise).union(*(self.nachbarn.get(k, set()) for k in plz_kreise))
        if erlaubt.isdisjoint(netz_kreise):
            return (f"{VORWAHL_FALSCH}: 0{vorwahl} ({netz}, {', '.join(netz_kreise)}) – PLZ {plz} "
                    f"({', '.join(sorted(plz_kreise))})")
        return None


_aktiv: VorwahlPruefer | None = None


def setze_pruefer(p: VorwahlPruefer | None) -> None:
    global _aktiv
    _aktiv = p


def pruefer() -> VorwahlPruefer | None:
    return _aktiv
