"""Stufe B für ein ganzes Land: das amtliche Steuerberaterverzeichnis PLZ für PLZ (`recherche stb-verzeichnis -b SH`).

Ablauf (Entscheidung 04.11.2026): alle PLZ des Landes (aus der Ortsliste, je PLZ einmal) → Abfrage je PLZ (POST,
30–35 s Pause, robots.txt, Cache) → Abgleich mit dem Bestand über die Dubletten-Logik (`Zuordnung`: Domain, Telefon,
Adresse, PLZ + Name) → neue Einträge direkt als Kandidaten (Quelle „stb_verzeichnis") → Website-Nachsuche über Brave
(Kostenschätzung vorab) → crawl → extract. Die **Lückenliste** sind die Kandidaten, die das amtliche Verzeichnis neu
angelegt hat (erster Fund „stb_verzeichnis") – Blatt „Lücken (amtlich)" im Export des Landes und CSV.

Wiederaufnehmbar: jede erledigte PLZ steht im Lauf-Protokoll (Schritt „stb-verzeichnis", Suchbegriff „plz:24103");
ein erneuter Aufruf fragt nur die offenen ab, Antworten aus dem Cache kosten nichts.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import Kanzlei, LaufEintrag, SchrittStatus
from .search.base import LimitErreicht, OrtsAdapter, SearchError
from .search.pipeline import Ort, Zuordnung
from .storage import Storage

QUELLE = "stb_verzeichnis"
SCHRITT = "stb-verzeichnis"


def plz_orte(orte: list[Ort]) -> list[Ort]:
    """Je PLZ des Landes ein Suchort (Name und Kreis der ersten Gemeinde mit dieser PLZ), sortiert nach PLZ –
    ohne Zusatz-Suchorte großer Städte (ihre PLZ stehen schon bei der Gemeinde)."""
    je_plz: dict[str, Ort] = {}
    for o in orte:
        if o.ist_zusatz:
            continue
        for plz in o.alle_plz:
            je_plz.setdefault(plz, Ort(o.name, plz, o.kreis, o.einwohner, o.ags))
    return [je_plz[p] for p in sorted(je_plz)]


def suchbegriff(ort: Ort) -> str:
    return f"plz:{ort.plz}"


def offene_plz(storage: Storage, bundesland: str, orte: list[Ort]) -> list[Ort]:
    return [o for o in orte if not storage.query_erledigt(bundesland, QUELLE, suchbegriff(o), SCHRITT)]


@dataclass
class VerzeichnisStatistik:
    plz: int = 0
    uebersprungen: int = 0  # schon erledigt (Wiederaufnahme)
    treffer: int = 0
    neu: int = 0
    bekannt: int = 0
    verworfen: int = 0  # schon von dieser Quelle gefunden
    fehler: int = 0
    abbruch: str | None = None


def run_verzeichnis(storage: Storage, adapter: OrtsAdapter, orte: list[Ort], bundesland: str, blocklist: set[str],
                    lauf_id: str, fortschritt: Callable[[str], None] | None = None) -> VerzeichnisStatistik:
    """Je PLZ eine Abfrage; Treffer über die Dubletten-Logik in den Bestand (neue Kandidaten direkt)."""
    zuordnung = Zuordnung(storage, bundesland, blocklist, lauf_id)
    stat = VerzeichnisStatistik(plz=len(orte))
    for n, ort in enumerate(orte, start=1):
        begriff = suchbegriff(ort)
        if storage.query_erledigt(bundesland, QUELLE, begriff, SCHRITT):
            stat.uebersprungen += 1
            continue
        start, vorher = time.monotonic(), adapter.anfragen
        eintrag = LaufEintrag(lauf_id=lauf_id, schritt=SCHRITT, bundesland=bundesland, suchbegriff=begriff,
                              adapter=QUELLE, ort=ort.name)
        try:
            hits = adapter.search_ort(ort)
        except SearchError as e:
            eintrag.fehler, eintrag.anfragen = str(e), adapter.anfragen - vorher
            eintrag.laufzeit_s = round(time.monotonic() - start, 2)
            storage.log_lauf(eintrag)
            if isinstance(e, LimitErreicht):
                stat.abbruch = str(e)
                break
            stat.fehler += 1
            if fortschritt:
                fortschritt(f"PLZ {n}/{len(orte)} · {ort.plz} {ort.name}: Fehler – {e}")
            continue
        ergebnisse = [zuordnung.verarbeite(h, adapter, begriff, ort.name) for h in hits]
        neu = sum(e is True for e in ergebnisse)
        bekannt = sum(e is False for e in ergebnisse)
        stat.treffer += len(hits)
        stat.neu += neu
        stat.bekannt += bekannt
        stat.verworfen += len(hits) - neu - bekannt
        eintrag.trefferzahl, eintrag.neue_kandidaten = len(hits), neu
        eintrag.anfragen = adapter.anfragen - vorher
        eintrag.laufzeit_s = round(time.monotonic() - start, 2)
        storage.log_lauf(eintrag)
        if fortschritt:
            fortschritt(f"PLZ {n}/{len(orte)} · {ort.plz} {ort.name}: {len(hits)} Kanzleien, {neu} neu, {bekannt} bekannt")
    return stat


# ---------------------------------------------------------------------- Lückenliste


@dataclass
class Luecke:
    kandidat_id: int
    name: str | None
    berufstraeger: str | None
    strasse: str | None
    plz: str | None
    ort: str | None
    kreis: str | None
    website: str | None
    telefon: str | None
    stand: str  # nicht gecrawlt / gelesen / extrahiert: Status der Zeile
    kanzleiname: str | None = None
    konfidenz: str | None = None


LUECKEN_SPALTEN = ["Kandidat", "Name (amtlich)", "Berufsträger (amtlich)", "Straße", "PLZ", "Ort", "Kreis", "Website",
                   "Telefon", "Stand", "Kanzleiname (Website)", "Konfidenz"]


def luecken(storage: Storage, bundesland: str, kreis_je_plz: dict[str, str] | None = None) -> list[Luecke]:
    """Kandidaten, die das amtliche Verzeichnis neu angelegt hat (im Bestand vorher unbekannt), nach PLZ."""
    kreis_je_plz = kreis_je_plz or {}
    zeilen = []
    for k in storage.kandidaten(bundesland):
        if k.quelle != QUELLE or k.id is None:
            continue
        fund = next((f for f in storage.funde_kandidat(k.id) if f.quelle == QUELLE), None)
        z: Kanzlei | None = storage.kanzlei_fuer_kandidat(k.id)
        if z is not None:
            stand = f"extrahiert ({z.status.value})"
        elif k.website and k.crawl_status == SchrittStatus.OFFEN:
            stand = "Website gefunden, noch nicht gelesen"
        elif k.website:
            stand = f"gelesen (crawl {k.crawl_status.value}, extract {k.extract_status.value})"
        else:
            stand = "ohne Website (amtliche Anschrift)"
        plz = (z.plz if z and z.plz else None) or k.plz
        zeilen.append(Luecke(k.id, k.name, fund.snippet if fund else None, (z.strasse if z else None) or k.strasse,
                             plz, (z.ort if z else None) or k.ort, kreis_je_plz.get(plz or ""), k.website,
                             (z.telefon if z else None) or k.telefon, stand, z.kanzleiname if z else None,
                             z.konfidenz.value if z else None))
    return sorted(zeilen, key=lambda z: (z.plz or "", z.name or ""))


def luecken_werte(z: Luecke) -> list:
    return [z.kandidat_id, z.name, z.berufstraeger, z.strasse, z.plz, z.ort, z.kreis, z.website, z.telefon, z.stand,
            z.kanzleiname, z.konfidenz]


def luecken_csv(zeilen: list[Luecke], pfad: str | Path) -> Path:
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:  # BOM: Excel erkennt UTF-8
        w = csv.writer(f, delimiter=";")
        w.writerow(LUECKEN_SPALTEN)
        w.writerows(luecken_werte(z) for z in zeilen)
    return pfad


def kreise(orte: list[Ort]) -> dict[str, str]:
    return {p: o.kreis for o in orte if not o.ist_zusatz and o.kreis for p in o.alle_plz}
