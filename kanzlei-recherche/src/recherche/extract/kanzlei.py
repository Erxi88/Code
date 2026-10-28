"""Kanzlei-Erkennung: Ist der gelesene Kandidat überhaupt eine Steuerberaterkanzlei?

Magazine, Jobbörsen, Branchenbücher und Hochschulen haben oft ein sauberes Verlags-Impressum
und bekämen sonst Konfidenz „hoch". Eine Zeile gilt als Kanzlei, wenn mindestens eins zutrifft (ODER):

- das Impressum nennt eine Steuerberaterkammer (Aufsicht, § 5 DDG) – der wichtigste Anker,
- mindestens ein Berufsträger erkannt (Impressum oder Teamseite),
- Rechtsform aus der Kanzlei-Liste (`extract.kanzlei_rechtsformen`: PartG mbB, PartG, Einzelkanzlei),
- Kanzleiname enthält Steuer…, StB, Kanzlei, Treuhand, Revision oder Wirtschaftsprüf…,
- Titel oder Snippet eines Suchtreffers enthält „Steuerberat…", „Steuerkanzlei" oder „StB"
  („Spieckermann | Goetz" mit knapper Website) – nur bei weniger als SNIPPET_BIS_ORTE Suchorten,
  denn Branchenbücher („Die besten Steuerberater in Kiel") kommen in vielen Orten.

Sonst: Status „keine Kanzlei", Grund vorn in „Abweichung", Konfidenz höchstens „mittel", eigenes
Excel-Blatt mit allen Feldern (nichts wird gelöscht). Nur aktiv mit `extract.kanzlei_pruefung: true`; `recherche kanzlei-pruefung` wendet
die Prüfung ohne LLM auf bereits extrahierte Kanzleien an.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..models import Fund, Kanzlei, Konfidenz, Seitentyp, Status
from ..storage import Storage
from ..textnorm import text_normalisieren
from .merge import konfidenz

STANDARD_RECHTSFORMEN = ("PartG mbB", "PartG", "Einzelkanzlei")
GRUND_PRAEFIX = "Keine Kanzlei: "

_KANZLEI_WORT = re.compile(r"steuer|\bstb\b|kanzlei|treuhand|revision|wirtschaftspr[üu]f", re.IGNORECASE)
_SUCHTREFFER_WORT = re.compile(r"steuerberat|steuerkanzlei|\bstb\b", re.IGNORECASE)
SNIPPET_BIS_ORTE = 5  # ab so vielen Suchorten zählt das Snippet nicht (Portal)
_KAMMER = re.compile(r"steuerberaterkammer|kammer\s+der\s+steuerberater", re.IGNORECASE)
# ein Grund-Eintrag in „Abweichung" (bis zum nächsten „; " oder Ende)
_GRUND_EINTRAG = re.compile(re.escape(GRUND_PRAEFIX) + r"[^;]*(?:;\s*|$)")


def kein_kanzlei_grund(k: Kanzlei, impressum_text: str | None,
                       rechtsformen: tuple[str, ...] | list[str] = STANDARD_RECHTSFORMEN,
                       funde: Sequence[Fund] = ()) -> str | None:
    """None, wenn ein Kanzlei-Merkmal zutrifft; sonst der Grund (welche Merkmale fehlen)."""
    if impressum_text and _KAMMER.search(impressum_text):
        return None
    if k.berufstraeger_anzahl:
        return None
    if k.rechtsform and k.rechtsform in rechtsformen:
        return None
    if k.kanzleiname and _KANZLEI_WORT.search(k.kanzleiname):
        return None
    orte = {f.suchort for f in funde if f.suchort}
    suchtreffer = len(orte) < SNIPPET_BIS_ORTE and any(
        _SUCHTREFFER_WORT.search(f"{f.name or ''} {f.snippet or ''}") for f in funde)
    if suchtreffer:
        return None
    form = f" ({k.rechtsform})" if k.rechtsform else ""
    treffer = f"Suchtreffer in {len(orte)} Orten (Portal?)" if len(orte) >= SNIPPET_BIS_ORTE else "kein Steuerberater im Suchtreffer"
    return (f"keine Steuerberaterkammer im Impressum, kein Berufsträger, keine Kanzlei-Rechtsform{form}, "
            f"kein Kanzleiwort im Namen, {treffer}")


def grund_aus_abweichung(abweichung: str | None) -> str | None:
    if abweichung and (m := _GRUND_EINTRAG.search(abweichung)):
        return m.group(0)[len(GRUND_PRAEFIX):].rstrip("; ").strip()
    return None


def anwenden(k: Kanzlei, grund: str | None, impressum_gelesen: bool) -> Kanzlei:
    """Status, Grund und Konfidenz setzen. Ein von Hand gepflegter Status bleibt beim Speichern
    erhalten (Storage.upsert_kanzlei); hier wechselt nur „neu" ↔ „keine Kanzlei"."""
    rest = _GRUND_EINTRAG.sub("", k.abweichung or "").strip().rstrip(";").strip()
    if grund:
        k.abweichung = GRUND_PRAEFIX + grund + (f"; {rest}" if rest else "")
        if k.status == Status.NEU:
            k.status = Status.KEINE_KANZLEI
    else:
        k.abweichung = rest or None
        if k.status == Status.KEINE_KANZLEI:
            k.status = Status.NEU
    k.konfidenz = konfidenz(k, impressum_gelesen, kanzlei=grund is None)
    return k


def pruefen(k: Kanzlei, seiten: dict, rechtsformen=STANDARD_RECHTSFORMEN, funde: Sequence[Fund] = ()) -> Kanzlei:
    impressum = seiten.get(Seitentyp.IMPRESSUM)
    text = text_normalisieren(impressum.text) if impressum else None
    return anwenden(k, kein_kanzlei_grund(k, text, rechtsformen, funde), impressum is not None)


@dataclass
class PruefStatistik:
    geprueft: int = 0
    keine_kanzlei: int = 0
    wieder_kanzlei: int = 0
    herabgestuft: int = 0  # Konfidenz hoch → mittel/niedrig


def run_pruefung(storage: Storage, bundesland: str, rechtsformen=STANDARD_RECHTSFORMEN,
                 fortschritt: Callable[[str], None] | None = None) -> PruefStatistik:
    """Kanzlei-Erkennung auf alle extrahierten Kanzleien mit Website anwenden (ohne LLM)."""
    stat = PruefStatistik()
    for k in storage.kanzleien(bundesland):
        if not k.domain or k.kandidat_id is None:
            continue  # ohne Website: Discovery aus Verzeichnissen, keine Seiten zum Prüfen
        vorher_status, vorher_konf = k.status, k.konfidenz
        pruefen(k, storage.kandidat_seiten(k.kandidat_id), rechtsformen, storage.funde_kandidat(k.kandidat_id))
        stat.geprueft += 1
        if k.status == Status.KEINE_KANZLEI or grund_aus_abweichung(k.abweichung):
            stat.keine_kanzlei += 1
            if fortschritt and vorher_status != Status.KEINE_KANZLEI:
                fortschritt(f"{k.domain}: keine Kanzlei ({k.kanzleiname or '–'})")
        elif vorher_status == Status.KEINE_KANZLEI:
            stat.wieder_kanzlei += 1
        stat.herabgestuft += vorher_konf == Konfidenz.HOCH and k.konfidenz != Konfidenz.HOCH
        storage.upsert_kanzlei(k)
    return stat
