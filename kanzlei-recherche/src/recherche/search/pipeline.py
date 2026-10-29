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
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..config import EinwohnerStufe, WeiterWennVollConfig
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
    ags: str | None = None  # amtlicher Gemeindeschlüssel
    plz_weitere: tuple[str, ...] = ()
    gemeinde: str | None = None  # gesetzt bei Zusatz-Suchorten (PLZ-Bezirk/Stadtteil großer Städte)
    stadtteil: str | None = None  # eigener Ortsname laut OpenPLZ, falls vorhanden

    @property
    def ist_zusatz(self) -> bool:
        return self.gemeinde is not None

    @property
    def alle_plz(self) -> tuple[str, ...]:
        return tuple(p for p in (self.plz, *self.plz_weitere) if p)


ORTE_SPALTEN = ["ort", "plz", "kreis", "einwohner", "ags", "plz_weitere", "gemeinde", "stadtteil"]


# ---------------------------------------------------------------------- Konfiguration


def _zelle(r: dict, k: str) -> str:
    return (r.get(k) or "").strip()


def load_orte(pfad: str | Path, mit_zusatz: bool = True) -> list[Ort]:
    """Ortsliste (Startliste oder von `recherche orte` erzeugt); Zusatz-Suchorte optional."""
    with open(pfad, encoding="utf-8-sig", newline="") as f:
        orte = [
            Ort(
                name=_zelle(r, "ort"),
                plz=_zelle(r, "plz"),
                kreis=_zelle(r, "kreis") or None,
                einwohner=int(_zelle(r, "einwohner")) if _zelle(r, "einwohner") else None,
                ags=_zelle(r, "ags") or None,
                plz_weitere=tuple(_zelle(r, "plz_weitere").split()),
                gemeinde=_zelle(r, "gemeinde") or None,
                stadtteil=_zelle(r, "stadtteil") or None,
            )
            for r in csv.DictReader(f, delimiter=";")
            if _zelle(r, "ort")
        ]
    return orte if mit_zusatz else [o for o in orte if not o.ist_zusatz]


def schreibe_orte(orte: list[Ort], pfad: str | Path) -> Path:
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with pfad.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";", lineterminator="\n")
        w.writerow(ORTE_SPALTEN)
        for o in orte:
            w.writerow([o.name, o.plz, o.kreis or "", o.einwohner if o.einwohner is not None else "", o.ags or "",
                        " ".join(o.plz_weitere), o.gemeinde or "", o.stadtteil or ""])  # fmt: skip
    return pfad


def orte_filtern(orte: list[Ort], namen: Iterable[str] | None) -> list[Ort]:
    """Beschränkt auf die genannten Orte (samt ihren Zusatz-Suchorten); unbekannte Namen
    werden als Ort ohne PLZ ergänzt."""
    if not namen:
        return orte
    nach_name = {o.name.casefold(): o for o in orte}
    ergebnis: list[Ort] = []
    for n in (n.strip() for n in namen):
        if not n:
            continue
        ort = nach_name.get(n.casefold(), Ort(name=n, plz=""))
        ergebnis.append(ort)
        ergebnis += [o for o in orte if o.gemeinde and o.gemeinde.casefold() == n.casefold()]
    return ergebnis


def _vorlagen_yaml(pfad: str | Path) -> dict:
    return yaml.safe_load(Path(pfad).read_text(encoding="utf-8-sig")) or {}


def load_vorlagen(pfad: str | Path) -> list[str]:
    return [v for v in _vorlagen_yaml(pfad).get("vorlagen", []) if v]


def load_stadtteil_vorlagen(pfad: str | Path) -> list[str]:
    return [v for v in _vorlagen_yaml(pfad).get("stadtteil_vorlagen", []) if v]


def queries_fuer_ort(vorlagen: list[str], ort: Ort, n: int | None = None) -> list[str]:
    """Füllt die Vorlagen; Vorlagen mit fehlenden Werten (z. B. {plz}) entfallen.
    n begrenzt auf die ersten n verwendbaren Vorlagen (Einwohnerregel)."""
    werte = {"ort": ort.name, "plz": ort.plz, "kreis": ort.kreis or "", "gemeinde": ort.gemeinde or "",
             "stadtteil": ort.stadtteil or ""}  # fmt: skip
    queries: list[str] = []
    for v in vorlagen:
        if any(f"{{{k}}}" in v and not w for k, w in werte.items()):
            continue
        q = " ".join(v.format(**werte).split())
        if q not in queries:
            queries.append(q)
    return queries if n is None else queries[:n]


