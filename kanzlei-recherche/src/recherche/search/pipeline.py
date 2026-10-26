"""Gemeinsame Such-Pipeline für alle Adapter.

Query-Vorlagen füllen, Treffer-URLs auf Domains normalisieren, Blocklist anwenden,
Dubletten entfernen (jede Domain genau einmal) und Kandidaten speichern.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..models import Kandidat, LaufEintrag
from ..storage import Storage
from .base import SearchAdapter, SearchError, SearchHit

log = logging.getLogger(__name__)

_SCHEMA_OHNE_HOST = re.compile(r"^(mailto|tel|javascript|data):", re.I)


@dataclass(frozen=True)
class Ort:
    name: str
    plz: str
    kreis: str | None = None


# ---------------------------------------------------------------------- Konfiguration


def load_orte(pfad: str | Path) -> list[Ort]:
    with open(pfad, encoding="utf-8", newline="") as f:
        return [
            Ort(name=r["ort"].strip(), plz=r["plz"].strip(), kreis=(r.get("kreis") or "").strip() or None)
            for r in csv.DictReader(f, delimiter=";")
            if r.get("ort")
        ]


def orte_filtern(orte: list[Ort], namen: Iterable[str] | None) -> list[Ort]:
    """Beschränkt auf die genannten Orte; unbekannte Namen werden als Ort ohne PLZ ergänzt."""
    if not namen:
        return orte
    nach_name = {o.name.casefold(): o for o in orte}
    return [nach_name.get(n.strip().casefold(), Ort(name=n.strip(), plz="")) for n in namen if n.strip()]


def load_vorlagen(pfad: str | Path) -> list[str]:
    daten = yaml.safe_load(Path(pfad).read_text(encoding="utf-8")) or {}
    return [v for v in daten.get("vorlagen", []) if v]


def queries_fuer_ort(vorlagen: list[str], ort: Ort) -> list[str]:
    """Füllt die Vorlagen; Vorlagen mit fehlenden Werten (z. B. {plz}) entfallen."""
    werte = {"ort": ort.name, "plz": ort.plz, "kreis": ort.kreis or ""}
    queries: list[str] = []
    for v in vorlagen:
        if any(f"{{{k}}}" in v and not w for k, w in werte.items()):
            continue
        q = " ".join(v.format(**werte).split())
        if q not in queries:
            queries.append(q)
    return queries


def load_blocklist(pfad: str | Path) -> set[str]:
    eintraege: set[str] = set()
    for zeile in Path(pfad).read_text(encoding="utf-8").splitlines():
        zeile = zeile.split("#", 1)[0].strip().lower()
        if zeile:
            eintraege.add(normalize_domain(zeile) or zeile)
    return eintraege


# ---------------------------------------------------------------------- Domains


def normalize_domain(url: str) -> str | None:
    """URL oder Hostname → kleingeschriebene Domain ohne www., Port und Punkt am Ende."""
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        if _SCHEMA_OHNE_HOST.match(url):
            return None
        url = "http://" + url
    teile = urlsplit(url)
    if teile.scheme not in ("http", "https"):
        return None
    host = (teile.hostname or "").rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if "." not in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return host


def ist_blockiert(domain: str, blocklist: set[str]) -> bool:
    teile = domain.split(".")
    return any(".".join(teile[i:]) in blocklist for i in range(len(teile) - 1))


def filter_hits(
    hits: Iterable[SearchHit], blocklist: set[str], bekannt: set[str] | None = None
) -> list[tuple[str, SearchHit]]:
    """Gibt (Domain, erster Treffer) je neuer, nicht blockierter Domain zurück.

    `bekannt` wird fortgeschrieben, damit über mehrere Queries hinweg jede Domain
    nur einmal auftaucht.
    """
    bekannt = bekannt if bekannt is not None else set()
    ergebnis: list[tuple[str, SearchHit]] = []
    for hit in hits:
        domain = normalize_domain(hit.url)
        if domain is None or domain in bekannt or ist_blockiert(domain, blocklist):
            continue
        bekannt.add(domain)
        ergebnis.append((domain, hit))
    return ergebnis


# ---------------------------------------------------------------------- Lauf


@dataclass
class SuchStatistik:
    queries: int = 0
    uebersprungen: int = 0
    fehler: int = 0
    treffer: int = 0
    neue_domains: int = 0


def run_search(
    storage: Storage,
    adapter: SearchAdapter,
    orte: list[Ort],
    vorlagen: list[str],
    blocklist: set[str],
    bundesland: str,
    lauf_id: str,
    max_results: int,
    fortschritt: Callable[[str], None] | None = None,
) -> SuchStatistik:
    """Sucht alle Queries aller Orte und speichert neue Domains als Kandidaten.

    Wiederaufnehmbar: Queries, die mit diesem Adapter schon erfolgreich liefen,
    werden übersprungen; fehlgeschlagene werden beim nächsten Aufruf erneut versucht.
    """
    stat = SuchStatistik()
    for ort in orte:
        for query in queries_fuer_ort(vorlagen, ort):
            if storage.query_erledigt(bundesland, adapter.name, query):
                stat.uebersprungen += 1
                continue
            start = time.monotonic()
            eintrag = LaufEintrag(
                lauf_id=lauf_id, schritt="search", bundesland=bundesland,
                suchbegriff=query, adapter=adapter.name, ort=ort.name,
            )  # fmt: skip
            try:
                hits = adapter.search(query, max_results)
            except SearchError as e:
                stat.fehler += 1
                eintrag.fehler = str(e)
                eintrag.laufzeit_s = round(time.monotonic() - start, 2)
                storage.log_lauf(eintrag)
                log.warning("Suche fehlgeschlagen: %s", e)
                continue
            neu = 0
            for domain, hit in filter_hits(hits, blocklist):
                kandidat = Kandidat(
                    domain=domain, bundesland=bundesland, url=hit.url, ort=ort.name,
                    suchbegriff=query, quelle=hit.quelle, titel=hit.titel, snippet=hit.snippet,
                )  # fmt: skip
                if storage.add_kandidat(kandidat):
                    neu += 1
            eintrag.trefferzahl = len(hits)
            eintrag.neue_domains = neu
            eintrag.laufzeit_s = round(time.monotonic() - start, 2)
            storage.log_lauf(eintrag)
            stat.queries += 1
            stat.treffer += len(hits)
            stat.neue_domains += neu
            if fortschritt:
                fortschritt(f"{query}: {len(hits)} Treffer, {neu} neue Domains")
    return stat
