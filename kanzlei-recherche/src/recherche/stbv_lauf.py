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
from .search.verzeichnisse import (
    FIRMA_LAUT_VERZEICHNIS,
    NICHT_ZUGEORDNET,
    OHNE_GESELLSCHAFT,
    ist_einzeln,
)
from .storage import Storage

QUELLE = "stb_verzeichnis"
# Stand der Gruppierungsregeln (stbv_gruppieren): ein Land mit älterem Stand wird automatisch neu gruppiert
GRUPPIERUNG_VERSION = "2026-11-05-kurzform"  # Regeln 1, 2 (auch Firmenzeile↔Firmenzeile), Kurzformen
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
        bericht.ohne_gesellschaft = sum(ist_einzeln(h.snippet) for h in hits)
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
    kandidat_id: int | None  # None = Syndikus/Unternehmen (kein Kandidat)
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
    nicht_zugeordnet: bool = False  # Person ohne Firmenzeile, mehrere Kanzleien an der Adresse


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
                             z.konfidenz.value if z else None, ist_einzeln(snippet), firma,
                             NICHT_ZUGEORDNET in snippet))
    zeilen += syndikus_luecken(storage, bundesland, kreis_je_plz)
    return sorted(zeilen, key=lambda z: (z.plz or "", z.name or ""))


SYNDIKUS_KENNZEICHEN = "Syndikus/Unternehmen – kein Kandidat"


def syndikus_luecken(storage: Storage, bundesland: str, kreis_je_plz: dict[str, str] | None = None) -> list[Luecke]:
    """Berufsträger mit Firmenzeile ohne Steuerberatungsbezug (Syndikus eines Unternehmens) aus den gecachten
    Ergebnisseiten der im Land abgefragten PLZ – nie Kandidat, aber in der Lückenliste (Vorgabe 05.11.2026)."""
    import json

    from .search.verzeichnisse import stbv_karten, stbv_syndikus

    kreis_je_plz = kreis_je_plz or {}
    abgefragt = {r[0].removeprefix("plz:") for r in storage.conn.execute(
        "SELECT DISTINCT suchbegriff FROM lauf_protokoll WHERE bundesland = ? AND schritt = ? AND adapter = ? "
        "AND fehler IS NULL", (bundesland, SCHRITT, QUELLE))}
    if not abgefragt:
        return []
    gesehen, zeilen = set(), []
    for schluessel, antwort in storage.conn.execute(
            "SELECT schluessel, antwort FROM api_cache WHERE adapter = ?", (QUELLE,)):
        m = re.search(r"plzFilter=(\d{5})", schluessel)
        if not m or m.group(1) not in abgefragt:
            continue
        try:
            html = json.loads(antwort).get("html") or ""
        except (ValueError, AttributeError):
            continue
        karten, _ = stbv_karten(html, schluessel.split(" POST ")[0])
        for k in stbv_syndikus(karten):
            if k.url in gesehen or (k.plz and k.plz not in abgefragt):
                continue  # Nachbar-PLZ eines anderen Landes bzw. schon von einer anderen Abfrage
            gesehen.add(k.url)
            person = ", ".join(t for t in (" ".join(t for t in (k.titel, k.name) if t), k.beruf) if t)
            zeilen.append(Luecke(None, k.name, person, k.strasse, k.plz, k.ort, kreis_je_plz.get(k.plz or ""), None,
                                 None, "Syndikus – nicht angelegt", firma=k.firma))
    return zeilen


