"""Ausbeute der Websuche je Einwohnerstufe – zum Kalibrieren der Einwohnerregel.

Grundlage: Lauf-Protokoll (Queries, Anfragen, Kosten je Suchort) und die Kandidaten mit ihrem ersten
Suchort (`kandidaten.suchort`), eingeordnet über die Kanzlei-Zeile: echte Kanzlei im Bundesland,
außerhalb, keine Kanzlei, nicht gelesen. „Anfragen über Seitenregel" zählt Seiten, die nur wegen
`weiter_wenn_voll` geholt wurden.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..models import Status
from ..storage import Storage
from .pipeline import Ort, Suchplan

_ZUSATZ = re.compile(r"\s\(\d{5}\)$")


@dataclass
class StufenZeile:
    stufe: str
    orte: int = 0
    queries: int = 0
    anfragen: int = 0
    ueber_regel: int = 0  # Anfragen über die Seitenregel hinaus (weiter_wenn_voll)
    kosten: float = 0.0
    kandidaten: int = 0
    kanzleien: int = 0  # Status Kanzlei, Bundesland laut PLZ = Lauf-Bundesland
    ausserhalb: int = 0
    keine_kanzlei: int = 0
    nicht_gelesen: int = 0  # ohne Kanzlei-Zeile (Nicht gefunden, offen)

    @property
    def kosten_je_kanzlei(self) -> float | None:
        return self.kosten / self.kanzleien if self.kanzleien else None

    @property
    def kanzleien_je_100_anfragen(self) -> float | None:
        return 100 * self.kanzleien / self.anfragen if self.anfragen else None

    @property
    def anteil_kanzleien(self) -> float | None:
        return self.kanzleien / self.kandidaten if self.kandidaten else None


def _stufe_name(plan: Suchplan, ort: Ort | None, name: str, grenzen: list[int] | None = None) -> str:
    if (ort and ort.ist_zusatz) or (ort is None and _ZUSATZ.search(name)):
        return "Stadtteile (PLZ-Bezirke)"
    if ort is None:
        return "nicht in der Ortsliste"
    if ort.einwohner is None:
        return "ohne Einwohnerzahl"
    unter_grenzen = sorted(grenzen) + [None] if grenzen else [s.unter for s in plan.regel]
    vorher = 0
    for unter in unter_grenzen:
        if unter is None or ort.einwohner < unter:
            return f"{vorher:,}–{unter - 1:,} Einwohner".replace(",", ".") if unter else f"ab {vorher:,} Einwohner".replace(",", ".")
        vorher = unter
    return f"ab {vorher:,} Einwohner".replace(",", ".")


def auswerten(storage: Storage, bundesland: str, orte: list[Ort], plan: Suchplan, adapter: str,
              ziel_land: str, grenzen: list[int] | None = None) -> list[StufenZeile]:
    """`grenzen` (z. B. [500, 2000, 20000]): eigene Einteilung statt der Einwohnerregel – zum Kalibrieren."""
    nach_name = {o.name: o for o in orte}
    zeilen: dict[str, StufenZeile] = {}
    orte_je_stufe: dict[str, set[str]] = defaultdict(set)

    def zeile(name: str | None) -> StufenZeile:
        ort = nach_name.get(name or "")
        stufe = _stufe_name(plan, ort, name or "", grenzen)
        return zeilen.setdefault(stufe, StufenZeile(stufe))

    for e in storage.lauf_protokoll(bundesland):
        if e.schritt != "search" or e.adapter != adapter or e.fehler:
            continue
        z = zeile(e.ort)
        orte_je_stufe[z.stufe].add(e.ort or "")
        z.queries += 1
        z.anfragen += e.anfragen
        z.kosten += e.kosten
        ort = nach_name.get(e.ort or "")
        seiten = plan.stufe(ort).seiten if ort else None
        if seiten:
            z.ueber_regel += max(0, e.anfragen - seiten)
    kanzleien = {k.kandidat_id: k for k in storage.kanzleien(bundesland) if k.kandidat_id is not None}
    for k in storage.kandidaten(bundesland):
        if k.quelle != adapter or not k.suchort:
            continue
        z = zeile(k.suchort)
        z.kandidaten += 1
        kz = kanzleien.get(k.id)
        if kz is None:
            z.nicht_gelesen += 1
        elif kz.status == Status.KEINE_KANZLEI:
            z.keine_kanzlei += 1
        elif kz.status == Status.AUSSERHALB or (kz.bundesland and kz.bundesland != ziel_land):
            z.ausserhalb += 1
        else:
            z.kanzleien += 1
    for stufe, namen in orte_je_stufe.items():
        zeilen[stufe].orte = len(namen)
    def rang(z: StufenZeile) -> tuple[int, int]:
        zahl = re.match(r"(?:ab )?([\d.]+)", z.stufe)
        return (0, int(zahl.group(1).replace(".", ""))) if zahl else (1, 0)

    return sorted(zeilen.values(), key=rang)


def als_csv(liste: list[StufenZeile], pfad: Path) -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["stufe", "orte", "queries", "anfragen", "ueber_seitenregel", "kosten", "kandidaten", "kanzleien_im_land",
                    "ausserhalb", "keine_kanzlei", "nicht_gelesen", "kosten_je_kanzlei", "kanzleien_je_100_anfragen"])
        for z in liste:
            w.writerow([z.stufe, z.orte, z.queries, z.anfragen, z.ueber_regel, round(z.kosten, 2), z.kandidaten, z.kanzleien,
                        z.ausserhalb, z.keine_kanzlei, z.nicht_gelesen,
                        round(z.kosten_je_kanzlei, 3) if z.kosten_je_kanzlei is not None else "",
                        round(z.kanzleien_je_100_anfragen, 1) if z.kanzleien_je_100_anfragen is not None else ""])
    return pfad
