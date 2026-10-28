"""Blocklist-Vorschläge: Domains aus „Nicht gefunden" (robots.txt, HTTP 403) und gelesene Domains mit
Status „keine Kanzlei" – bei vielen Suchorten als Treffer sind es meist Portale, keine Kanzleien
(ab PORTAL_AB_ORTEN fast sicher).

Nur lesend – die Entscheidung trifft ein Mensch und trägt die Domain in
config/domain_blocklist.txt ein.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .models import Status
from .search.pipeline import ist_blockiert
from .storage import Storage

GRUENDE = ("robots.txt", "HTTP 403")
KEINE_KANZLEI = "keine Kanzlei"
PORTAL_AB_ORTEN = 5


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
        return ", ".join(sorted({"robots.txt" if "robots" in g else "HTTP 403" if "403" in g else g for g in self.gruende}))

    @property
    def portal(self) -> bool:
        return len(self.suchorte) >= PORTAL_AB_ORTEN


def vorschlaege(storage: Storage, bundesland: str, blocklist: set[str] | None = None,
                gruende: tuple[str, ...] = (*GRUENDE, KEINE_KANZLEI)) -> list[Vorschlag]:
    """Je Stammdomain: Suchorte, Treffer, Beispiel-Name und -Snippet – meiste Suchorte zuerst."""
    blocklist = blocklist or set()
    ergebnis: dict[str, Vorschlag] = {}

    def aufnehmen(domain: str, grund: str, kandidat_id: int | None, name: str | None) -> None:
        v = ergebnis.setdefault(stammdomain(domain), Vorschlag(stammdomain(domain)))
        v.domains.add(domain)
        v.gruende.add(grund)
        funde = storage.funde_kandidat(kandidat_id) if kandidat_id else []
        v.treffer += len(funde)
        v.suchorte |= {f.suchort for f in funde if f.suchort}
        v.name = v.name or name or next((f.name for f in funde if f.name), None)
        v.snippet = v.snippet or next((f.snippet for f in funde if f.snippet), None)

    for ng in storage.nicht_gefunden(bundesland):
        if any(g in ng.grund for g in gruende) and not ist_blockiert(ng.domain, blocklist):
            aufnehmen(ng.domain, ng.grund, ng.kandidat_id, ng.name)
    if KEINE_KANZLEI in gruende:
        for k in storage.kanzleien(bundesland):
            if k.status == Status.KEINE_KANZLEI and k.domain and not ist_blockiert(k.domain, blocklist):
                aufnehmen(k.domain, KEINE_KANZLEI, k.kandidat_id, k.kanzleiname)
    return sorted(ergebnis.values(), key=lambda v: (-len(v.suchorte), -v.treffer, v.domain))


def als_csv(liste: list[Vorschlag], pfad: Path) -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["domain", "suchorte", "treffer", "portal", "grund", "subdomains", "name", "snippet"])
        for v in liste:
            w.writerow([v.domain, len(v.suchorte), v.treffer, "ja" if v.portal else "", v.grund_kurz, " ".join(sorted(v.domains)),
                        v.name or "", (v.snippet or "")[:300]])
    return pfad