def luecken_werte(z: Luecke) -> list:
    kennzeichen = (SYNDIKUS_KENNZEICHEN if z.kandidat_id is None
                   else NICHT_ZUGEORDNET if z.nicht_zugeordnet else OHNE_GESELLSCHAFT if z.ohne_gesellschaft else None)
    return [z.kandidat_id, z.name, z.berufstraeger, kennzeichen, z.firma,
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


def gruppierung_veraltet(storage: Storage, bundesland: str) -> bool:
    """Das Land hat amtliche Funde einer älteren Gruppierung – vor Nachsuche/crawl/extract neu gruppieren."""
    stand = (storage.cache_get("stbv_gruppierung", bundesland) or {}).get("version")
    return stand != GRUPPIERUNG_VERSION and bool(storage.funde(bundesland, QUELLE))


def neu_gruppieren_vorbereiten(storage: Storage, bundesland: str) -> tuple[int, int]:
    """Gruppierung aus dem Cache neu berechnen (ohne neue Abfragen): Kandidaten, die nur das amtliche Verzeichnis (und
    dessen Website-Nachsuche) angelegt hat, werden entfernt; anderen Kandidaten werden nur die amtlichen Funde
    abgenommen; das Lauf-Protokoll „stb-verzeichnis" wird geleert. Ein erneuter `stb-verzeichnis`-Lauf liest dann alle
    PLZ aus dem Cache und legt die Kandidaten nach den aktuellen Regeln an; Nachsuche, Crawl und Seiten kommen
    ebenfalls aus dem Cache, nur die Extraktion läuft neu. Gibt (entfernte Kandidaten, bereinigte Kandidaten) zurück."""
    from .search.nachsuche import MERKER

    entfernt = bereinigt = 0
    ids = []
    for k in storage.kandidaten(bundesland):
        funde = storage.funde_kandidat(k.id)
        amtlich = [f for f in funde if f.quelle == QUELLE]
        if not amtlich:
            continue
        ids.append(str(k.id))
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
        # Nachsuche-Merker nur dieses Landes (die Brave-Antworten bleiben im Cache – nichts wird doppelt bezahlt)
        storage.conn.executemany("DELETE FROM api_cache WHERE adapter = ? AND schluessel = ?",
                                 [(MERKER, i) for i in ids])
    return entfernt, bereinigt



# ---------------------------------------------------------------------- Detailseiten (Vorgabe 05.11.2026)

AMTLICH = "amtlich"  # Fund-Quelle der Detailseite: Felder gelten vor Website-Daten
BERATUNGSSTELLE_VON = "Weitere Beratungsstelle von "


def offene_details(storage: Storage, bundesland: str, plz: set[str] | None = None) -> list[tuple[int, str]]:
    """(Kandidat, Detail-URL) aller Kandidaten mit Fund des amtlichen Verzeichnisses, deren Detailseite noch nicht
    gelesen ist (kein Fund „amtlich" mit dieser URL) – fortsetzbar."""
    offen = []
    for k in storage.kandidaten(bundesland):
        funde = storage.funde_kandidat(k.id)
        gelesen = {f.quell_id for f in funde if f.quelle == AMTLICH}
        for f in funde:
            if f.quelle == QUELLE and f.roh_url and "/details/" in f.roh_url and f.roh_url not in gelesen \
                    and (plz is None or f.plz in plz):
                offen.append((k.id, f.roh_url))
                break
    return offen


@dataclass
class DetailStatistik:
    gelesen: int = 0
    mit_internet: int = 0
    mit_telefon: int = 0
    mit_email: int = 0
    zusammengefuehrt: int = 0  # Internet-Domain gehörte schon einem Kandidaten
    beratungsstellen: int = 0  # neue Standort-Kandidaten
    leer: int = 0
    fehler: int = 0
    abbruch: str | None = None
    robots_gesperrt: bool = False  # robots.txt verbietet die Detailseiten – Schritt übersprungen


ROBOTS_GESPERRT = "Detailseiten per robots.txt gesperrt – übersprungen"


def run_details(storage: Storage, adapter, bundesland: str, blocklist: set[str], lauf_id: str,
                fortschritt: Callable[[str], None] | None = None, plz: set[str] | None = None) -> DetailStatistik:
    """Je Kandidat die Detailseite: Fund „amtlich" (Name bei Gesellschaften, Adresse, Telefon, Internet, JSON aller
    Felder im Snippet), Telefon/Website am Kandidaten ergänzen (gehört die Domain schon einem Kandidaten → zusammen-
    führen), weitere Beratungsstellen als eigene Standort-Kandidaten mit Verweis auf die Zentrale."""
    import json

    from .models import Fund
    from .normalize import normalize_telefon
    from .search.base import SearchHit
    from .search.pipeline import normalize_domain

    stat = DetailStatistik()
    zuordnung = Zuordnung(storage, bundesland, blocklist, lauf_id)
    offen = offene_details(storage, bundesland, plz)
    if offen and hasattr(adapter, "detail_erlaubt") and not adapter.detail_erlaubt(offen[0][1]):
        stat.robots_gesperrt = True  # eine Zeile statt einer Fehlermeldung je Kandidat
        if fortschritt:
            fortschritt(ROBOTS_GESPERRT)
        return stat
    for n, (kid, url) in enumerate(offen, start=1):
        k = storage.get_kandidat(kid)
        if k is None:
            continue
        try:
            d = adapter.detail(url)
        except SearchError as e:
            if isinstance(e, LimitErreicht):
                stat.abbruch = str(e)
                break
            if "robots.txt verbietet" in str(e):  # Sperre erst im Lauf (robots.txt geändert): einmal melden, Schluss
                stat.robots_gesperrt = True
                if fortschritt:
                    fortschritt(ROBOTS_GESPERRT)
                break
            stat.fehler += 1
            if fortschritt:
                fortschritt(f"Detail {n}/{len(offen)} · {k.name}: Fehler – {e}")
            continue
        stat.gelesen += 1
        if d is None:
            stat.leer += 1
            storage.add_fund(Fund(kandidat_id=kid, bundesland=bundesland, quelle=AMTLICH, quell_id=url, lauf_id=lauf_id,
                                  snippet="{}", roh_url=url))
            continue
        gesellschaft = d.rechtsform is not None or bool(d.geschaeftsfuehrung or d.gesellschafter)
        telefon = normalize_telefon(d.telefon) or normalize_telefon(d.mobil)
        domain = normalize_domain(d.internet) if d.internet else None
        ziel = kid
        if domain and (anderer := storage.kandidat_nach_domain(domain)) and anderer.id != kid:
            storage.kandidat_zusammenfuehren(kid, anderer.id)  # gleiche Website = derselbe Kandidat
            ziel = anderer.id
            stat.zusammengefuehrt += 1
        vorher = storage.get_kandidat(ziel)
        storage.ergaenze_kandidat(ziel, telefon=telefon, website=d.internet, domain=domain)
        if domain and vorher and not vorher.domain:
            storage.set_crawl_status(ziel, SchrittStatus.OFFEN)
        storage.set_extract_status(ziel, SchrittStatus.OFFEN)
        storage.add_fund(Fund(kandidat_id=ziel, bundesland=bundesland, quelle=AMTLICH, quell_id=url, lauf_id=lauf_id,
                              name=d.name if gesellschaft else None, strasse=d.strasse, plz=d.plz, ort=d.ort,
                              telefon=telefon, website=d.internet, snippet=json.dumps(d.als_dict(), ensure_ascii=False),
                              roh_url=url))
        stat.mit_internet += bool(d.internet)
        stat.mit_telefon += bool(telefon)
        stat.mit_email += bool(d.email)
        for i, (strasse, plz, ort) in enumerate(d.beratungsstellen, start=1):
            name = d.name if gesellschaft else (vorher.name if vorher else d.name)
            hit = SearchHit(quelle=AMTLICH, quell_id=f"{url}#beratungsstelle-{i}", name=name, strasse=strasse, plz=plz,
                            ort=ort, roh_url=url,
                            snippet=f"{BERATUNGSSTELLE_VON}{name} ({d.strasse}, {d.plz} {d.ort})")
            if zuordnung.verarbeite(hit, adapter, f"detail:{url}", ort) is True:
                stat.beratungsstellen += 1
            zeile = storage.conn.execute("SELECT kandidat_id FROM funde WHERE bundesland = ? AND quelle = ? AND "
                                         "quell_id = ?", (bundesland, AMTLICH, hit.quell_id)).fetchone()
            if zeile is not None:  # amtliche Felder der Zentrale (Telefon, Vertreter) mit der Adresse der Beratungsstelle
                info = dict(d.als_dict(), strasse=strasse, plz=plz, ort=ort, beratungsstelle_von=name)
                with storage.conn:
                    storage.conn.execute("UPDATE funde SET telefon = ?, snippet = ? WHERE kandidat_id = ? AND quell_id = ?",
                                         (telefon, json.dumps(info, ensure_ascii=False), zeile[0], hit.quell_id))
                storage.set_extract_status(zeile[0], SchrittStatus.OFFEN)
        if fortschritt:
            teile = [x for x in (telefon, d.email, d.internet) if x]
            fortschritt(f"Detail {n}/{len(offen)} · {d.name}: {', '.join(teile) or 'keine Kontaktdaten'}"
                        + (f", {len(d.beratungsstellen)} weitere Beratungsstelle(n)" if d.beratungsstellen else ""))
    return stat


# ---------------------------------------------------------------------- Sperre je Land (laufende Einzelläufe erkennen)

SPERRE = "stbv_sperre"
SPERRE_VERWAIST_S = 600  # ohne Lebenszeichen seit 10 min gilt eine Sperre als verwaist (Lauf abgebrochen)


def lauf_kennung() -> str:
    import os
    import socket
    import threading

    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def fremde_sperre(storage: Storage, bundesland: str, kennung: str, jetzt: float | None = None) -> dict | None:
    """Sperre eines anderen, noch lebenden Laufs (Lebenszeichen jünger als SPERRE_VERWAIST_S) – sonst None."""
    sperre = storage.cache_get(SPERRE, bundesland)
    jetzt = time.time() if jetzt is None else jetzt
    if not sperre or sperre.get("kennung") == kennung or jetzt - sperre.get("zuletzt", 0) > SPERRE_VERWAIST_S:
        return None
    return sperre


def sperre_setzen(storage: Storage, bundesland: str, kennung: str, schritt: str = "") -> None:
    alt = storage.cache_get(SPERRE, bundesland) or {}
    seit = alt.get("seit") if alt.get("kennung") == kennung else time.time()
    storage.cache_put(SPERRE, bundesland, {"kennung": kennung, "seit": seit, "zuletzt": time.time(), "schritt": schritt})


def sperre_loesen(storage: Storage, bundesland: str, kennung: str) -> None:
    if (storage.cache_get(SPERRE, bundesland) or {}).get("kennung") == kennung:
        with storage.conn:
            storage.conn.execute("DELETE FROM api_cache WHERE adapter = ? AND schluessel = ?", (SPERRE, bundesland))
