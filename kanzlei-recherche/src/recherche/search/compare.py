"""Vergleich mehrerer Such-Adapter auf denselben Orten und Queries (search-compare)."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from .base import SearchAdapter, SearchError
from .pipeline import Ort, filter_hits, queries_fuer_ort


@dataclass
class AdapterErgebnis:
    name: str
    # Ort → Domains (bereits gefiltert: Blocklist, Dubletten)
    domains_je_ort: dict[str, set[str]] = field(default_factory=dict)
    queries: int = 0
    fehler: int = 0
    treffer: int = 0

    @property
    def domains(self) -> set[str]:
        return set().union(*self.domains_je_ort.values()) if self.domains_je_ort else set()


@dataclass
class PaarVergleich:
    a: str
    b: str
    beide: set[str]
    nur_a: set[str]
    nur_b: set[str]


@dataclass
class VergleichsErgebnis:
    orte: list[str]
    ergebnisse: dict[str, AdapterErgebnis]
    uebersprungen: dict[str, str]  # Adapter → Grund

    def domains(self, adapter: str, ort: str | None = None) -> set[str]:
        e = self.ergebnisse[adapter]
        return e.domains_je_ort.get(ort, set()) if ort else e.domains

    def ueberschneidung(self, ort: str | None = None) -> set[str]:
        """Domains, die alle gelaufenen Adapter gefunden haben."""
        mengen = [self.domains(n, ort) for n in self.ergebnisse]
        return set.intersection(*mengen) if mengen else set()

    def alle_domains(self, ort: str | None = None) -> set[str]:
        return set().union(*(self.domains(n, ort) for n in self.ergebnisse))

    def paare(self, ort: str | None = None) -> list[PaarVergleich]:
        result = []
        for a, b in combinations(self.ergebnisse, 2):
            da, db = self.domains(a, ort), self.domains(b, ort)
            result.append(PaarVergleich(a, b, da & db, da - db, db - da))
        return result

    def als_csv(self, pfad: str | Path) -> Path:
        """Eine Zeile je (Ort, Domain) mit x je Adapter, der sie gefunden hat."""
        pfad = Path(pfad)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        namen = list(self.ergebnisse)
        with pfad.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["ort", "domain", *namen])
            for ort in self.orte:
                for domain in sorted(self.alle_domains(ort)):
                    w.writerow([ort, domain, *("x" if domain in self.domains(n, ort) else "" for n in namen)])
        return pfad


def compare_adapters(
    adapters: list[SearchAdapter],
    orte: list[Ort],
    vorlagen: list[str],
    blocklist: set[str],
    max_results: int,
) -> VergleichsErgebnis:
    ergebnisse: dict[str, AdapterErgebnis] = {}
    uebersprungen: dict[str, str] = {}
    for adapter in adapters:
        grund = adapter.nicht_konfiguriert_grund()
        if grund:
            uebersprungen[adapter.name] = grund
            continue
        erg = AdapterErgebnis(adapter.name)
        for ort in orte:
            bekannt: set[str] = set()
            for query in queries_fuer_ort(vorlagen, ort):
                erg.queries += 1
                try:
                    hits = adapter.search(query, max_results)
                except SearchError:
                    erg.fehler += 1
                    continue
                erg.treffer += len(hits)
                filter_hits(hits, blocklist, bekannt)
            erg.domains_je_ort[ort.name] = bekannt
        ergebnisse[adapter.name] = erg
    return VergleichsErgebnis([o.name for o in orte], ergebnisse, uebersprungen)
