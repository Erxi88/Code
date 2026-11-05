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
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .models import Kanzlei, LaufEintrag, SchrittStatus
from .search.base import LimitErreicht, OrtsAdapter, SearchError
from .search.pipeline import Ort, Zuordnung
from .search.verzeichnisse import FIRMA_LAUT_VERZEICHNIS, OHNE_GESELLSCHAFT
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
class PlzBericht:
    """Eine Abfrage des Pilots: Einträge laut Verzeichnis, angezeigt/gesamt, Kandidaten nach Gruppierung, Abgleich und
    Antwortzeit (ohne Pause; 0 und `cache` bei einer Antwort aus dem Cache)."""
    plz: str
    ort: str
    eintraege: int | None = None  # Karten auf der Ergebnisseite (Berufsträger + Gesellschaften)
    angezeigt: int | None = None  # „Treffer: X von Y"
    gesamt: int | None = None
    kanzleien: int = 0  # Kandidaten nach der Gruppierung
    ohne_gesellschaft: int = 0  # davon Personen ohne Gesellschaft an ihrer Adresse
    neu: int = 0
    bekannt: int = 0
    sekunden: float | None = None
    cache: bool = False
    fehler: str | None = None

    @property
    def abgeschnitten(self) -> bool:
        return bool(self.angezeigt is not None and self.gesamt is not None and self.angezeigt < self.gesamt)


BERICHT_SPALTEN = ["PLZ", "Ort", "Einträge (Karten)", "angezeigt", "gesamt", "abgeschnitten", "Kanzleien (Kandidaten)",
                   "davon Person ohne Gesellschaft", "neu", "bekannt", "Antwortzeit (s)", "aus Cache", "Fehler"]


def bericht_csv(berichte: list[PlzBericht], pfad: str | Path) -> Path:
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(BERICHT_SPALTEN)
        for b in berichte:
            w.writerow([b.plz, b.ort, b.eintraege, b.angezeigt, b.gesamt, "ja" if b.abgeschnitten else "", b.kanzleien,
                        b.ohne_gesellschaft, b.neu, b.bekannt, "" if b.sekunden is None else f"{b.sekunden:.2f}".replace(".", ","),
                        "ja" if b.cache else "", b.fehler or ""])
    return pfad


def orte_im_kreis(orte: list[Ort], kreis: str) -> tuple[list[Ort], str | None]:
    """Gemeinden eines Kreises (Name wie in der Ortsliste, ohne Groß/Klein; eindeutiger Teilname genügt:
    „Kiel" → „Kreisfreie Stadt Kiel"). Gibt (Orte, Fehlermeldung) zurück."""
    k = kreis.casefold().strip()
    kreise_alle = sorted({o.kreis for o in orte if o.kreis})
    treffer = [x for x in kreise_alle if x.casefold() == k] or [x for x in kreise_alle if k in x.casefold()]
    if len(treffer) != 1:
        return [], (f"Kreis „{kreis}“ nicht eindeutig: {', '.join(treffer)}" if treffer else
                    f"Kreis „{kreis}“ nicht in der Ortsliste – vorhanden: {', '.join(kreise_alle)}")
    return [o for o in orte if o.kreis == treffer[0]], None


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
    berichte: list[PlzBericht] = field(default_factory=list)

    @property
    def abgeschnitten(self) -> list[PlzBericht]:
        return [b for b in self.berichte if b.abgeschnitten]


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
        abrufe = getattr(adapter, "abrufe", None)
        abrufe_vorher = len(abrufe) if abrufe is not None else 0
        bericht = PlzBericht(ort.plz, ort.name)
        stat.berichte.append(bericht)
        eintrag = LaufEintrag(lauf_id=lauf_id, schritt=SCHRITT, bundesland=bundesland, suchbegriff=begriff,
                              adapter=QUELLE, ort=ort.name)
        try:
            hits = adapter.search_ort(ort)
        except SearchError as e:
            bericht.fehler = str(e)
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
        for a in (abrufe or [])[abrufe_vorher:]:
            bericht.sekunden = (bericht.sekunden or 0.0) + a.get("sekunden", 0.0)
            bericht.cache = bericht.cache or a.get("cache", False)
            bericht.eintraege = a.get("eintraege", bericht.eintraege)
            bericht.angezeigt, bericht.gesamt = a.get("angezeigt", bericht.angezeigt), a.get("gesamt", bericht.gesamt)
        ergebnisse = [zuordnung.verarbeite(h, adapter, begriff, ort.name) for h in hits]
        neu = sum(e is True for e in ergebnisse)
        bekannt = sum(e is False for e in ergebnisse)
        bericht.kanzleien, bericht.neu, bericht.bekannt = len(hits), neu, bekannt
        bericht.ohne_gesellschaft = sum(OHNE_GESELLSCHAFT in (h.snippet or "") for h in hits)
        stat.treffer += len(hits)
        stat.neu += neu
        stat.bekannt += bekannt
        stat.verworfen += len(hits) - neu - bekannt
        eintrag.trefferzahl, eintrag.neue_kandidaten = len(hits), neu
        eintrag.anfragen = adapter.anfragen - vorher
        eintrag.laufzeit_s = round(time.monotonic() - start, 2)
        storage.log_lauf(eintrag)
        if fortschritt:
            einträge = (f"{bericht.eintraege} Einträge (Treffer {bericht.angezeigt} von {bericht.gesamt})"
                        if bericht.gesamt is not None else f"{bericht.eintraege or 0} Einträge")
            zeit = "Cache" if bericht.cache else (f"{bericht.sekunden:.1f} s" if bericht.sekunden is not None else "–")
            warnung = " · ABGESCHNITTEN" if bericht.abgeschnitten else ""
            fortschritt(f"PLZ {n}/{len(orte)} · {ort.plz} {ort.name}: {einträge} → {len(hits)} Kanzleien "
                        f"({bericht.ohne_gesellschaft} Person ohne Gesellschaft), {neu} neu, {bekannt} bekannt · {zeit}"
                        f"{warnung}")
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
    ohne_gesellschaft: bool = False  # „Person ohne Gesellschaft an dieser Adresse"
    firma: str | None = None  # Firmenzeile der Person laut Verzeichnis


