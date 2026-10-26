"""Nachverarbeitung: Telefon, PLZ/Ort, Bundesland, Dubletten.

Bisher nur Telefon-Normalisierung; OpenPLZ-Check und Dubletten folgen.
"""

from __future__ import annotations

import re

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
