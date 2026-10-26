"""Vergleich aller aktiven Discovery-Adapter auf denselben Orten (search-compare).

Treffer werden über die gemeinsame Dubletten-Logik zu Kanzleien zusammengefasst – nicht
nur über die Domain, weil Verzeichnis-/Places-Treffer oft keine Website haben. Der
Vergleich schreibt keine Kandidaten; API-Antworten landen höchstens im Cache.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import partial
from itertools import combinations
from pathlib import Path

from .base import LimitErreicht, OrtsAdapter, SearchAdapter, SearchError, SearchHit
from .dedupe import Index, zusammenfuehren
from .pipeline import (
    Ort,
    Suchplan,
    als_suchplan,
    bezeichnung,
    schluessel,
    suche_query,
    vorbereiten,
)


@dataclass
class AdapterErgebnis:
    name: str
    # Ort → IDs der gefundenen Kanzleien (nach Blocklist und Zusammenführung)
    kanzleien_je_ort: dict[str, set[int]] = field(default_factory=dict)
    queries: int = 0
    fehler: int = 0
    treffer: int = 0
    anfragen: int = 0
    kosten: float = 0.0
    abbruch: str | None = None

    @property
    def kanzleien(self) -> set[int]:
        return set().union(*self.kanzleien_je_ort.values()) if self.kanzleien_je_ort else set()


@dataclass
class PaarVergleich:
    a: str
    b: str
    beide: set[int]
    nur_a: set[int]
    nur_b: set[int]


@dataclass
class VergleichsErgebnis:
    orte: list[str]
    ergebnisse: dict[str, AdapterErgebnis]
    uebersprungen: dict[str, str]  # Adapter → Grund
    bezeichnungen: dict[int, str] = field(default_factory=dict)  # Kanzlei-ID → Domain/Name
    domains: dict[int, str | None] = field(default_factory=dict)

    def kanzleien(self, adapter: str, ort: str | None = None) -> set[int]:
        e = self.ergebnisse[adapter]
        return e.kanzleien_je_ort.get(ort, set()) if ort else e.kanzleien

    def alle(self, ort: str | None = None) -> set[int]:
        return set().union(*(self.kanzleien(n, ort) for n in self.ergebnisse))

    def ueberschneidung(self, ort: str | None = None) -> set[int]:
        """Kanzleien, die alle gelaufenen Adapter gefunden haben."""
        mengen = [self.kanzleien(n, ort) for n in self.ergebnisse]
        return set.intersection(*mengen) if mengen else set()

    def exklusiv(self, adapter: str, ort: str | None = None) -> set[int]:
        """Nur dieser Adapter hat sie gefunden."""
        andere = set().union(*(self.kanzleien(n, ort) for n in self.ergebnisse if n != adapter))
        return self.kanzleien(adapter, ort) - andere

    def paare(self, ort: str | None = None) -> list[PaarVergleich]:
        result = []
        for a, b in combinations(self.ergebnisse, 2):
            ka, kb = self.kanzleien(a, ort), self.kanzleien(b, ort)
            result.append(PaarVergleich(a, b, ka & kb, ka - kb, kb - ka))
        return result

    def namen(self, ids: Iterable[int]) -> list[str]:
        return sorted(self.bezeichnungen[i] for i in ids)

    def als_csv(self, pfad: str | Path) -> Path:
        """Eine Zeile je (Ort, Kanzlei) mit x je Adapter, der sie gefunden hat."""
        pfad = Path(pfad)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        namen = list(self.ergebnisse)
        with pfad.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["ort", "kanzlei", "domain", *namen])
            for ort in self.orte:
                for kid in sorted(self.alle(ort), key=lambda i: self.bezeichnungen[i]):
                    w.writerow([
                        ort, self.bezeichnungen[kid], self.domains.get(kid) or "",
                        *("x" if kid in self.kanzleien(n, ort) else "" for n in namen),
                    ])  # fmt: skip
        return pfad


class _Gruppierung:
    """Fasst Treffer aller Adapter über die Dubletten-Logik zu Kanzlei-IDs zusammen."""

    def __init__(self) -> None:
        self.index = Index()
        self.bezeichnungen: dict[int, str] = {}
        self.domains: dict[int, str | None] = {}

    def id_fuer(self, hit: SearchHit, domain: str | None) -> int:
        s = schluessel(hit, domain)
        abgleich = self.index.finde(s)
        if abgleich.id is not None:
            self.index.setze(abgleich.id, zusammenfuehren(self.index.eintraege[abgleich.id], s))
            if domain and not self.domains.get(abgleich.id):
                self.domains[abgleich.id] = domain
                self.bezeichnungen[abgleich.id] = domain
            return abgleich.id
        kid = len(self.bezeichnungen) + 1
        self.index.setze(kid, s)
        self.bezeichnungen[kid] = bezeichnung(hit, domain)
        self.domains[kid] = domain
        return kid


def _nur_treffer(*args) -> list[SearchHit]:
    return suche_query(*args)[0]


def compare_adapters(
    adapters: list[SearchAdapter],
    orte: list[Ort],
    vorlagen: list[str] | Suchplan,
    blocklist: set[str],
    max_results: int,
) -> VergleichsErgebnis:
    """Alle Adapter mit demselben Suchplan (Varianten und Seiten je Einwohnerklasse)."""
    plan = als_suchplan(vorlagen)
    gruppen = _Gruppierung()
    ergebnisse: dict[str, AdapterErgebnis] = {}
    uebersprungen: dict[str, str] = {}
    for adapter in adapters:
        grund = adapter.nicht_konfiguriert_grund()
        if grund:
            uebersprungen[adapter.name] = grund
            continue
        erg = AdapterErgebnis(adapter.name)
        start_anfragen = adapter.anfragen
        for ort in orte:
            gefunden = erg.kanzleien_je_ort.setdefault(ort.name, set())
            if isinstance(adapter, OrtsAdapter):
                aufrufe = [] if ort.ist_zusatz else [partial(adapter.search_ort, ort)]
            else:
                stufe = plan.stufe(ort)
                aufrufe = [
                    partial(_nur_treffer, adapter, q, stufe, max_results, blocklist, plan.weiter)
                    for q in plan.queries(ort)
                ]
            for aufruf in aufrufe:
                erg.queries += 1
                try:
                    hits = aufruf()
                except LimitErreicht as e:
                    erg.abbruch = str(e)
                    break
                except SearchError:
                    erg.fehler += 1
                    continue
                erg.treffer += len(hits)
                for hit in hits:
                    if (vorbereitet := vorbereiten(hit, adapter, blocklist)) is not None:
                        gefunden.add(gruppen.id_fuer(*vorbereitet))
            if erg.abbruch:
                break
        erg.anfragen = adapter.anfragen - start_anfragen
        erg.kosten = adapter.kosten(erg.anfragen)
        ergebnisse[adapter.name] = erg
    return VergleichsErgebnis(
        [o.name for o in orte], ergebnisse, uebersprungen, gruppen.bezeichnungen, gruppen.domains
    )