LUECKEN_SPALTEN = ["Kandidat", "Name (amtlich)", "Berufsträger (amtlich)", "Kennzeichen", "Firma laut Verzeichnis",
                   "Straße", "PLZ", "Ort", "Kreis", "Website", "Telefon", "Stand", "Kanzleiname (Website)", "Konfidenz"]


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
        snippet = fund.snippet if fund else ""
        firma = (m.group(1).strip() if (m := re.search(rf"{re.escape(FIRMA_LAUT_VERZEICHNIS)}(.+?)(?:;|$)", snippet))
                 else None)
        berufstraeger = (m.group(1) if (m := re.search(r"Berufsträger \((.*?)\)", snippet)) else snippet) or None
        zeilen.append(Luecke(k.id, k.name, berufstraeger, (z.strasse if z else None) or k.strasse,
                             plz, (z.ort if z else None) or k.ort, kreis_je_plz.get(plz or ""), k.website,
                             (z.telefon if z else None) or k.telefon, stand, z.kanzleiname if z else None,
                             z.konfidenz.value if z else None, OHNE_GESELLSCHAFT in snippet, firma))
    return sorted(zeilen, key=lambda z: (z.plz or "", z.name or ""))


def luecken_werte(z: Luecke) -> list:
    return [z.kandidat_id, z.name, z.berufstraeger, OHNE_GESELLSCHAFT if z.ohne_gesellschaft else None, z.firma,
            z.strasse, z.plz, z.ort, z.kreis, z.website, z.telefon, z.stand, z.kanzleiname, z.konfidenz]


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


def neu_gruppieren_vorbereiten(storage: Storage, bundesland: str) -> tuple[int, int]:
    """Gruppierung aus dem Cache neu berechnen (ohne neue Abfragen): Kandidaten, die nur das amtliche Verzeichnis (und
    dessen Website-Nachsuche) angelegt hat, werden entfernt; anderen Kandidaten werden nur die amtlichen Funde
    abgenommen; das Lauf-Protokoll „stb-verzeichnis" wird geleert. Ein erneuter `stb-verzeichnis`-Lauf liest dann alle
    PLZ aus dem Cache und legt die Kandidaten nach den aktuellen Regeln an; Nachsuche, Crawl und Seiten kommen
    ebenfalls aus dem Cache, nur die Extraktion läuft neu. Gibt (entfernte Kandidaten, bereinigte Kandidaten) zurück."""
    from .search.nachsuche import MERKER

    entfernt = bereinigt = 0
    for k in storage.kandidaten(bundesland):
        funde = storage.funde_kandidat(k.id)
        amtlich = [f for f in funde if f.quelle == QUELLE]
        if not amtlich:
            continue
        andere = [f for f in funde if f.quelle != QUELLE and not (f.suchbegriff and storage.cache_get(MERKER, str(k.id)))]
        with storage.conn:
            if k.quelle == QUELLE and not andere:
                storage.conn.execute("DELETE FROM kanzleien WHERE kandidat_id = ?", (k.id,))
                storage.conn.execute("DELETE FROM kandidaten WHERE id = ?", (k.id,))
                entfernt += 1
            else:
                storage.conn.execute("DELETE FROM funde WHERE kandidat_id = ? AND quelle = ?", (k.id, QUELLE))
                bereinigt += 1
        if k.quelle != QUELLE or andere:
            storage.set_extract_status(k.id, SchrittStatus.OFFEN)
    with storage.conn:
        storage.conn.execute("DELETE FROM lauf_protokoll WHERE bundesland = ? AND schritt = ?", (bundesland, SCHRITT))
        storage.conn.execute("DELETE FROM api_cache WHERE adapter = ?", (MERKER,))
    return entfernt, bereinigt

