"""Dubletten-Erkennung über alle Quellen hinweg.

Viele Treffer (Places, Branchenverzeichnisse) kommen ohne Website. Deshalb wird
nicht nur über die Domain, sondern auch über normalisierte Telefonnummer, Adresse
und Namens-Token abgeglichen:

1. Domain gleich                                  → zusammenführen (sicher)
2. Telefon gleich                                 → zusammenführen (sicher)
3. Adresse gleich und Name passt (oder fehlt)     → zusammenführen (sicher)
   Adresse gleich, Name verschieden               → NICHT zusammenführen, nur Verdacht
                                                    (Bürogemeinschaft, Ärztehaus …)
4. PLZ gleich und Namens-Token ähnlich            → zusammenführen, aber „unsicher"
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..models import DublettenArt
from ..normalize import normalize_telefon

# Wörter, die in fast jedem Kanzleinamen vorkommen und nichts unterscheiden
NAME_STOPWORTE = frozenset(
    """
    steuerberater steuerberaterin steuerberaterinnen steuerberatung steuerberatungen
    steuerberatungsgesellschaft steuerberatungsgesellschaften steuerkanzlei steuerbuero
    steuerburo steuer steuern kanzlei kanzleien buero stb stbg stbin partg partgmbb mbb mbh
    gmbh co kg ag gbr ug ohg ev partnerschaft partnerschaftsgesellschaft partner partnerin
    kollegen kollegin und the der die das den dem des in im am an fuer von vom zu zur zum
    mit ihr ihre ihren team home startseite impressum kontakt willkommen dipl kfm kffr
    oec bw fh dr rer pol prof rechtsanwalt rechtsanwaelte rechtsanwaeltin wirtschaftspruefer
    wirtschaftsprueferin wp vbp und www de com example
    """.split()
)

_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
# Straßenname, Hausnummer mit Buchstabe, optional Bereich ("42-44") am Ende
_STRASSE_NR = re.compile(r"^(.*?)(\d+)\s*([a-z])?(?:\s*[-/–]\s*\d+\s*[a-z]?)?$")


def _basis(text: str) -> str:
    return text.lower().translate(_UMLAUTE)


def name_tokens(name: str | None, ort: str | None = None) -> frozenset[str]:
    """Unterscheidende Wörter eines Namens; Stopwörter und der eigene Ortsname entfallen."""
    if not name:
        return frozenset()
    ort_tokens = set(re.split(r"[^a-z0-9]+", _basis(ort))) if ort else set()
    tokens = re.split(r"[^a-z0-9]+", _basis(name))
    return frozenset(
        t for t in tokens if len(t) >= 2 and not t.isdigit() and t not in NAME_STOPWORTE and t not in ort_tokens
    )


def telefon_key(telefon: str | None) -> str | None:
    norm = normalize_telefon(telefon)
    return re.sub(r"\D", "", norm) if norm else None


def adresse_key(strasse: str | None, plz: str | None) -> str | None:
    """'Holstenstraße 42', '24103' → '24103|holstenstr|42'. None ohne Straße, Nummer oder PLZ."""
    if not strasse or not plz:
        return None
    m = _STRASSE_NR.match(_basis(strasse).strip())
    if m is None:
        return None
    name, nummer = m.group(1), m.group(2) + (m.group(3) or "")
    name = re.sub(r"(strasse|str\.?)(?=\W|$)", "str", name)
    name = re.sub(r"[^a-z]", "", name)
    if not name:
        return None
    return f"{plz.strip()}|{name}|{nummer}"


@dataclass(frozen=True)
class Schluessel:
    domain: str | None = None
    telefon: str | None = None
    adresse: str | None = None
    plz: str | None = None
    tokens: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def aus_feldern(
        cls,
        domain: str | None,
        telefon: str | None,
        strasse: str | None,
        plz: str | None,
        name: str | None,
        ort: str | None,
    ) -> Schluessel:
        plz = plz.strip() if plz else None
        return cls(
            domain=domain,
            telefon=telefon_key(telefon),
            adresse=adresse_key(strasse, plz),
            plz=plz or None,
            tokens=name_tokens(name, ort),
        )


def namen_aehnlich(a: frozenset[str], b: frozenset[str]) -> bool:
    """Mindestens die Hälfte der Token des kürzeren Namens kommt im anderen vor."""
    if not a or not b:
        return False
    gemeinsam = a & b
    return bool(gemeinsam) and len(gemeinsam) / min(len(a), len(b)) >= 0.5


@dataclass
class Abgleich:
    id: int | None = None  # gefundener Kandidat
    kriterium: str | None = None
    art: DublettenArt | None = None
    verdacht: list[int] = field(default_factory=list)  # gleiche Adresse, anderer Name


class Index:
    """In-Memory-Index aller bekannten Kandidaten eines Laufs."""

    def __init__(self) -> None:
        self.eintraege: dict[int, Schluessel] = {}
        self._domain: dict[str, int] = {}
        self._telefon: dict[str, int] = {}
        self._adresse: dict[str, list[int]] = defaultdict(list)
        self._plz: dict[str, list[int]] = defaultdict(list)

    def __len__(self) -> int:
        return len(self.eintraege)

    def setze(self, kid: int, s: Schluessel) -> None:
        """Fügt einen Kandidaten hinzu oder ersetzt seine Schlüssel."""
        alt = self.eintraege.get(kid)
        if alt is not None:
            if alt.adresse and kid in self._adresse[alt.adresse]:
                self._adresse[alt.adresse].remove(kid)
            if alt.plz and kid in self._plz[alt.plz]:
                self._plz[alt.plz].remove(kid)
        self.eintraege[kid] = s
        if s.domain:
            self._domain.setdefault(s.domain, kid)
        if s.telefon:
            self._telefon.setdefault(s.telefon, kid)
        if s.adresse:
            self._adresse[s.adresse].append(kid)
        if s.plz:
            self._plz[s.plz].append(kid)

    def finde(self, s: Schluessel) -> Abgleich:
        if s.domain and s.domain in self._domain:
            return Abgleich(self._domain[s.domain], "domain", DublettenArt.ZUSAMMENGEFUEHRT)
        if s.telefon and s.telefon in self._telefon:
            return Abgleich(self._telefon[s.telefon], "telefon", DublettenArt.ZUSAMMENGEFUEHRT)
        verdacht: list[int] = []
        if s.adresse:
            for kid in self._adresse.get(s.adresse, []):
                anderer = self.eintraege[kid].tokens
                if not s.tokens or not anderer or s.tokens & anderer:
                    return Abgleich(kid, "adresse", DublettenArt.ZUSAMMENGEFUEHRT)
                verdacht.append(kid)
        if s.plz and s.tokens:
            for kid in self._plz.get(s.plz, []):
                if kid in verdacht:
                    continue
                if namen_aehnlich(s.tokens, self.eintraege[kid].tokens):
                    return Abgleich(kid, "plz+name", DublettenArt.UNSICHER, verdacht)
        return Abgleich(verdacht=verdacht)


def zusammenfuehren(s: Schluessel, neu: Schluessel) -> Schluessel:
    """Ergänzt fehlende Schlüssel; vorhandene werden nie überschrieben."""
    return Schluessel(
        domain=s.domain or neu.domain,
        telefon=s.telefon or neu.telefon,
        adresse=s.adresse or neu.adresse,
        plz=s.plz or neu.plz,
        tokens=s.tokens or neu.tokens,
    )
