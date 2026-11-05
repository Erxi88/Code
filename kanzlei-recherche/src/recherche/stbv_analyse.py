"""Analyse des amtlichen Verzeichnisses aus dem Cache (`recherche stb-verzeichnis-analyse`) – ohne neue Abfragen.

Liest die gecachten Ergebnisseiten (api_cache, Adapter „stb_verzeichnis") **nur lesend** (SQLite `mode=ro`, läuft
neben einem laufenden `stb-verzeichnis`) und berechnet die Gruppierung mit den aktuellen Regeln neu: je PLZ Einträge,
davon Gesellschaften und Personen, daraus Kanzleien (Gesellschaft / aus Firmenzeile / Person einzeln), Syndikus, und
welche Orte bzw. PLZ die Abfrage tatsächlich lieferte (Nachbarorte mit derselben PLZ, Hamburger PLZ an der Grenze).
Anlass: zu hohe Kanzleizahlen je PLZ im ersten Lauf (05.11.2026).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .search.verzeichnisse import (
    FIRMENZEILE_OHNE_KARTE,
    SOZIETAET,
    ist_einzeln,
    stbv_gruppieren,
    stbv_karten,
    stbv_syndikus,
)

QUELLE = "stb_verzeichnis"
_PLZ_IM_SCHLUESSEL = re.compile(r"plzFilter=(\d{5})")


def gecachte_seiten(db: str | Path) -> dict[str, tuple[str, str]]:
    """PLZ → (URL, HTML) aller gecachten Ergebnisseiten, schreibgeschützt gelesen."""
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        zeilen = conn.execute("SELECT schluessel, antwort FROM api_cache WHERE adapter = ?", (QUELLE,)).fetchall()
    finally:
        conn.close()
    seiten = {}
    for schluessel, antwort in zeilen:
        if m := _PLZ_IM_SCHLUESSEL.search(schluessel):
            seiten[m.group(1)] = (schluessel.split(" POST ")[0], json.loads(antwort).get("html") or "")
    return seiten


@dataclass
class PlzAnalyse:
    plz: str
    eintraege: int = 0
    gesellschaften: int = 0
    personen: int = 0
    personen_ohne_firmenzeile: int = 0
    syndikus: int = 0
    kanzleien: int = 0
    aus_gesellschaft: int = 0
    aus_firmenzeile: int = 0
    person_einzeln: int = 0
    sozietaet: int = 0  # Regel 3: mehrere Personen ohne Firmenzeile an einer Adresse ohne Kanzlei
    angezeigt: int | None = None
    gesamt: int | None = None
    orte: Counter = field(default_factory=Counter)  # „PLZ Ort" der Einträge
    liste: list = field(default_factory=list)  # gebildete Kanzleien (SearchHit)
    # Diagnose der Einzelpersonen (Rückmeldung HH 05.11.2026: 1.562 von 2.286 „Kanzleien" sind Einzelpersonen)
    einzeln_an_kanzlei_adresse: int = 0  # Person ohne Firmenzeile an einer Adresse mit Gesellschaft/Firmenzeile
    einzeln_adresse_geteilt: int = 0  # Person ohne Firmenzeile, Adresse mit weiteren solchen Personen geteilt
    syndikus_verdacht: list = field(default_factory=list)  # Firmenzeilen mit Konzern-Steuerabteilung o. Ä.

    @property
    def fremde_plz(self) -> int:
        """Einträge mit einer anderen PLZ als der abgefragten."""
        return sum(n for o, n in self.orte.items() if not o.startswith(self.plz))


def analysiere(html: str, plz: str, url: str = "") -> PlzAnalyse:
    karten, zahl = stbv_karten(html, url)
    hits = stbv_gruppieren(karten)
    a = PlzAnalyse(plz, len(karten), sum(k.gesellschaft for k in karten), sum(not k.gesellschaft for k in karten),
                   sum(1 for k in karten if not k.gesellschaft and not k.firma), len(stbv_syndikus(karten)), len(hits),
                   angezeigt=zahl[0] if zahl else None, gesamt=zahl[1] if zahl else None, liste=hits)
    a.aus_firmenzeile = sum(FIRMENZEILE_OHNE_KARTE in (h.snippet or "") for h in hits)
    a.person_einzeln = sum(ist_einzeln(h.snippet) for h in hits)
    a.sozietaet = sum(SOZIETAET in (h.snippet or "") for h in hits)
    a.aus_gesellschaft = a.kanzleien - a.aus_firmenzeile - a.person_einzeln - a.sozietaet
    a.orte = Counter(f"{k.plz or '?'} {k.ort or '?'}" for k in karten)
    adr = lambda k: (k.strasse or "", k.plz or "")  # noqa: E731
    kanzlei_adressen = {adr(k) for k in karten if k.gesellschaft or k.firma}
    ohne = [k for k in karten if not k.gesellschaft and not k.firma]
    je_adresse = Counter(adr(k) for k in ohne)
    a.einzeln_an_kanzlei_adresse = sum(1 for k in ohne if adr(k) in kanzlei_adressen)
    a.einzeln_adresse_geteilt = sum(1 for k in ohne if je_adresse[adr(k)] > 1)
    a.syndikus_verdacht = sorted({k.firma for k in karten if not k.gesellschaft and k.firma
                                  and _KONZERN_STEUER.search(k.firma)})
    return a


# Steuerbezug nur über eine Konzern-Steuerabteilung („Beiersdorf AG Steuerabteilung", „Otto GmbH & Co KG Tax")
_KONZERN_STEUER = re.compile(r"steuerabteilung|abteilung\s+steuern|konzernsteuer|\btax\s+(?:department|dept)|"
                             r"\bgroup\s+tax|\b(?:ag|se)\b.*\btax\b", re.IGNORECASE)


ANALYSE_SPALTEN = ["PLZ", "Einträge", "Gesellschaften", "Personen", "davon ohne Firmenzeile", "Syndikus",
                   "Kanzleien", "aus Gesellschaft", "aus Firmenzeile", "Person einzeln", "Sozietät/Bürogemeinschaft",
                   "einzeln an Kanzlei-Adresse", "einzeln, Adresse geteilt", "Syndikus-Verdacht", "Treffer angezeigt",
                   "Treffer gesamt", "Einträge mit anderer PLZ", "Orte der Einträge"]


def analyse_csv(analysen: list[PlzAnalyse], pfad: str | Path) -> Path:
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(ANALYSE_SPALTEN)
        for a in analysen:
            w.writerow([a.plz, a.eintraege, a.gesellschaften, a.personen, a.personen_ohne_firmenzeile, a.syndikus,
                        a.kanzleien, a.aus_gesellschaft, a.aus_firmenzeile, a.person_einzeln, a.sozietaet,
                        a.einzeln_an_kanzlei_adresse, a.einzeln_adresse_geteilt, "; ".join(a.syndikus_verdacht),
                        a.angezeigt, a.gesamt,
                        a.fremde_plz, ", ".join(f"{o} ({n})" for o, n in a.orte.most_common())])
    return pfad