@dataclass
class Suchplan:
    """Welche Queries mit wie vielen Ergebnisseiten je Ort gesucht werden (Einwohnerregel)."""

    vorlagen: list[str]
    stadtteil_vorlagen: list[str] = field(default_factory=list)
    regel: list[EinwohnerStufe] = field(default_factory=list)
    ohne_einwohner: EinwohnerStufe = field(default_factory=EinwohnerStufe)
    stadtteil: EinwohnerStufe = field(default_factory=EinwohnerStufe)
    weiter: WeiterWennVollConfig = field(default_factory=lambda: WeiterWennVollConfig(aktiv=False))

    @classmethod
    def aus_settings(cls, settings) -> Suchplan:
        a = settings.discovery.stufe_a
        pfad = settings.pfad(settings.pfade.queries)
        return cls(load_vorlagen(pfad), load_stadtteil_vorlagen(pfad), a.einwohner_regel, a.ohne_einwohner,
                   a.stadtteil, a.weiter_wenn_voll)  # fmt: skip

    def stufe(self, ort: Ort) -> EinwohnerStufe:
        if ort.ist_zusatz:
            return self.stadtteil
        if ort.einwohner is None:
            return self.ohne_einwohner if self.regel else EinwohnerStufe()
        for s in self.regel:
            if s.unter is None or ort.einwohner < s.unter:
                return s
        return EinwohnerStufe()

    def queries(self, ort: Ort) -> list[str]:
        vorlagen = self.stadtteil_vorlagen if ort.ist_zusatz else self.vorlagen
        return queries_fuer_ort(vorlagen, ort, self.stufe(ort).varianten)


def als_suchplan(vorlagen: list[str] | Suchplan) -> Suchplan:
    return vorlagen if isinstance(vorlagen, Suchplan) else Suchplan(list(vorlagen))


def load_blocklist(pfad: str | Path) -> set[str]:
    eintraege: set[str] = set()
    for zeile in Path(pfad).read_text(encoding="utf-8-sig").splitlines():
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


def anteil_kanzlei_domains(hits: list[SearchHit], blocklist: set[str]) -> float:
    """Anteil der Treffer mit eigener (nicht blockierter) Website – grobes Maß für „echte Kanzleien"."""
    if not hits:
        return 0.0
    gut = sum(1 for h in hits if (d := website_domain(h, blocklist))[0] is not None and not d[1])
    return gut / len(hits)


def suche_query(
    adapter: QueryAdapter,
    query: str,
    stufe: EinwohnerStufe,
    max_results: int,
    blocklist: set[str],
    weiter: WeiterWennVollConfig,
) -> tuple[list[SearchHit], int]:
    """Query mit Seitenbegrenzung der Einwohnerstufe.

    Nach der letzten erlaubten Seite wird trotzdem weitergeblättert, solange die Seite voll war
    und überwiegend Kanzlei-Domains enthielt. Gibt (Treffer, zusätzlich geholte Seiten) zurück.
    """
    if stufe.seiten is None:
        return adapter.search(query, max_results), 0
    if not adapter.blaettert:
        return adapter.search(query, min(max_results, stufe.seiten * adapter.seitengroesse)), 0
    hits: list[SearchHit] = []
    extra = 0
    for nr, seite in enumerate(adapter.seiten(query, max_results), start=1):
        hits.extend(seite)
        if nr > stufe.seiten:
            extra += 1
        if nr >= stufe.seiten:
            voll = len(seite) >= adapter.seitengroesse
            if not (weiter.aktiv and stufe.weiter and voll
                    and anteil_kanzlei_domains(seite, blocklist) >= weiter.anteil_kanzlei_domains):
                break
    return hits, extra


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
    zusatzseiten: int = 0  # über die Seitenregel hinaus geholt (volle Seite mit vielen Kanzleien)
    stadtteile: int = 0  # automatisch nachgesuchte Zusatz-Suchorte großer Städte
    bekannt: int = 0  # Treffer, die einem bestehenden Kandidaten zugeordnet wurden (Telefon, Adresse, Domain)
    verworfen: int = 0  # Blocklist oder schon von dieser Quelle gefunden
    fremdort: int = 0  # Kleingemeinde-Treffer mit großer Stadt eines anderen Bundeslandes – nicht angelegt
    abbruch: str | None = None  # gesetzt, wenn der Lauf wegen LimitErreicht endete


