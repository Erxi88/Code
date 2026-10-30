"""Kanzlei-Erkennung: Ist der gelesene Kandidat überhaupt eine Steuerberaterkanzlei?

Magazine, Jobbörsen, Branchenbücher und Hochschulen haben oft ein sauberes Verlags-Impressum
und bekämen sonst Konfidenz „hoch". Eine Zeile gilt als Kanzlei, wenn mindestens eins zutrifft (ODER):

- das Impressum nennt eine Steuerberaterkammer (Aufsicht, § 5 DDG) – der wichtigste Anker,
- mindestens ein Berufsträger erkannt (Impressum oder Teamseite),
- Rechtsform aus der Kanzlei-Liste (`extract.kanzlei_rechtsformen`: PartG mbB, PartG, Einzelkanzlei),
- Kanzleiname enthält Steuer…, StB, Kanzlei, Treuhand, Revision oder Wirtschaftsprüf… (kein
  Domain-Name wie „11880-steuerberater.com" und kein Portal-Titel wie „Steuerberater in Kiel"),
- Titel oder Snippet eines Suchtreffers enthält „Steuerberat…", „Steuerkanzlei" oder „StB"
  („Spieckermann | Goetz" mit knapper Website).

Die letzten drei Merkmale zählen nicht,
- wenn die Stammdomain (alle Subdomains und Unterseiten zusammen) in PORTAL_AB_ORTEN oder mehr
  Suchorten gefunden wurde – Portale wie city-map.de kommen als viele Kandidaten mit je 1–2 Orten,
- oder wenn der Kanzleiname leer ist (Portal-Seite ohne Kanzlei).

Vorab „keine Kanzlei", auch wenn ein Merkmal zutrifft: Domain auf der Blocklist; Lohnsteuerhilfeverein
(„Lohnsteuerhilfe…", „e. V." im Namen); reine Rechtsanwaltskanzlei (Anwalt/Notar in Name oder Impressum, aber
nirgends Steuerberater, Steuerkammer, WP/vBP – gemischte bleiben); Name nur aus dem Ansprechpartner abgeleitet
ohne Steuerberaterkammer und ohne Berufsbezeichnung im Impressum (Gemeinden, Vereine, Praxen – Konfidenz dann
„niedrig").

Sonst: Status „keine Kanzlei", Grund vorn in „Abweichung", Konfidenz höchstens „mittel", eigenes
Excel-Blatt mit allen Feldern (nichts wird gelöscht). Nur aktiv mit `extract.kanzlei_pruefung: true`; `recherche kanzlei-pruefung` wendet
die Prüfung ohne LLM auf bereits extrahierte Kanzleien an.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..models import AUTO_STATUS, Fund, Kanzlei, Konfidenz, Seitentyp, Status
from ..storage import Storage
from ..textnorm import text_normalisieren
from .merge import konfidenz

STANDARD_RECHTSFORMEN = ("PartG mbB", "PartG", "Einzelkanzlei")
GRUND_PRAEFIX = "Keine Kanzlei: "

_KANZLEI_WORT = re.compile(r"steuer|\bstb\b|kanzlei|treuhand|revision|wirtschaftspr[üu]f", re.IGNORECASE)
_SUCHTREFFER_WORT = re.compile(r"steuerberat|steuerkanzlei|\bstb\b", re.IGNORECASE)
PORTAL_AB_ORTEN = 5  # ab so vielen Suchorten der Stammdomain zählen Name, Rechtsform und Snippet nicht
# kein Kanzleiname: Domain („11880-steuerberater.com") oder Portal-Seitentitel
_SCHEIN_NAME = re.compile(
    r"\b[\w-]+\.(?:de|com|net|org|info|eu|biz|online|tax)\b|\bsteuerberater(?:in|innen)?\s+(?:in|im|für|aus|bei|nahe)\b|"
    r"\b(?:finden|suche|vergleich|verzeichnis|branchenbuch|bewertung|die\s+besten|top\s*\d+|in\s+der\s+nähe)\b",
    re.IGNORECASE,
)
_KAMMER = re.compile(r"steuerberaterkammer|kammer\s+der\s+steuerberater", re.IGNORECASE)
_LOHNSTEUERHILFE = re.compile(r"lohnsteuerhilfe|\be\.\s?v\.(?!\w)|eingetragener\s+verein", re.IGNORECASE)
_ANWALT = re.compile(r"rechtsanw[aä]lt|anwalt|anwält|notar|rechtsanwaltskammer", re.IGNORECASE)
# Steuerberatung irgendwo belegt → gemischte Kanzlei (StB + RA) bleibt
_STEUER_BERUF = re.compile(r"steuerberat|\bstb\b|steuerbevollm|steuerkanzlei|steuerbüro|wirtschaftspr[üu]f|\bwp\b|"
                           r"buchpr[üu]f|\bvbp\b|steuerberaterkammer", re.IGNORECASE)
NAME_ABGELEITET = "Name abgeleitet aus Ansprechpartner"
# ausländische Domains: außerhalb ohne LLM (Österreich, Schweiz, Liechtenstein …)
AUSLAND_TLD = ("at", "ch", "li", "tirol", "wien", "swiss", "lu")
KEINE_DEUTSCHE_PLZ = "keine deutsche PLZ"
_AUSSERHALB_PRAEFIX = "Außerhalb: "
_AUSSERHALB_EINTRAG = re.compile(re.escape(_AUSSERHALB_PRAEFIX) + r"[^;]*(?:;\s*|$)")
# ein Grund-Eintrag in „Abweichung" (bis zum nächsten „; " oder Ende)
_GRUND_EINTRAG = re.compile(re.escape(GRUND_PRAEFIX) + r"[^;]*(?:;\s*|$)")


def kein_kanzlei_grund(k: Kanzlei, impressum_text: str | None,
                       rechtsformen: tuple[str, ...] | list[str] = STANDARD_RECHTSFORMEN,
                       funde: Sequence[Fund] = (), suchorte: int | None = None) -> str | None:
    """None, wenn ein Kanzlei-Merkmal zutrifft; sonst der Grund.

    `suchorte`: Zahl der Suchorte der Stammdomain über alle Kandidaten (Standard: nur diese Funde)."""
    if k.kanzleiname and _LOHNSTEUERHILFE.search(k.kanzleiname):
        return "Lohnsteuerhilfeverein"
    personen = " ".join(f"{p.titel or ''} {p.funktion or ''}" for p in k.ansprechpartner)
    belege = f"{k.kanzleiname or ''} \n {impressum_text or ''} \n {personen}"
    if _ANWALT.search(f"{k.kanzleiname or ''} \n {impressum_text or ''}") and not _STEUER_BERUF.search(belege):
        return "Rechtsanwaltskanzlei (kein Steuerberater in Name, Impressum oder bei den Ansprechpartnern)"
    kammer = bool(impressum_text and _KAMMER.search(impressum_text))
    if NAME_ABGELEITET in (k.abweichung or "") and not kammer and not _STEUER_BERUF.search(impressum_text or ""):
        return "kein Kanzleiname (aus Ansprechpartner abgeleitet), keine Steuerberaterkammer und keine Berufsbezeichnung im Impressum"
    if kammer:
        return None
    if k.berufstraeger_anzahl:
        return None
    basis = "keine Steuerberaterkammer im Impressum, kein Berufsträger"
    if suchorte is None:
        suchorte = len({f.suchort for f in funde if f.suchort})
    if suchorte >= PORTAL_AB_ORTEN:
        return f"{basis}, Domain in {suchorte} Suchorten (Portal)"
    name = k.kanzleiname if k.kanzleiname and not _SCHEIN_NAME.search(k.kanzleiname) else None
    if not name:
        return f"{basis}, kein Kanzleiname" + (f" („{k.kanzleiname}“ ist Domain oder Portal-Titel)" if k.kanzleiname else "")
    if k.rechtsform and k.rechtsform in rechtsformen:
        return None
    if _KANZLEI_WORT.search(name):
        return None
    if any(_SUCHTREFFER_WORT.search(f"{f.name or ''} {f.snippet or ''}") for f in funde):
        return None
    form = f" ({k.rechtsform})" if k.rechtsform else ""
    return f"{basis}, keine Kanzlei-Rechtsform{form}, kein Kanzleiwort im Namen, kein Steuerberater im Suchtreffer"


def grund_aus_abweichung(abweichung: str | None) -> str | None:
    if abweichung and (m := _GRUND_EINTRAG.search(abweichung)):
        return m.group(0)[len(GRUND_PRAEFIX):].rstrip("; ").strip()
    return None


def ausland_tld(domain: str | None) -> str | None:
    """„.at" für kanzlei.at / www.stb.tirol – sonst None."""
    tld = (domain or "").lower().rstrip(".").rsplit(".", 1)[-1]
    return f".{tld}" if domain and tld in AUSLAND_TLD else None


