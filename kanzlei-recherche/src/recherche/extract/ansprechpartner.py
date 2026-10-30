"""Ansprechpartner-Bereinigung nach Prüfkatalog Abschnitt 2 („Ansprechpartner").

Läuft nach der Zusammenführung (merge.ansprechpartner) und in `kanzlei-pruefung` auf gespeicherten Zeilen:

- Präfixe aus Bild-Texten abschneiden („Porträt von", „Porträtfoto von", „Foto von", „Bild von", „Portrait",
  „Bild:", „Abbildung"); Suffixe „Steuerberater", „StB" (→ Funktion) und „Dipl.-Kfm." (→ Titel).
- Verwerfen, wenn Name, Titel oder Funktion ein Wort der Verwerf-Liste enthält (Datenschutz, Grundverordnung,
  Amtsdirektor, Amtsvorsteher, Bürgermeister, Präsident, Archiv, Regionen, Homepages, Anträge, Browser, Verein)
  oder „Fachanwalt … <Ort>". Orts-, Länder-, Monatsnamen und Wochentage verwerfen nur als **Zusatzwort** neben
  Vor- und Nachname („Fachanwalt Elmshorn", „Kiel Holtenau") – „Oliver Hagen" und „August Petersen" bleiben
  (Rücksprache 30.10.2026).
- Pflichtform Vorname + Nachname (mindestens zwei Wörter, das erste kein Titel); nur Nachname
  („Steuerberaterin Schröder") → verworfen, Nachname in „Abweichung".
- Rechtsanwälte und Notare nur vorn, wenn kein Steuerberater benannt ist.

Jede verworfene Person steht als „Ansprechpartner verworfen: …" in „Abweichung" (Q3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..models import Ansprechpartner

_PRAEFIX = re.compile(r"^\s*(?:porträtfoto\s+von|porträt\s+von|portrait\s+von|portraitfoto\s+von|foto\s+von|bild\s+von|"
                      r"portrait|porträt|bild\s*:|abbildung\s*:?)\s*", re.IGNORECASE)
_SUFFIX_FUNKTION = re.compile(r"[,\s]+(steuerberater(?:in)?|stb)\.?\s*$", re.IGNORECASE)
_SUFFIX_TITEL = re.compile(r"[,\s]+(dipl\.\s*-?\s*kf(?:m|fr)\.?)\s*$", re.IGNORECASE)
_VERWERFEN = re.compile(r"datenschutz|grundverordnung|amtsdirektor|amtsvorsteher|bürgermeister|präsident|archiv|regionen|"
                        r"homepages|anträge|browser|verein", re.IGNORECASE)
_TITEL_WORT = re.compile(r"^(?:dr|prof|dipl|mag|ll|m\.?a|m\.?sc|b\.?a|b\.?sc|herr|frau|stb|wp|ra|rain)\b\.?", re.IGNORECASE)
_PARTIKEL = {"von", "van", "de", "der", "zu", "vom", "ten", "ter", "da", "di"}
_RA_NOTAR = re.compile(r"rechtsanw|notar|anwält|anwalt", re.IGNORECASE)
_STB = re.compile(r"steuerberat|\bstb\b|steuerbevollm|wirtschaftspr[üu]f|\bwp\b|buchprüf|\bvbp\b", re.IGNORECASE)

LAENDER = {"deutschland", "österreich", "schweiz", "dänemark", "niederlande", "belgien", "frankreich", "polen",
           "tschechien", "luxemburg", "liechtenstein", "italien", "spanien", "schweden", "norwegen", "germany",
           "austria", "switzerland", "denmark"}
MONATE = {"januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober", "november",
          "dezember", "jänner"}
WOCHENTAGE = {"montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag", "sonnabend"}

_ortsnamen: set[str] = {"kiel", "lübeck", "flensburg", "neumünster", "elmshorn", "norderstedt", "hamburg", "berlin",
                        "bremen", "hannover", "münchen", "köln", "frankfurt", "stuttgart", "düsseldorf"}


def setze_ortsnamen(namen: Iterable[str]) -> None:
    """Ortsnamen des Laufs (Ortsliste des Bundeslandes, Großstädte) für die Zusatzwort-Regel."""
    global _ortsnamen
    _ortsnamen = {n.casefold() for n in namen if n} | _ortsnamen


def _sonderwort(w: str, nachbarn: tuple[str, ...] = ()) -> bool:
    """Orts-, Länder-, Wochentagsname – Monatsname nur mit einer Zahl daneben („Mai 2024", „3. Mai"); „Hanh Mai
    Luong" ist ein Name (Rücksprache 30.10.2026)."""
    w = w.casefold().strip(".,;:()")
    if w in MONATE:
        return any(re.match(r"^\d", n) for n in nachbarn)
    return w in _ortsnamen or w in LAENDER or w in WOCHENTAGE


