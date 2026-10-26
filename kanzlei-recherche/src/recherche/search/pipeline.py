"""Gemeinsame Discovery-Pipeline für alle Adapter.

Query-Vorlagen füllen, Website-Domains normalisieren, Blocklist anwenden, Treffer über
die Dubletten-Logik (dedupe.py) bestehenden Kandidaten zuordnen oder neu anlegen, jeden
Fund mit Quelle speichern, Lauf-Protokoll mit Kostenlog schreiben.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..models import Dublette, DublettenArt, Fund, Kandidat, LaufEintrag
from ..storage import Storage
from .base import (
    LimitErreicht,
    OrtsAdapter,
    QueryAdapter,
    SearchAdapter,
    SearchError,
    SearchHit,
)
from .dedupe import Index, Schluessel, zusammenfuehren

log = logging.getLogger(__name__)

_SCHEMA_OHNE_HOST = re.compile(r"^(mailto|tel|javascript|data):", re.I)


@dataclass(frozen=True)
class Ort:
    name: str
    plz: str
    kreis: str | None = None
    einwohner: int | None = None


# ---------------------------------------------------------------------- Konfiguration


def load_orte(pfad: str | Path) -> list[Ort]:
    with open(pfad, encoding="utf-8", newline="") as f:
        return [
            Ort(
                name=r["ort"].strip(),
                plz=(r.get("plz") or "").strip(),
                kreis=(r.get("kreis") or "").strip() or None,
                einwohner=int(r["einwohner"]) if (r.get("einwohner") or "").strip() else None,
            )
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


def website_domain(hit: SearchHit, blocklist: set[str]) -> tuple[str | None, bool]:
    """(Domain der Website, blockiert?) – Domain ist None ohne Website oder wenn blockiert."""
    if not hit.website:
        return None, False
    domain = normalize_domain(hit.website)
    if domain is None:
        return None, False
    if ist_blockiert(domain, blocklist):
        return None, True
    return domain, False


def vorbereiten(
    hit: SearchHit, adapter: SearchAdapter, blocklist: set[str]
) -> tuple[SearchHit, str | None] | None:
    """Blocklist für alle Adapter gleich: Websuche-Treffer auf Portalen werden verworfen,
    bei Verzeichnis-/Places-Treffern wird nur die (Portal-)Website entfernt."""
    domain, blockiert = website_domain(hit, blocklist)
    if not blockiert:
        return hit, domain
    if isinstance(adapter, QueryAdapter):
        return None
    return hit.model_copy(update={"website": None}), None


def schluessel(hit: SearchHit, domain: str | None) -> Schluessel:
    return Schluessel.aus_feldern(domain, hit.telefon, hit.strasse, hit.plz, hit.name, hit.ort)


def kandidat_schluessel(k: Kandidat) -> Schluessel:
    return Schluessel.aus_feldern(k.domain, k.telefon, k.strasse, k.plz, k.name, k.ort)


def bezeichnung(k: Kandidat | SearchHit, domain: str | None = None) -> str:
    """Lesbare Kennung für das Dubletten-Blatt: Domain, sonst Name und Adresse."""
    domain = domain or getattr(k, "domain", None)
    if domain:
        return domain
    teile = [k.name, k.strasse, " ".join(x for x in (k.plz, k.ort) if x)]
    return ", ".join(x for x in teile if x) or "(ohne Angaben)"


class Zuordnung:
    """Ordnet Treffer Kandidaten zu (Dubletten-Logik) und speichert Kandidaten und Funde."""

    def __init__(self, storage: Storage, bundesland: str, blocklist: set[str], lauf_id: str):
        self.storage = storage
        self.bundesland = bundesland
        self.blocklist = blocklist
        self.lauf_id = lauf_id
        self.index = Index()
        for k in storage.kandidaten(bundesland):
            self.index.setze(k.id, kandidat_schluessel(k))

    def verarbeite(
        self, hit: SearchHit, adapter: SearchAdapter, suchbegriff: str | None, suchort: str | None
    ) -> bool | None:
        """True: neuer Kandidat, False: bestehendem zugeordnet, None: verworfen/schon bekannt."""
        vorbereitet = vorbereiten(hit, adapter, self.blocklist)
        if vorbereitet is None:
            return None
        hit, domain = vorbereitet
        if self.storage.hat_fund(self.bundesland, hit.quelle, hit.quell_id):
            return None
        s = schluessel(hit, domain)
        abgleich = self.index.finde(s)
        felder = dict(
            domain=domain, website=hit.website, name=hit.name, strasse=hit.strasse,
            plz=hit.plz, ort=hit.ort, telefon=hit.telefon,
        )  # fmt: skip
        if abgleich.id is not None:
            kid, neu = abgleich.id, False
            vorher = self.storage.get_kandidat(kid)
            k = self.storage.ergaenze_kandidat(kid, **felder)
            self.index.setze(kid, zusammenfuehren(self.index.eintraege[kid], kandidat_schluessel(k)))
            if abgleich.art == DublettenArt.UNSICHER:
                self._dublette(vorher, hit, domain, abgleich.kriterium, DublettenArt.UNSICHER)
        else:
            k = Kandidat(bundesland=self.bundesland, suchort=suchort, quelle=hit.quelle, **felder)
            kid, neu = self.storage.add_kandidat(k), True
            self.index.setze(kid, s)
        for vid in abgleich.verdacht:
            if vid != kid:
                self._dublette(self.storage.get_kandidat(vid), hit, domain, "adresse", DublettenArt.VERDACHT)
        self.storage.add_fund(
            Fund(
                kandidat_id=kid, bundesland=self.bundesland, quelle=hit.quelle, quell_id=hit.quell_id,
                lauf_id=self.lauf_id, suchbegriff=suchbegriff, suchort=suchort, name=hit.name,
                strasse=hit.strasse, plz=hit.plz, ort=hit.ort, telefon=hit.telefon,
                website=hit.website, snippet=hit.snippet, roh_url=hit.roh_url,
            )
        )  # fmt: skip
        return neu

    def _dublette(self, k: Kandidat, hit: SearchHit, domain: str | None, kriterium: str, art: DublettenArt) -> None:
        self.storage.add_dublette(
            Dublette(
                behalten=bezeichnung(k), dublette=bezeichnung(hit, domain), bundesland=self.bundesland,
                kriterium=kriterium, art=art, kanzleiname=hit.name or k.name,
                details=f"Quelle {hit.quelle}: {hit.quell_id}",
            )
        )  # fmt: skip


# ---------------------------------------------------------------------- Lauf


@dataclass
class SuchStatistik:
    queries: int = 0
    uebersprungen: int = 0
    fehler: int = 0
    treffer: int = 0
    neue_kandidaten: int = 0
    anfragen: int = 0
    kosten: float = 0.0
    abbruch: str | None = None  # gesetzt, wenn der Lauf wegen LimitErreicht endete


def _auftraege(adapter: SearchAdapter, orte: list[Ort], vorlagen: list[str], max_results: int):
    """(Ort, Suchbegriff, Aufruf) je Arbeitseinheit des Adapters."""
    for ort in orte:
        if isinstance(adapter, OrtsAdapter):
            yield ort, f"ort:{ort.name}", partial(adapter.search_ort, ort)
        elif isinstance(adapter, QueryAdapter):
            for query in queries_fuer_ort(vorlagen, ort):
                yield ort, query, partial(adapter.search, query, max_results)


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
    """Führt einen Adapter für alle Orte aus und speichert Kandidaten und Funde.

    QueryAdapter (Stufe A): je Ort alle Query-Vorlagen. OrtsAdapter (Stufe B): je Ort ein
    Aufruf von search_ort. Wiederaufnehmbar: Erfolgreich erledigte Einheiten werden
    übersprungen, fehlgeschlagene beim nächsten Aufruf wiederholt. Bei LimitErreicht
    (Kontingent, Budget, Zugang) endet der Lauf sauber.
    """
    schritt = "fill" if isinstance(adapter, OrtsAdapter) else "search"
    zuordnung = Zuordnung(storage, bundesland, blocklist, lauf_id)
    stat = SuchStatistik()
    start_anfragen = adapter.anfragen
    for ort, suchbegriff, aufruf in _auftraege(adapter, orte, vorlagen, max_results):
        if storage.query_erledigt(bundesland, adapter.name, suchbegriff, schritt):
            stat.uebersprungen += 1
            continue
        start, anfragen_vorher = time.monotonic(), adapter.anfragen
        eintrag = LaufEintrag(
            lauf_id=lauf_id, schritt=schritt, bundesland=bundesland,
            suchbegriff=suchbegriff, adapter=adapter.name, ort=ort.name,
        )  # fmt: skip
        fehler: SearchError | None = None
        try:
            hits = aufruf()
        except SearchError as e:
            fehler, hits = e, []
        eintrag.anfragen = adapter.anfragen - anfragen_vorher
        eintrag.kosten = adapter.kosten(eintrag.anfragen)
        if fehler is not None:
            eintrag.fehler = str(fehler)
            eintrag.laufzeit_s = round(time.monotonic() - start, 2)
            storage.log_lauf(eintrag)
            if isinstance(fehler, LimitErreicht):
                stat.abbruch = str(fehler)
                log.warning("Lauf beendet: %s", fehler)
                break
            stat.fehler += 1
            log.warning("Suche fehlgeschlagen: %s", fehler)
            continue
        neu = sum(1 for h in hits if zuordnung.verarbeite(h, adapter, suchbegriff, ort.name))
        eintrag.trefferzahl = len(hits)
        eintrag.neue_kandidaten = neu
        eintrag.laufzeit_s = round(time.monotonic() - start, 2)
        storage.log_lauf(eintrag)
        stat.queries += 1
        stat.treffer += len(hits)
        stat.neue_kandidaten += neu
        if fortschritt:
            fortschritt(f"{suchbegriff}: {len(hits)} Treffer, {neu} neu, {eintrag.anfragen} Anfragen")
    stat.anfragen = adapter.anfragen - start_anfragen
    stat.kosten = adapter.kosten(stat.anfragen)
    return stat
