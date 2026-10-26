"""Stufe 1: regelbasierte Extraktion (Straße, PLZ, Ort, Telefon, E-Mail) aus dem Impressum.

Arbeitet zeilenweise auf dem Text aus crawl.html_zu_text(). Fallstrick: Fast jedes
Kanzlei-Impressum nennt auch die Steuerberaterkammer (Pflichtangabe), oft auch Hoster und
Webdesigner. Adressen und Telefonnummern in solchen Fremd-Abschnitten werden ignoriert
(config/impressum_ausschluss.yaml); genommen wird die erste verbleibende Angabe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..normalize import normalize_telefon
from ..search.dedupe import adresse_key
from .base import RegelErgebnis

_PLZ_ORT = re.compile(
    r"(?<![\d.,])(?:D\s*-\s*)?(\d{5})\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-/() ]*?[A-Za-zäöüß.)])"
    r"(?=\s*$|\s*[,;|·•–]|\s+(?:Tel|Fon|Fax|Telefon|E-?Mail|Mail|Deutschland|Germany|DE)\b)"
)
_STRASSE = re.compile(
    r"^([A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-' ]{1,50}?\.?\s+\d{1,4}\s*[a-zA-Z]?(?:\s*[-/]\s*\d{1,4}\s*[a-zA-Z]?)?)$"
)
# Zahlen nach diesen Kennzeichen sind keine PLZ (Registernummern, Konten, Jahresangaben …)
_KEINE_PLZ_DAVOR = re.compile(r"(?:HR\s?[AB]|PR|VR|GnR|Nr\.?|Nummer|Konto|IBAN|BLZ|seit|ab|bis|von|ca\.?)\s*:?\s*$", re.I)
_STRASSE_NEIN = re.compile(r"^(tel|fon|fax|postfach|ust|hrb|hra|amtsgericht|steuer-?nr|steuernummer|pr \d)", re.I)
_TEL_LABEL = re.compile(
    r"(?<![a-zäöü])(?:tel(?:efon)?|fon|phone|t)\s*\.?\s*:?\s*(\(?\+?\d[\d\s/()\-–.]{4,}\d)", re.I
)
_FAX_DAVOR = re.compile(r"(fax|telefax)\s*\.?\s*:?\s*$", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_AT = re.compile(r"\s*(?:\(|\[|\{)\s*(?:at|ät|@)\s*(?:\)|\]|\})\s*", re.I)
_DOT = re.compile(r"\s*(?:\(|\[|\{)\s*(?:dot|punkt)\s*(?:\)|\]|\})\s*", re.I)


@dataclass
class Ausschluss:
    abschnitt_zeilen: int = 6
    abschnitt_muster: list[re.Pattern] = field(default_factory=list)
    adressen: set[str] = field(default_factory=set)  # adresse_key
    email_nachrangig: tuple[str, ...] = ()

    @classmethod
    def laden(cls, pfad: str | Path | None) -> Ausschluss:
        if pfad is None or not Path(pfad).exists():
            return cls()
        d = yaml.safe_load(Path(pfad).read_text(encoding="utf-8")) or {}
        return cls(
            abschnitt_zeilen=int(d.get("abschnitt_zeilen", 6)),
            abschnitt_muster=[re.compile(m, re.I) for m in d.get("abschnitt_woerter", [])],
            adressen={k for a in d.get("adressen", []) if (k := adresse_key(a.get("strasse"), str(a.get("plz"))))},
            email_nachrangig=tuple(d.get("email_nachrangig", [])),
        )

    def fremd_zeilen(self, zeilen: list[str]) -> set[int]:
        """Indizes der Zeilen in Fremd-Abschnitten (Kammer, Hoster, Webdesign …)."""
        fremd: set[int] = set()
        for i, z in enumerate(zeilen):
            if any(m.search(z) for m in self.abschnitt_muster):
                fremd.update(range(i, min(len(zeilen), i + self.abschnitt_zeilen + 1)))
        return fremd


@dataclass
class Adresse:
    strasse: str | None
    plz: str
    ort: str
    zeile: int


def _ort_bereinigen(ort: str) -> str:
    ort = re.sub(r"\s+(?:OT|Ortsteil)\s.*$", "", ort.strip())
    return ort.strip(" .,-")


def adressen(zeilen: list[str]) -> list[Adresse]:
    """Alle Adressen (PLZ + Ort, mit Straße aus derselben oder der Zeile darüber)."""
    gefunden = []
    for i, z in enumerate(zeilen):
        for m in _PLZ_ORT.finditer(z):
            plz, ort = m.group(1), _ort_bereinigen(m.group(2))
            if not ort or len(ort) > 40 or _KEINE_PLZ_DAVOR.search(z[: m.start()]):
                continue
            if len(ort) < 3 or (ort.isupper() and len(ort) <= 4):  # „KI" aus „HRB 12345 KI"
                continue
            strasse = None
            davor = re.split(r"\s*[,;|·•–]\s*", z[: m.start()].strip(" ,;|·•–"))
            if davor and davor[-1] and _ist_strasse(davor[-1]):
                strasse = davor[-1]
            else:
                for j in (i - 1, i - 2):
                    if j >= 0 and _ist_strasse(zeilen[j]):
                        strasse = zeilen[j].strip()
                        break
                    if j >= 0 and zeilen[j].strip():
                        teile = re.split(r"\s*[,;|·•–]\s*", zeilen[j].strip())
                        if _ist_strasse(teile[-1]):
                            strasse = teile[-1]
                            break
            gefunden.append(Adresse(strasse, plz, ort, i))
    return gefunden


def _ist_strasse(text: str) -> bool:
    text = text.strip()
    return bool(_STRASSE.match(text)) and not _STRASSE_NEIN.match(text) and len(text) <= 60


def telefone(zeilen: list[str]) -> list[tuple[str, int]]:
    """(normalisierte Nummer, Zeile) aller als Telefon gekennzeichneten Nummern (kein Fax)."""
    ergebnis = []
    for i, z in enumerate(zeilen):
        for m in _TEL_LABEL.finditer(z):
            if _FAX_DAVOR.search(z[: m.start()]):
                continue
            label = z[m.start() : m.start(1)].lower()
            if label.strip(" .:").startswith("fax"):
                continue
            if (nummer := normalize_telefon(m.group(1))) is not None:
                ergebnis.append((nummer, i))
    return ergebnis


def entschleiern(text: str) -> str:
    return _DOT.sub(".", _AT.sub("@", text))


def emails(text: str, links: list[str] | None = None) -> list[str]:
    """E-Mail-Adressen aus mailto-Links und Text (auch „info (at) kanzlei.de"), Reihenfolge erhalten."""
    gefunden: list[str] = []
    for href in links or []:
        if href.lower().startswith("mailto:"):
            adresse = href[7:].split("?", 1)[0].strip()
            if _EMAIL.fullmatch(adresse):
                gefunden.append(adresse.lower())
    gefunden += [m.group(0).lower().rstrip(".") for m in _EMAIL.finditer(entschleiern(text))]
    return list(dict.fromkeys(gefunden))


def beste_email(kandidaten: list[str], domain: str | None, nachrangig: tuple[str, ...]) -> str | None:
    def rang(mail: str) -> tuple[int, int]:
        lokal, _, host = mail.partition("@")
        eigene = domain is not None and (host == domain or host.endswith("." + domain))
        return (0 if eigene else 1, 1 if lokal.startswith(nachrangig) else 0)

    return min(kandidaten, key=rang) if kandidaten else None


def extract_impressum(
    text: str,
    ausschluss: Ausschluss | None = None,
    links: list[str] | None = None,
    domain: str | None = None,
    weitere_texte: list[tuple[str, list[str]]] | None = None,
) -> RegelErgebnis:
    """Erste Adresse, Telefonnummer und E-Mail außerhalb von Fremd-Abschnitten.

    `weitere_texte` (z. B. Kontakt- und Startseite) werden in Reihenfolge nur für Felder
    herangezogen, die im Impressum fehlen.
    """
    ausschluss = ausschluss or Ausschluss()
    erg = RegelErgebnis()
    mails: list[str] = []
    for txt, lnks in [(text, links or []), *(weitere_texte or [])]:
        zeilen = txt.splitlines()
        fremd = ausschluss.fremd_zeilen(zeilen)
        if erg.plz is None:
            for a in adressen(zeilen):
                if a.zeile in fremd or adresse_key(a.strasse, a.plz) in ausschluss.adressen:
                    continue
                erg.strasse, erg.plz, erg.ort = a.strasse, a.plz, a.ort
                break
        if erg.telefon is None:
            erg.telefon = next((nr for nr, i in telefone(zeilen) if i not in fremd), None)
            if erg.telefon is None:
                erg.telefon = next(
                    (n for h in lnks if h.lower().startswith("tel:") and (n := normalize_telefon(h[4:]))), None
                )
        if not mails:
            fremd_text = "\n".join(z for i, z in enumerate(zeilen) if i not in fremd)
            mails = emails(fremd_text, [h for h in lnks if h.lower().startswith("mailto:")])
    erg.email = beste_email(mails, domain, ausschluss.email_nachrangig)
    return erg


_RECHTSFORMEN = [
    (r"\bPartG\s*mbB\b|Partnerschaftsgesellschaft\s+mbB", "PartG mbB"),
    (r"\bPartG\b|Partnerschaftsgesellschaft\b|\bPartnerschaft\b", "PartG"),
    (r"\bGmbH\s*&\s*Co\.?\s*KG\b", "GmbH & Co. KG"),
    (r"\bmbH\b|\bGmbH\b|Gesellschaft mit beschränkter Haftung", "GmbH"),
    (r"\bAG\b|Aktiengesellschaft", "AG"),
    (r"\bGbR\b|Gesellschaft bürgerlichen Rechts", "GbR"),
    (r"\bUG\b", "UG (haftungsbeschränkt)"),
    (r"\bKG\b", "KG"),
]


def rechtsform_aus_name(name: str | None) -> str | None:
    """Rechtsform, wenn sie im Kanzleinamen steht (Rückfall, wenn das LLM keine liefert)."""
    if not name:
        return None
    for muster, form in _RECHTSFORMEN:
        if re.search(muster, name):
            return form
    return None