def ausserhalb_grund(k: Kanzlei, ziel_land: str | None) -> str | None:
    """Grund für „außerhalb Bundesland": ausländische Domain, keine (gültige) deutsche PLZ oder PLZ laut OpenPLZ
    in einem anderen Land. PLZ nicht prüfbar (OpenPLZ nicht erreichbar) → kein Grund."""
    if not ziel_land:
        return None
    if tld := ausland_tld(k.domain):
        return f"ausländische Domain ({tld})"
    if not k.plz or not re.fullmatch(r"\d{5}", k.plz) or (k.plz_verifiziert is False and not k.bundesland):
        return KEINE_DEUTSCHE_PLZ
    if k.bundesland and k.bundesland != ziel_land:
        return f"PLZ {k.plz} in {k.bundesland}"
    return None


def ausserhalb(k: Kanzlei, ziel_land: str | None) -> bool:
    return ausserhalb_grund(k, ziel_land) is not None


def status_setzen(k: Kanzlei, ziel_land: str | None = None) -> Kanzlei:
    """Automatischer Status: „keine Kanzlei" vor „außerhalb Bundesland" vor „neu" – nur wenn der
    bisherige Status automatisch ist (von Hand gepflegte bleiben). Ausländische Domain und keine deutsche PLZ
    stehen als „Außerhalb: …" in „Abweichung" (ein anderes Bundesland zeigt schon die Spalte)."""
    rest = _AUSSERHALB_EINTRAG.sub("", k.abweichung or "").strip().rstrip(";").strip() or None
    grund = ausserhalb_grund(k, ziel_land)
    if grund and not grund.startswith("PLZ "):
        rest = f"{_AUSSERHALB_PRAEFIX}{grund}" + (f"; {rest}" if rest else "")
    k.abweichung = rest
    if k.status in AUTO_STATUS:
        k.status = (Status.KEINE_KANZLEI if grund_aus_abweichung(k.abweichung)
                    else Status.AUSSERHALB if grund else Status.NEU)
    return k


