"""Standorte einer Kanzlei und Dubletten-Verdacht über Telefon + Ansprechpartner.

SHBB & Co.: Niederlassungen als Subdomains (buedelsdorf.shbb.de, …) und eigene Domains
(ihre-steuerberater.com) mit derselben Zentralnummer und demselben Ansprechpartner. Sie bleiben
eigene Zeilen (verschiedene Adressen), werden aber gruppiert:

- gleiche Stammdomain (Subdomains; Baukasten-Hoster je Subdomain) → eine Gruppe,
- gleiches Telefon **und** gleicher Hauptansprechpartner → eine Gruppe und Dubletten-Verdacht
  („telefon+ansprechpartner"), auch bei verschiedener Adresse.

Zentrale ist die Zeile auf der Stammdomain selbst (oder www.), sonst die mit den meisten
Ansprechpartnern. Die Spalte „Zentrale/Standort" entsteht beim Export, ohne Schema-Eingriff.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..models import Dublette, DublettenArt, Kanzlei
from ..search.dedupe import telefon_key
from .kanzlei import stammdomain
from .merge import gleiche_person

KRITERIUM = "telefon+ansprechpartner"


def _schluessel(k: Kanzlei) -> str:
    return k.domain or f"id:{k.id}"


def gruppen(kanzleien: list[Kanzlei]) -> tuple[list[list[Kanzlei]], list[tuple[Kanzlei, Kanzlei]]]:
    """(Gruppen mit mehr als einer Zeile, Paare mit gleichem Telefon und Ansprechpartner)."""
    eltern: dict[str, str] = {_schluessel(k): _schluessel(k) for k in kanzleien}

    def wurzel(x: str) -> str:
        while eltern[x] != x:
            eltern[x] = eltern[eltern[x]]
            x = eltern[x]
        return x

    def verbinden(a: Kanzlei, b: Kanzlei) -> None:
        eltern[wurzel(_schluessel(a))] = wurzel(_schluessel(b))

    je_domain: dict[str, list[Kanzlei]] = defaultdict(list)
    je_telefon: dict[str, list[Kanzlei]] = defaultdict(list)
    for k in kanzleien:
        if k.domain:
            je_domain[stammdomain(k.domain)].append(k)
        if k.telefon and (t := telefon_key(k.telefon)):
            je_telefon[t].append(k)
    for liste in je_domain.values():
        for k in liste[1:]:
            verbinden(k, liste[0])
    paare = []
    for liste in je_telefon.values():
        for i, a in enumerate(liste):
            for b in liste[i + 1:]:
                ha, hb = a.hauptansprechpartner, b.hauptansprechpartner
                if ha and hb and gleiche_person(ha.name, hb.name):
                    verbinden(a, b)
                    if not (a.domain and b.domain and stammdomain(a.domain) == stammdomain(b.domain)):
                        paare.append((a, b))
    nach_wurzel: dict[str, list[Kanzlei]] = defaultdict(list)
    for k in kanzleien:
        nach_wurzel[wurzel(_schluessel(k))].append(k)
    return [g for g in nach_wurzel.values() if len(g) > 1], paare


def zentrale(gruppe: list[Kanzlei]) -> Kanzlei:
    """Zeile auf der Stammdomain, die die meisten Zeilen der Gruppe teilen (shbb.de bei *.shbb.de), sonst die
    mit den meisten Ansprechpartnern."""
    zaehler = Counter(stammdomain(k.domain) for k in gruppe if k.domain)
    haupt = zaehler.most_common(1)[0][0] if zaehler else None

    def rang(k: Kanzlei) -> tuple[bool, int, int]:
        auf_stamm = bool(k.domain) and k.domain.removeprefix("www.") == haupt
        return (auf_stamm, len(k.ansprechpartner), -len(k.domain or ""))

    return max(gruppe, key=rang)


def standort_spalte(kanzleien: list[Kanzlei]) -> dict[str, str]:
    """Schlüssel (Domain bzw. id) → „Zentrale (5 Standorte)" / „Standort von SHBB …"."""
    ergebnis = {}
    for g in gruppen(kanzleien)[0]:
        z = zentrale(g)
        ergebnis[_schluessel(z)] = f"Zentrale ({len(g)} Standorte)"
        for k in g:
            if k is not z:
                ergebnis[_schluessel(k)] = f"Standort von {z.kanzleiname or z.domain}"
    return ergebnis


def dubletten_verdacht(kanzleien: list[Kanzlei], bundesland: str) -> list[Dublette]:
    """Gleiches Telefon + gleicher Hauptansprechpartner auf verschiedenen Domains → Verdacht fürs Blatt."""
    return [
        Dublette(behalten=_schluessel(a), dublette=_schluessel(b), bundesland=bundesland, kriterium=KRITERIUM,
                 art=DublettenArt.VERDACHT, kanzleiname=a.kanzleiname or b.kanzleiname,
                 details=f"Telefon {a.telefon}, Ansprechpartner {a.hauptansprechpartner.name}; Adressen "
                         f"{a.strasse or '–'}, {a.ort or '–'} / {b.strasse or '–'}, {b.ort or '–'}")
        for a, b in gruppen(kanzleien)[1]
    ]
