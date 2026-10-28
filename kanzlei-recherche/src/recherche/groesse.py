"""Größenklassen der Teamgröße – Grenzen aus settings.yaml (`extract.groessenklassen`).

Die Grenzen sind die Obergrenzen der Klassen: [1, 5, 10, 20, 50, 100] ergibt
„1", „2-5", „6-10", „11-20", „21-50", „51-100", „>100". Zuordnung nur aus einer Zahl
oder einer Untergrenze im Text („über 20" → 21 → „21-50"); keine Schätzung.

Gespeichert wird die Klasse als Text. Klassen einer früheren Einteilung werden beim Laden
aus der Zahl neu eingeordnet (`neu_einordnen`); ohne Zahl über die Text-Basis, sonst leer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

STANDARD_GRENZEN = (1, 5, 10, 20, 50, 100)


class Groessenklassen:
    def __init__(self, grenzen: Sequence[int] = STANDARD_GRENZEN):
        grenzen = sorted({int(g) for g in grenzen if int(g) >= 1})
        if not grenzen:
            raise ValueError("extract.groessenklassen: mindestens eine Grenze ≥ 1")
        self.grenzen = tuple(grenzen)
        self.namen: list[str] = []
        unten = 1
        for g in self.grenzen:
            self.namen.append(str(g) if g == unten else f"{unten}-{g}")
            unten = g + 1
        self.namen.append(f">{self.grenzen[-1]}")

    def aus_zahl(self, n: int) -> str:
        for name, g in zip(self.namen, self.grenzen, strict=False):
            if n <= g:
                return name
        return self.namen[-1]

    def index(self, klasse: str) -> int:
        return self.namen.index(klasse)

    def gilt(self, klasse: str | None) -> bool:
        return klasse in self.namen

    def bereich(self, klasse: str) -> tuple[int, int | None] | None:
        """„6-10" → (6, 10), „>100" → (101, None), „1" → (1, 1) – für beliebige Einteilungen."""
        if m := re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", klasse or ""):
            return int(m.group(1)), int(m.group(2))
        if m := re.fullmatch(r"\s*>\s*(\d+)\s*", klasse or ""):
            return int(m.group(1)) + 1, None
        if m := re.fullmatch(r"\s*(\d+)\s*", klasse or ""):
            return int(m.group(1)), int(m.group(1))
        return None

    def umrechnen(self, klasse: str | None) -> str | None:
        """Klasse einer anderen Einteilung → aktuelle Klasse, wenn der Bereich in genau eine passt."""
        if klasse is None or self.gilt(klasse):
            return klasse
        if (b := self.bereich(klasse)) is None:
            return None
        unten, oben = b
        ziel = self.aus_zahl(unten)
        if oben is None:
            return ziel if ziel == self.namen[-1] else None
        return ziel if self.aus_zahl(oben) == ziel else None


_aktiv = Groessenklassen()


def aktiv() -> Groessenklassen:
    return _aktiv


def setze_grenzen(grenzen: Sequence[int]) -> Groessenklassen:
    global _aktiv
    _aktiv = Groessenklassen(grenzen)
    return _aktiv


def neu_einordnen(zahl: int | None, klasse: str | None, basis: str | None) -> tuple[str | None, str | None]:
    """(Klasse, Hinweis) für eine gespeicherte Zeile nach der aktuellen Einteilung.

    Zahl vorhanden → Klasse aus der Zahl. Sonst eine Klasse der aktuellen Einteilung behalten;
    eine alte Klasse über die Text-Basis neu auswerten („Text: „über 35 Mitarbeiter"") oder,
    wenn der Bereich eindeutig in eine neue Klasse fällt, umrechnen; sonst leer mit Hinweis.
    """
    k = aktiv()
    if zahl is not None:
        return k.aus_zahl(zahl), None
    if klasse is None or k.gilt(klasse):
        return klasse, None
    if basis and (m := re.search(r"Text:\s*[„\"](.+?)[“\"]", basis)):
        from .extract.personen import mitarbeiterzahl

        if (angabe := mitarbeiterzahl([m.group(1)])) and (angabe.klasse or angabe.zahl):
            return angabe.klasse or k.aus_zahl(angabe.zahl), None
    if neu := k.umrechnen(klasse):
        return neu, None
    return None, f"Größenklasse „{klasse}“ (alte Einteilung) nicht umrechenbar"