def _auftraege(adapter: SearchAdapter, orte: list[Ort], plan: Suchplan, max_results: int, blocklist: set[str]):
    """(Ort, Suchbegriff, Aufruf) je Arbeitseinheit des Adapters; Aufruf → (Treffer, Zusatzseiten)."""
    for ort in orte:
        if isinstance(adapter, OrtsAdapter):
            if not ort.ist_zusatz:  # Lückenfüller arbeiten je Gemeinde
                yield ort, f"ort:{ort.name}", lambda o=ort: (adapter.search_ort(o), 0)
        elif isinstance(adapter, QueryAdapter):
            stufe = plan.stufe(ort)
            for query in plan.queries(ort):
                yield ort, query, partial(suche_query, adapter, query, stufe, max_results, blocklist, plan.weiter)


class Fremdorte:
    """Treffer einer Kleingemeinde-Suche, die eine große Stadt eines anderen Bundeslandes nennen, nicht aber
    den Suchort („Steuerberater Bonn" bei „Steuerberater Tetenbüll") – kein Kandidat."""

    def __init__(self, staedte: dict[str, str], land: str, unter_einwohner: int = 2000):
        namen = sorted((n for n, k in staedte.items() if k.upper() != land.upper()), key=len, reverse=True)
        self.muster = re.compile(r"(?<![\wäöüß-])(" + "|".join(map(re.escape, namen)) + r")(?![\wäöüß])") if namen else None
        self.unter_einwohner = unter_einwohner

    @classmethod
    def laden(cls, pfad: str | Path, land: str, unter_einwohner: int = 2000) -> Fremdorte:
        p = Path(pfad)
        daten = yaml.safe_load(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
        return cls({str(k): str(v) for k, v in (daten or {}).items()}, land, unter_einwohner)

    def grund(self, hit: SearchHit, ort: Ort) -> str | None:
        if self.muster is None or ort.einwohner is None or ort.einwohner >= self.unter_einwohner:
            return None
        text = f"{hit.name or ''} {hit.snippet or ''}"
        if not (m := self.muster.search(text)):
            return None
        suchort = (ort.gemeinde or ort.name).split("(")[0].strip()
        if suchort and re.search(r"(?<![\wäöüß])" + re.escape(suchort.split()[0]), text, re.I):
            return None
        return m.group(1)


def _nummer(orte: list[Ort], ort: Ort) -> int:
    return next((i for i, o in enumerate(orte, start=1) if o is ort), 0)


def schaetze_anfragen(storage: Storage, adapter: QueryAdapter, orte: list[Ort], vorlagen: list[str] | Suchplan,
                      bundesland: str, max_results: int) -> tuple[int, int]:
    """(offene Queries, höchstens nötige Anfragen) für einen Websuche-Lauf – Seitenregel je Ort, ohne
    Zusatzseiten (weiter_wenn_voll) und ohne nachgesuchte Stadtteile; erledigte Queries zählen nicht."""
    plan = als_suchplan(vorlagen)
    queries = anfragen = 0
    seite = max(1, adapter.seitengroesse)
    for ort in orte:
        stufe = plan.stufe(ort)
        je_query = -(-min(max_results, (stufe.seiten or 99) * seite) // seite)
        for q in plan.queries(ort):
            if not storage.query_erledigt(bundesland, adapter.name, q, "search"):
                queries += 1
                anfragen += je_query
    return queries, anfragen


def _grenze(adapter: QueryAdapter, stufe: EinwohnerStufe, max_results: int) -> int:
    """Trefferzahl, ab der eine Liste voll ist (max_results bzw. Seitenregel × Seitengröße)."""
    return max_results if stufe.seiten is None else min(max_results, stufe.seiten * adapter.seitengroesse)


def run_search(
    storage: Storage,
    adapter: SearchAdapter,
    orte: list[Ort],
    vorlagen: list[str] | Suchplan,
    blocklist: set[str],
    bundesland: str,
    lauf_id: str,
    max_results: int,
    fortschritt: Callable[[str], None] | None = None,
    stadtteile: Callable[[Ort], list[Ort]] | None = None,
    fremdorte: Fremdorte | None = None,
) -> SuchStatistik:
    """Führt einen Adapter für alle Orte aus und speichert Kandidaten und Funde.

    `stadtteile`: liefert für eine große Stadt Zusatz-Suchorte je PLZ-Bezirk. Ist eine Trefferliste der
    Stadt voll (die Suche bricht ab, „Steuerberater Kiel" bei 60), werden sie automatisch nachgesucht –
    auch wenn die Ortsliste keine Stadtteil-Einträge hat.

    QueryAdapter (Stufe A): je Ort die Query-Vorlagen und Seiten laut Suchplan (Einwohnerregel).
    OrtsAdapter (Stufe B): je Ort ein
    Aufruf von search_ort. Wiederaufnehmbar: Erfolgreich erledigte Einheiten werden
    übersprungen, fehlgeschlagene beim nächsten Aufruf wiederholt. Bei LimitErreicht
    (Kontingent, Budget, Zugang) endet der Lauf sauber.
    """
    schritt = "fill" if isinstance(adapter, OrtsAdapter) else "search"
    zuordnung = Zuordnung(storage, bundesland, blocklist, lauf_id)
    stat = SuchStatistik()
    start_anfragen = adapter.anfragen
    plan = als_suchplan(vorlagen)
    orte = list(orte)  # wächst um nachgesuchte Stadtteile (der Generator sieht neue Einträge)
    def nachsuchen(ort: Ort, trefferzahl: int) -> None:
        """Volle Trefferliste einer Stadt → Zusatz-Suchorte je PLZ-Bezirk anhängen (einmal je Gemeinde)."""
        if not stadtteile or not isinstance(adapter, QueryAdapter) or ort.ist_zusatz:
            return
        if trefferzahl < _grenze(adapter, plan.stufe(ort), max_results) or any(o.gemeinde == ort.name for o in orte):
            return
        neue = stadtteile(ort)
        orte.extend(neue)
        stat.stadtteile += len(neue)
        if neue and fortschritt:
            fortschritt(f"{ort.name}: Trefferliste voll ({trefferzahl}) – {len(neue)} PLZ-/Stadtteil-Suchorte nachgesucht")

    for ort, suchbegriff, aufruf in _auftraege(adapter, orte, plan, max_results, blocklist):
        if storage.query_erledigt(bundesland, adapter.name, suchbegriff, schritt):
            stat.uebersprungen += 1
            if stadtteile:  # Wiederaufnahme / Pilot-Bestand: auch erledigte volle Queries lösen aus
                nachsuchen(ort, storage.query_treffer(bundesland, adapter.name, suchbegriff, schritt))
            continue
        start, anfragen_vorher = time.monotonic(), adapter.anfragen
        eintrag = LaufEintrag(
            lauf_id=lauf_id, schritt=schritt, bundesland=bundesland,
            suchbegriff=suchbegriff, adapter=adapter.name, ort=ort.name,
        )  # fmt: skip
        fehler: SearchError | None = None
        try:
            hits, extra = aufruf()
        except SearchError as e:
            fehler, hits, extra = e, [], 0
        stat.zusatzseiten += extra
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
        behalten = hits
        if fremdorte and isinstance(adapter, QueryAdapter):
            behalten = [h for h in hits if not fremdorte.grund(h, ort)]
            stat.fremdort += len(hits) - len(behalten)
        ergebnisse = [zuordnung.verarbeite(h, adapter, suchbegriff, ort.name) for h in behalten]
        neu = sum(1 for e in ergebnisse if e is True)
        stat.bekannt += sum(1 for e in ergebnisse if e is False)
        stat.verworfen += sum(1 for e in ergebnisse if e is None)
        eintrag.trefferzahl = len(hits)
        eintrag.neue_kandidaten = neu
        eintrag.laufzeit_s = round(time.monotonic() - start, 2)
        storage.log_lauf(eintrag)
        stat.queries += 1
        stat.treffer += len(hits)
        stat.neue_kandidaten += neu
        if fortschritt:
            zusatz = f", +{extra} Seite(n) wegen voller Kanzlei-Seite" if extra else ""
            gesamt = adapter.anfragen - start_anfragen
            fortschritt(f"Ort {_nummer(orte, ort)}/{len(orte)} · {suchbegriff}: {len(hits)} Treffer, {neu} neu, "
                        f"{eintrag.anfragen} Anfragen{zusatz} · gesamt {gesamt} Anfragen ≈ {adapter.kosten(gesamt):.2f}")
        nachsuchen(ort, len(hits))
    stat.anfragen = adapter.anfragen - start_anfragen
    stat.kosten = adapter.kosten(stat.anfragen)
    return stat
