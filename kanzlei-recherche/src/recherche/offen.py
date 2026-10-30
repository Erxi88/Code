"""`recherche offen`: Kandidaten ohne Kanzlei-Zeile („offen (nicht gelesen)" in coverage) nach Ursache.

Ursache aus Crawl- und Extract-Status und dem gespeicherten Crawl-Grund (kandidaten.fehler, wie im Blatt
„Nicht gefunden"), dazu je Ursache, womit sie voraussichtlich noch zu holen ist:

- `crawl --wiederholen`: Timeout, Verbindungsfehler, HTTP 5xx/429, unerwartete Fehler, „Kein Impressum" ohne
  die zusätzlichen Impressum-Pfade; mit `crawl.playwright: true` rendert derselbe Aufruf HTTP-403-Startseiten
  und Seiten ohne Text, die bisher nicht gerendert wurden.
- `crawl` / `extract`: noch nie gecrawlt bzw. gecrawlt, aber nicht extrahiert.
- nein: DNS, echtes robots-Verbot, Weiterleitung auf ein Portal, HTTP 404/410, kein HTML, schon gerendert ohne
  mehr Text, außerhalb laut Suchtreffer.

Für jeden Kandidaten außerdem der Länder-Hinweis aus den Suchtreffern (wie der Vorfilter vor dem Crawl): PLZ
aus Kandidat, Fund-Adressen und „01067 Dresden" in Titel/Snippet – alle außerhalb, mindestens eine im Land oder
keine PLZ; zusätzlich eine große Stadt eines anderen Landes in Titel/Snippet (Fremdort-Liste, ohne
Einwohnergrenze) und die Einwohnerzahl des Suchorts.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .crawl import _PLZ_IM_TEXT, DNS_GRUND
from .models import Kandidat, SchrittStatus
from .search.pipeline import Fremdorte, Ort
from .storage import Storage

WIEDERHOLEN = "crawl --wiederholen"
PLAYWRIGHT = "crawl --wiederholen (Playwright)"
CRAWL = "crawl"
EXTRACT = "extract"
EXTRACT_WIEDERHOLEN = "extract --wiederholen"
NEIN = "nein"

# zusätzliche Impressum-Pfade von crawl --wiederholen (crawl.py); fehlen sie in „probiert", lohnt ein zweiter Lauf
_GRUENDLICHE_PFADE = ("/imprint", "/impressum.php", "/kontakt/impressum")


def ursache(k: Kandidat, playwright_an: bool) -> tuple[str, str]:
    """(Ursache, womit holbar) für einen Kandidaten ohne Kanzlei-Zeile."""
    if not (k.domain or k.website):
        return ("ohne Website, nicht extrahiert", EXTRACT if k.extract_status != SchrittStatus.FEHLER else EXTRACT_WIEDERHOLEN)
    if k.crawl_status == SchrittStatus.OFFEN:
        return "nie gecrawlt", CRAWL
    if k.crawl_status == SchrittStatus.FERTIG:
        if k.extract_status == SchrittStatus.FEHLER:
            return f"Extraktion fehlgeschlagen ({_kurz(k.fehler) or '?'})", EXTRACT_WIEDERHOLEN
        return "gecrawlt, nicht extrahiert", EXTRACT
    g = k.fehler or ""
    if "laut Suchtreffer" in g:
        return "außerhalb laut Suchtreffer (nicht gecrawlt)", NEIN
    if DNS_GRUND in g:
        return "Domain existiert nicht (DNS)", NEIN
    if "robots.txt verbietet" in g:
        return "robots.txt verbietet", NEIN
    if g.startswith("Weiterleitung auf Portal"):
        return "Weiterleitung auf Portal", NEIN
    if g.startswith("Startseite:"):
        rest = g.removeprefix("Startseite:").strip()
        if m := re.match(r"HTTP (\d+)", rest):
            code = int(m.group(1))
            if code == 403:
                return "Startseite HTTP 403 (Bot-Schutz)", PLAYWRIGHT if playwright_an else NEIN + " (crawl.playwright aus)"
            if code >= 500 or code == 429:
                return f"Startseite HTTP {code // 100}xx/429", WIEDERHOLEN
            if code in (404, 410):
                return "Startseite HTTP 404/410", NEIN
            return f"Startseite HTTP {code}", NEIN
        if rest.startswith("Timeout"):
            return "Startseite Timeout", WIEDERHOLEN
        if rest.startswith("nicht erreichbar"):
            return f"Startseite nicht erreichbar{_klammer(rest)}", WIEDERHOLEN
        if rest.startswith("kein HTML"):
            return "Startseite kein HTML", NEIN
        return f"Startseite: {_kurz(rest)}", WIEDERHOLEN
    if g.startswith("Seite ohne Text"):
        pw = g.split("Playwright:", 1)[1].rstrip(")").strip() if "Playwright:" in g else ""
        if pw in ("crawl.playwright ist aus", "nicht versucht") or not pw:
            return "Seite ohne Text, nicht gerendert", PLAYWRIGHT if playwright_an else NEIN + " (crawl.playwright aus)"
        return f"Seite ohne Text, gerendert ({_kurz(pw)})", NEIN
    if g.startswith("Kein Impressum"):
        if any(p in g for p in _GRUENDLICHE_PFADE):
            return "kein Impressum (alle Pfade probiert)", NEIN
        return "kein Impressum (ohne zusätzliche Pfade)", WIEDERHOLEN
    if g.startswith("Fehler:"):
        return f"unerwarteter Fehler{_klammer(g.removeprefix('Fehler:').split(':')[0].strip())}", WIEDERHOLEN
    return f"sonstiges: {_kurz(g) or 'ohne Grund'}", WIEDERHOLEN


def _kurz(text: str | None) -> str:
    return re.sub(r"\s*\(.*\)$", "", (text or "").strip())[:60]


def _klammer(text: str) -> str:
    m = re.search(r"\(([^)]*)\)", text) if "(" in text else None
    inhalt = m.group(1) if m else text
    return f" ({inhalt})" if inhalt else ""


@dataclass
class OffenEintrag:
    kandidat_id: int
    domain: str | None
    name: str | None
    suchort: str | None
    einwohner: int | None
    ursache: str
    holbar: str
    land_hinweis: str  # „PLZ außerhalb" / „PLZ im Land" / „keine PLZ"
    plz_hinweis: str | None  # „01067 Dresden – Sachsen"
    fremdort: str | None  # große Stadt eines anderen Landes in Titel/Snippet
    grund: str | None


@dataclass
class OffenAuswertung:
    eintraege: list[OffenEintrag] = field(default_factory=list)

    def je_ursache(self) -> list[tuple[str, str, Counter]]:
        """(Ursache, holbar, Zähler über Länder-Hinweis/Fremdort/Kleinort) – größte zuerst."""
        gruppen: dict[tuple[str, str], Counter] = {}
        for e in self.eintraege:
            c = gruppen.setdefault((e.ursache, e.holbar), Counter())
            c["gesamt"] += 1
            c[e.land_hinweis] += 1
            c["fremdort"] += e.fremdort is not None and e.land_hinweis != "PLZ außerhalb"
            c["unter 5.000 EW"] += e.einwohner is not None and e.einwohner < 5000
        return sorted(((u, h, c) for (u, h), c in gruppen.items()), key=lambda x: -x[2]["gesamt"])

    def je_holbar(self) -> Counter:
        return Counter(e.holbar for e in self.eintraege)

    def vermutlich_ausserhalb(self) -> int:
        return sum(1 for e in self.eintraege if e.land_hinweis == "PLZ außerhalb" or e.fremdort)


def land_hinweis(k: Kandidat, funde, plz_land: Callable[[str], str | None], ziel_land: str) -> tuple[str, str | None]:
    """Wie der Vorfilter vor dem Crawl (crawl.ausserhalb_laut_suchtreffer), aber mit allen drei Ausgängen."""
    plz: dict[str, str] = {}
    for p, ort in [(k.plz, k.ort), *((f.plz, f.ort) for f in funde)]:
        if p:
            plz.setdefault(p, ort or "")
    for f in funde:
        for m in _PLZ_IM_TEXT.finditer(f"{f.name or ''} \n {f.snippet or ''}"):
            plz.setdefault(m.group(1), m.group(2))
    bekannt = {p: land for p in plz if (land := plz_land(p))}
    if not bekannt:
        return "keine PLZ", None
    if ziel_land in bekannt.values():
        p = next(p for p, land in bekannt.items() if land == ziel_land)
        return "PLZ im Land", f"{p} {plz[p]}".strip()
    p, land = next(iter(bekannt.items()))
    return "PLZ außerhalb", f"{p} {plz[p]} – {land}".replace("  ", " ")


def auswerten(storage: Storage, bundesland: str, orte: list[Ort], plz_land: Callable[[str], str | None],
              ziel_land: str, fremdorte: Fremdorte | None = None, playwright_an: bool = True) -> OffenAuswertung:
    mit_zeile = {k.kandidat_id for k in storage.kanzleien(bundesland)}
    nach_name = {o.name: o for o in orte}
    erg = OffenAuswertung()
    for k in storage.kandidaten(bundesland):
        if k.id in mit_zeile:
            continue
        funde = storage.funde_kandidat(k.id)
        u, holbar = ursache(k, playwright_an)
        hinweis, plz_text = land_hinweis(k, funde, plz_land, ziel_land)
        ort = nach_name.get(k.suchort or "")
        fremd = None
        if fremdorte is not None:
            pruef_ort = ort or Ort(k.suchort or "", "", einwohner=0)
            pruef_ort = Ort(**{**pruef_ort.__dict__, "einwohner": 0})  # ohne Einwohnergrenze
            fremd = next((g for f in funde if (g := fremdorte.grund(SimpleNamespace(name=f.name, snippet=f.snippet),
                                                                     pruef_ort))), None)
        erg.eintraege.append(OffenEintrag(
            kandidat_id=k.id, domain=k.domain, name=k.name, suchort=k.suchort, einwohner=ort.einwohner if ort else None,
            ursache=u, holbar=holbar, land_hinweis=hinweis, plz_hinweis=plz_text, fremdort=fremd, grund=k.fehler))
    return erg


def als_csv(erg: OffenAuswertung, pfad: Path) -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["kandidat_id", "domain", "name", "suchort", "einwohner", "ursache", "holbar", "land_hinweis",
                    "plz_hinweis", "fremdort", "grund"])
        for e in erg.eintraege:
            w.writerow([e.kandidat_id, e.domain or "", e.name or "", e.suchort or "", e.einwohner or "", e.ursache,
                        e.holbar, e.land_hinweis, e.plz_hinweis or "", e.fremdort or "", e.grund or ""])
    return pfad