def _sonderwoerter(woerter: list[str]) -> list[bool]:
    return [_sonderwort(w, tuple(woerter[j] for j in (i - 1, i + 1) if 0 <= j < len(woerter)))
            for i, w in enumerate(woerter)]


def _woerter(name: str) -> list[str]:
    return [w for w in re.split(r"\s+", name.strip()) if w]


def verwerf_grund(p: Ansprechpartner) -> str | None:
    """Grund, aus dem der Eintrag kein Ansprechpartner ist – None, wenn er bleibt."""
    text = " ".join(x for x in (p.name, p.titel, p.funktion) if x)
    if m := _VERWERFEN.search(text):
        return m.group(0)
    if re.search(r"fachanwalt", text, re.I) and any(_sonderwoerter(_woerter(text))):
        return "Fachanwalt mit Ortsname"
    woerter = [w for w in _woerter(p.name) if w.casefold() not in _PARTIKEL]
    sonder = _sonderwoerter(woerter)
    if woerter and all(sonder):
        return "Orts-/Länder-/Monatsname statt Name"
    if len(woerter) >= 3 and (zusatz := [w for w, ja in zip(woerter[1:], sonder[1:], strict=True) if ja]):
        # Zusatzwort neben Vor- und Nachname („Jan Lund Kiel"); „Oliver Hagen" (zwei Wörter) bleibt
        return f"Zusatzwort „{zusatz[0]}“"
    if len(woerter) < 2 or _TITEL_WORT.match(woerter[0]):
        return "ohne Vorname"
    return None


def bereinigen_eins(p: Ansprechpartner) -> Ansprechpartner:
    name, titel, funktion = _PRAEFIX.sub("", p.name).strip(), p.titel, p.funktion
    while True:
        if m := _SUFFIX_FUNKTION.search(name):
            funktion = ", ".join(x for x in (funktion, m.group(1)) if x) if not (funktion and m.group(1).lower() in
                                                                                    funktion.lower()) else funktion
            name = name[: m.start()].strip()
        elif m := _SUFFIX_TITEL.search(name):
            titel = " ".join(x for x in (titel, m.group(1)) if x)
            name = name[: m.start()].strip()
        else:
            break
    return Ansprechpartner(name=name.strip(" ,;:-–"), titel=titel, funktion=funktion)


def bereinigen(personen: list[Ansprechpartner]) -> tuple[list[Ansprechpartner], list[str]]:
    """(bereinigte Liste, Hinweise für „Abweichung")."""
    bleiben, hinweise = [], []
    for roh in personen:
        p = bereinigen_eins(roh)
        if grund := verwerf_grund(p):
            anzeige = roh.anzeige()
            hinweise.append(f"Ansprechpartner verworfen: {anzeige} ({grund})" if grund != "ohne Vorname"
                            else f"Ansprechpartner ohne Vorname: {anzeige}")
            continue
        bleiben.append(p)
    # Rechtsanwälte und Notare nur, wenn kein Steuerberater benannt ist: sonst hinter die Steuerberater
    def nur_ra(p: Ansprechpartner) -> bool:
        text = f"{p.titel or ''} {p.funktion or ''}"
        return bool(_RA_NOTAR.search(text)) and not _STB.search(text)

    if any(not nur_ra(p) and _STB.search(f"{p.titel or ''} {p.funktion or ''}") for p in bleiben):
        bleiben = [p for p in bleiben if not nur_ra(p)] + [p for p in bleiben if nur_ra(p)]
    return bleiben, hinweise
