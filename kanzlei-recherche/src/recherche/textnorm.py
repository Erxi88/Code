"""Unsichtbare und Sonder-Leerzeichen entfernen – vor allen Regeln und vor dem Prompt.

Manche Seiten setzen durchgängig bedingte Trennstriche („Steu&shy;er&shy;be&shy;ra&shy;tung"):
dann trifft kein regulärer Ausdruck mehr, und auch das LLM liest nur Bruchstücke.
"""

from __future__ import annotations

import re
import unicodedata

# U+00AD bedingter Trennstrich, U+200B–U+200D Nullbreite, U+2060 Wortverbinder, U+FEFF BOM
_UNSICHTBAR = re.compile("[­​‌‍⁠﻿]")
# geschützte und schmale Leerzeichen → normales Leerzeichen
_LEERZEICHEN = re.compile("[  -   　]")
_PRUEFEN = re.compile("[ ­ -‍  ⁠　﻿]")


def text_normalisieren(text: str | None) -> str | None:
    """NFC, ohne unsichtbare Zeichen, Sonder-Leerzeichen als normale Leerzeichen."""
    if not text:
        return text
    if _PRUEFEN.search(text):
        text = _LEERZEICHEN.sub(" ", _UNSICHTBAR.sub("", text))
    return text if text.isascii() else unicodedata.normalize("NFC", text)


def braucht_normalisierung(text: str) -> bool:
    return bool(_PRUEFEN.search(text)) or (not text.isascii() and not unicodedata.is_normalized("NFC", text))