def anwenden(k: Kanzlei, grund: str | None, impressum_gelesen: bool, ziel_land: str | None = None,
             kammer: bool = False) -> Kanzlei:
    """Grund, Konfidenz und Status setzen. Ein von Hand gepflegter Status bleibt (auch beim Speichern,
    Storage.upsert_kanzlei). Name nur aus dem Ansprechpartner abgeleitet und keine Kammer → Konfidenz niedrig."""
    rest = _GRUND_EINTRAG.sub("", k.abweichung or "").strip().rstrip(";").strip()
    k.abweichung = (GRUND_PRAEFIX + grund + (f"; {rest}" if rest else "")) if grund else (rest or None)
    k.konfidenz = konfidenz(k, impressum_gelesen, kanzlei=grund is None)
    if NAME_ABGELEITET in (k.abweichung or "") and not kammer:
        k.konfidenz = Konfidenz.NIEDRIG
    return status_setzen(k, ziel_land)


def pruefen(k: Kanzlei, seiten: dict, rechtsformen=STANDARD_RECHTSFORMEN, funde: Sequence[Fund] = (),
            suchorte: int | None = None, ziel_land: str | None = None, blocklist: set[str] | None = None) -> Kanzlei:
    impressum = seiten.get(Seitentyp.IMPRESSUM)
    text = text_normalisieren(impressum.text) if impressum else None
    from ..search.pipeline import ist_blockiert

    if blocklist and k.domain and ist_blockiert(k.domain, blocklist):
        grund = "Domain auf der Blocklist (Portal, Kammer, Verzeichnis)"
    else:
        grund = kein_kanzlei_grund(k, text, rechtsformen, funde, suchorte)
    return anwenden(k, grund, impressum is not None, ziel_land, kammer=bool(text and _KAMMER.search(text)))


# Baukasten-Hoster: jede Subdomain ist eine eigene Website (oft eine kleine Kanzlei)
_HOSTER = frozenset({
    "wixsite.com", "jimdosite.com", "jimdo.com", "jimdofree.com", "webnode.page", "webnode.com", "weebly.com",
    "wordpress.com", "blogspot.com", "business.site", "site123.me", "strato-hosting.eu", "ionos.de", "one.com",
    "homepage.t-online.de", "beepworld.de", "npage.de", "co.uk", "com.de",
})  # fmt: skip


