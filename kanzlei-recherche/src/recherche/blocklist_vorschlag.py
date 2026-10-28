"""Blocklist-Vorschläge aus „Nicht gefunden": Domains, die robots.txt oder HTTP 403 sperren und
bei vielen Suchorten als Treffer kamen, sind meist Portale, keine Kanzleien.

Nur lesend – die Entscheidung trifft ein Mensch und trägt die Domain in
config/domain_blocklist.txt ein.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .search.pipeline import ist_blockiert
from .storage import Storage

GRUENDE = ("robots.txt", "HTTP 403")


def stammdomain(domain: str) -> str:
    """„kiel.portal.example" → „portal.example" (Subdomains je Ort zusammenfassen)."""
    teile = domain.lower().split(".")
    return ".".join(teile[-2:]) if len(teile) > 2 else domain.lower()


@dataclass
class Vorschlag:
    domain: str  # Stammdomain
    domains: set[str] = field(default_factory=set)
    gruende: set[str] = field(default_factory=set)
    suchorte: set[str] = field(default_factory=set)
    treffer: int = 0
    name: str | None = None
    snippet: str | None = None

    @property
    def grund_kurz(self) -> str:
        return ", ".join(sorted("robots.txt" if "robots" in g else "HTTP 403" if "403" in g else g for g in self.gruende))


def vorschlaege(storage: Storage, bundesland: str, blocklist: set[str] | None = None,
                gruende: tuple[str, ...] = GRUENDE) -> list[Vorschlag]:
    """Je Stammdomain: Suchorte, Treffer, Beispiel-Name und -Snippet – meiste Suchorte zuerst."""
    blocklist = blocklist or set()
    ergebnis: dict[str, Vorschlag] = {}
    for ng in storage.nicht_gefunden(bundesland):
        if not any(g in ng.grund for g in gruende) or ist_blockiert(ng.domain, blocklist):
            continue
        v = ergebnis.setdefault(stammdomain(ng.domain), Vorschlag(stammdomain(ng.domain)))
        v.domains.add(ng.domain)
        v.gruende.add(ng.grund)
        funde = storage.funde_kandidat(ng.kandidat_id) if ng.kandidat_id else []
        v.treffer += len(funde)
        v.suchorte |= {f.suchort for f in funde if f.suchort}
        v.name = v.name or ng.name or next((f.name for f in funde if f.name), None)
        v.snippet = v.snippet or next((f.snippet for f in funde if f.snippet), None)
    return sorted(ergebnis.values(), key=lambda v: (-len(v.suchorte), -v.treffer, v.domain))


def als_csv(liste: list[Vorschlag], pfad: Path) -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["domain", "suchorte", "treffer", "grund", "subdomains", "name", "snippet"])
        for v in liste:
            w.writerow([v.domain, len(v.suchorte), v.treffer, v.grund_kurz, " ".join(sorted(v.domains)),
                        v.name or "", (v.snippet or "")[:300]])
    return pfad
