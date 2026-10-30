"""Qualitätsbericht nach Prüfkatalog Abschnitt 4 (`recherche qualitaet`, Blätter „Qualität" und „Zu prüfen").

Jede Prüfung hat einen Schweregrad: **Sperre** (Zeile darf nicht ins Hauptblatt), **Warnung** (bleibt, Abweichung
gesetzt), **Hinweis** (nur Statistik). Sperren verschieben, löschen nie:

- Q1/Q2 wirken über den Status (außerhalb Bundesland / keine Kanzlei, extract/kanzlei.py) – hier gezählt.
- Q5, Q6, Q12 und 1B Rang 4/5 → Blatt „Zu prüfen" (`zu_pruefen_grund`), nur für Zeilen mit Status „neu"
  (von Hand gepflegte Status bleiben im Hauptblatt).
- Q3 leert das Feld schon bei der Bereinigung (extract/ansprechpartner.py) – hier gezählt.

Die Prüfungen lesen nur die gespeicherten Zeilen (Felder und Hinweise in „Abweichung"), kein Netz, kein LLM.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .extract import ansprechpartner as ap
from .extract.kanzlei import (
    NAME_ABGELEITET,
    RANG_PRAEFIX,
    TEAM_UNPLAUSIBEL,
    grund_aus_abweichung,
)
from .extract.standorte import standort_spalte
from .models import Dublette, Kanzlei, Konfidenz, Status
from .normalize import normalize_telefon
from .search.dedupe import adresse_key, telefon_key

SPERRE, WARNUNG, HINWEIS = "Sperre", "Warnung", "Hinweis"

PRUEFUNGEN: dict[str, tuple[str, str]] = {
    "Q1": ("Keine deutsche 5-stellige PLZ oder Auslands-TLD", SPERRE),
    "Q2": ("Ausschlussliste 1A (keine Kanzlei)", SPERRE),
    "Q3": ("Ansprechpartner verworfen oder nur ein Wort", SPERRE),
    "Q4": ("Telefon ungültig oder Vorwahl passt nicht zur PLZ", WARNUNG),
    "Q5": ("Name abgeleitet, keine Kammer, keine Berufsbezeichnung", SPERRE),
    "Q6": ("Kanzleiname: Navigationswort, Ortsname allein oder Rolle", SPERRE),
    "Q7": ("PLZ und Ort passen laut OpenPLZ nicht", WARNUNG),
    "Q8": ("E-Mail aus der Ausschlussliste (datenschutz@ …)", WARNUNG),
    "Q9": ("Gleiches Telefon + Ansprechpartner, verschiedene Domains", HINWEIS),
    "Q10": ("Gleiche Adresse, verschiedene Kanzleinamen", HINWEIS),
    "Q11": ("Standort ohne eigenes Telefon/Ansprechpartner", HINWEIS),
    "Q12": ("Konfidenz niedrig", SPERRE),
    "Q13": ("Teamgröße > 200 oder Berufsträger > Teamgröße", WARNUNG),
    "Q14": ("Coverage-Zahl ≠ Hauptblatt-Zeilen", HINWEIS),
    "1B": ("Positivsignal nur Rang 4/5 (Kanzleiwort, Suchtreffer)", SPERRE),
}

_NAVIGATION = re.compile(r"(?<![\wäöüß])(?:start|startseite|home|kontakt|impressum|datenschutz|leistungen|aktuelles|"
                         r"news|regionen|homepages|menü|menu|navigation)(?![\wäöüß])", re.IGNORECASE)
_ROLLE = re.compile(r"^\s*(?:die|der|unser[e]?)?\s*(?:amtsdirektor|amtsvorsteher|bürgermeister|geschäftsführer|inhaber|"
                    r"vorsitzende|präsident)(?:in)?\b", re.IGNORECASE)
_EMAIL_AUS = re.compile(r"^(?:datenschutz|dsb|bewerbung|karriere|jobs|noreply|no-reply|webmaster)@", re.IGNORECASE)
_RANG = re.compile(re.escape(RANG_PRAEFIX) + r"(\d):\s*([^;]*)")


@dataclass
class Befund:
    nr: str
    kanzlei: Kanzlei | None
    text: str

    @property
    def grad(self) -> str:
        return PRUEFUNGEN[self.nr][1]

    @property
    def pruefung(self) -> str:
        return PRUEFUNGEN[self.nr][0]


@dataclass
class Bericht:
    befunde: list[Befund] = field(default_factory=list)
    zu_pruefen: dict[int, str] = field(default_factory=dict)  # id(Kanzlei) → Grund

    def anzahl(self) -> dict[str, int]:
        zaehler: dict[str, int] = defaultdict(int)
        for b in self.befunde:
            zaehler[b.nr] += 1
        return dict(zaehler)


def name_grund(k: Kanzlei) -> str | None:
    """Q6: Kanzleiname mit Navigationswort, nur ein Ortsname oder eine Rolle."""
    name = (k.kanzleiname or "").strip()
    if not name:
        return None
    if m := _NAVIGATION.search(name):
        return f"Navigationswort „{m.group(0)}“"
    if all(ap._sonderwoerter(name.split())):
        return f"nur Ortsname „{name}“"
    if _ROLLE.match(name):
        return f"Rolle „{name}“"
    return None


def zu_pruefen_grund(k: Kanzlei) -> str | None:
    """Grund für das Blatt „Zu prüfen" (Sperre Q5, Q6, Q12, 1B Rang 4/5) – nur Status „neu"."""
    if k.status != Status.NEU:
        return None
    abw = k.abweichung or ""
    rang = _RANG.search(abw)
    if NAME_ABGELEITET in abw and rang:
        return f"Q5 Name abgeleitet, keine Kammer, keine Berufsbezeichnung ({rang.group(2).strip()})"
    if grund := name_grund(k):
        return f"Q6 {grund}"
    if rang:
        return f"1B Rang {rang.group(1)}: {rang.group(2).strip()}"
    if k.konfidenz == Konfidenz.NIEDRIG:
        return "Q12 Konfidenz niedrig"
    return None


def im_hauptblatt(k: Kanzlei, weitere: set[int] | None = None) -> bool:
    return (k.status not in (Status.KEINE_KANZLEI, Status.AUSSERHALB) and zu_pruefen_grund(k) is None
            and id(k) not in (weitere or set()))


def bericht(kanzleien: Sequence[Kanzlei], dubletten: Sequence[Dublette] = (), ziel_land: str | None = None,
            coverage_gefunden: int | None = None) -> Bericht:
    from .extract.standorte import KRITERIUM, spiegel_domains

    erg = Bericht()
    kanzleien = list(kanzleien)
    weitere = {id(k) for liste in spiegel_domains(kanzleien).values() for k in liste}

    def neu(nr: str, k: Kanzlei | None, text: str) -> None:
        erg.befunde.append(Befund(nr, k, text))

    for k in kanzleien:
        abw = k.abweichung or ""
        if k.status == Status.AUSSERHALB and "Außerhalb: " in abw:
            neu("Q1", k, abw.split("Außerhalb: ", 1)[1].split(";")[0])
        if k.status == Status.KEINE_KANZLEI and (g := grund_aus_abweichung(abw)):
            neu("Q2", k, g)
        for teil in abw.split("; "):
            if teil.startswith(("Ansprechpartner verworfen", "Ansprechpartner ohne Vorname")):
                neu("Q3", k, teil)
        if k.status in (Status.KEINE_KANZLEI, Status.AUSSERHALB):
            continue
        q4 = [t for t in abw.split("; ") if t.startswith(("Telefon ungültig", "Vorwahl passt nicht"))]
        if k.telefon and not normalize_telefon(k.telefon):
            q4.append(f"Telefon ungültig: {k.telefon}")
        for t in q4:
            neu("Q4", k, t)
        if grund := zu_pruefen_grund(k):
            erg.zu_pruefen[id(k)] = grund
            nr = grund.split(" ", 1)[0] if grund.startswith("Q") else "1B"
            neu(nr, k, grund)
        if "PLZ/Ort:" in abw:
            neu("Q7", k, abw.split("PLZ/Ort:", 1)[1].split(";")[0].strip())
        if k.email and _EMAIL_AUS.match(k.email):
            neu("Q8", k, k.email)
        if TEAM_UNPLAUSIBEL in abw:
            neu("Q13", k, abw.split(TEAM_UNPLAUSIBEL, 1)[1].split(";")[0].strip(" ()") or TEAM_UNPLAUSIBEL)
    for d in dubletten:
        if d.kriterium == KRITERIUM:
            neu("Q9", None, f"{d.behalten} ↔ {d.dublette}")
    # Q10: gleiche Adresse, verschiedene Kanzleinamen (Bürogemeinschaft oder Dublette)
    haupt = [k for k in kanzleien if im_hauptblatt(k, weitere)]
    je_adresse: dict[str, list[Kanzlei]] = defaultdict(list)
    for k in haupt:
        if (a := adresse_key(k.strasse, k.plz)) is not None:
            je_adresse[a].append(k)
    for liste in je_adresse.values():
        namen = {(k.kanzleiname or "").casefold() for k in liste}
        if len(namen) > 1:
            neu("Q10", liste[0], " / ".join(sorted(f"{k.kanzleiname or '?'} ({k.domain or 'ohne Website'})" for k in liste)))
    # Q11: Standort mit Telefon und Hauptansprechpartner der Zentrale
    spalte = standort_spalte(kanzleien)
    zentrale_je_name: dict[str, Kanzlei] = {}
    for k in kanzleien:
        if (spalte.get(k.domain or f"id:{k.id}") or "").startswith("Zentrale"):
            zentrale_je_name[k.kanzleiname or k.domain or ""] = k
    for k in kanzleien:
        wert = spalte.get(k.domain or f"id:{k.id}") or ""
        if wert.startswith("Standort von "):
            z = zentrale_je_name.get(wert.removeprefix("Standort von "))
            if z and k.telefon and telefon_key(k.telefon) == telefon_key(z.telefon) and k.hauptansprechpartner and (
                    z.hauptansprechpartner and k.hauptansprechpartner.name == z.hauptansprechpartner.name):
                neu("Q11", k, f"Telefon {k.telefon} und {k.hauptansprechpartner.name} wie die Zentrale {z.domain}")
    # Q14: Coverage-Zahl gegen Hauptblatt-Zeilen mit Bundesland
    if coverage_gefunden is not None:
        im_land = sum(1 for k in haupt if not ziel_land or k.bundesland == ziel_land)
        if coverage_gefunden != im_land:
            neu("Q14", None, f"coverage {coverage_gefunden}, Hauptblatt {im_land} (Differenz {coverage_gefunden - im_land})")
    return erg


def als_csv(erg: Bericht, pfad: Path) -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["nr", "pruefung", "grad", "kanzleiname", "domain", "befund"])
        for b in erg.befunde:
            w.writerow([b.nr, b.pruefung, b.grad, b.kanzlei.kanzleiname if b.kanzlei else "",
                        b.kanzlei.domain if b.kanzlei else "", b.text])
    return pfad