def stammdomain(domain: str) -> str:
    """„kiel.portal.example" → „portal.example" (Subdomains je Ort zusammenfassen); bei Baukasten-
    Hostern bleibt die Subdomain („kanzlei.wixsite.com")."""
    teile = domain.lower().removeprefix("www.").split(".")
    for n in (3, 2):
        if len(teile) > n and ".".join(teile[-n:]) in _HOSTER:
            return ".".join(teile[-n - 1:])
    return ".".join(teile[-2:]) if len(teile) > 2 else ".".join(teile)


def suchorte_je_stammdomain(storage: Storage, bundesland: str) -> dict[str, int]:
    """Zahl der Suchorte je Stammdomain über alle Kandidaten und Funde des Bundeslandes."""
    orte: dict[str, set[str]] = {}
    for domain, suchort in storage.conn.execute(
        "SELECT k.domain, f.suchort FROM funde f JOIN kandidaten k ON k.id = f.kandidat_id "
        "WHERE k.bundesland = ? AND k.domain IS NOT NULL AND f.suchort IS NOT NULL", (bundesland,)
    ):
        orte.setdefault(stammdomain(domain), set()).add(suchort)
    return {d: len(o) for d, o in orte.items()}


@dataclass
class PruefStatistik:
    geprueft: int = 0
    keine_kanzlei: int = 0
    ausserhalb: int = 0
    wieder_kanzlei: int = 0
    herabgestuft: int = 0  # Konfidenz hoch → mittel/niedrig
    dubletten: int = 0  # Verdacht: gleiches Telefon + gleicher Ansprechpartner


def run_pruefung(storage: Storage, bundesland: str, rechtsformen=STANDARD_RECHTSFORMEN,
                 fortschritt: Callable[[str], None] | None = None, ziel_land: str | None = None,
                 kanzlei_erkennung: bool = True, plz_pruefen: Callable | None = None,
                 blocklist: set[str] | None = None) -> PruefStatistik:
    """Kanzlei-Erkennung und Bundesland-Filter auf alle extrahierten Kanzleien anwenden (ohne LLM).
    Ohne Website nur der Bundesland-Filter (Discovery aus Verzeichnissen, keine Seiten zum Prüfen)."""
    stat = PruefStatistik()
    orte = suchorte_je_stammdomain(storage, bundesland)
    for k in storage.kanzleien(bundesland):
        vorher_status, vorher_konf = k.status, k.konfidenz
        if plz_pruefen and k.plz:
            # Ort und Bundesland nach der aktuellen Prüfung (OpenPLZ-Schreibweise, „Wyk/Insel Föhr")
            p = plz_pruefen(k.plz, k.ort)
            k.ort = p.ort_kanonisch or k.ort
            k.bundesland = p.bundesland or k.bundesland
            k.plz_verifiziert = p.verifiziert if p.verifiziert is not None else k.plz_verifiziert
        if kanzlei_erkennung and k.domain and k.kandidat_id is not None:
            pruefen(k, storage.kandidat_seiten(k.kandidat_id), rechtsformen, storage.funde_kandidat(k.kandidat_id),
                    orte.get(stammdomain(k.domain), 0), ziel_land, blocklist)
        else:
            status_setzen(k, ziel_land)
        stat.geprueft += 1
        stat.ausserhalb += k.status == Status.AUSSERHALB
        if k.status == Status.KEINE_KANZLEI or grund_aus_abweichung(k.abweichung):
            stat.keine_kanzlei += 1
            if fortschritt and vorher_status != Status.KEINE_KANZLEI:
                fortschritt(f"{k.domain}: keine Kanzlei ({k.kanzleiname or '–'})")
        elif vorher_status == Status.KEINE_KANZLEI:
            stat.wieder_kanzlei += 1
        stat.herabgestuft += vorher_konf == Konfidenz.HOCH and k.konfidenz != Konfidenz.HOCH
        storage.upsert_kanzlei(k)
    # Dubletten-Verdacht: gleiches Telefon + gleicher Ansprechpartner auf verschiedenen Domains (SHBB)
    from .standorte import (
        KRITERIUM,
        KRITERIUM_DOMAINS,
        dubletten_verdacht,
        zusammengefuehrt,
    )

    kanzleien = [k for k in storage.kanzleien(bundesland) if k.status not in (Status.KEINE_KANZLEI,)]
    storage.loesche_dubletten(bundesland, KRITERIUM)  # bei jedem Lauf neu (auch alte Paar-Einträge)
    storage.loesche_dubletten(bundesland, KRITERIUM_DOMAINS)
    for d in [*dubletten_verdacht(kanzleien, bundesland), *zusammengefuehrt(kanzleien, bundesland)]:
        storage.add_dublette(d)
        stat.dubletten += 1
    return stat
