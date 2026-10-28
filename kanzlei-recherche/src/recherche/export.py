"""Excel-Export mit den Blättern Kanzleien, Nicht gefunden, Dubletten, Lauf-Protokoll und – nur wenn
die Kanzlei-Erkennung welche gefunden hat – Keine Kanzlei."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from .extract.kanzlei import grund_aus_abweichung
from .models import Dublette, Kanzlei, LaufEintrag, NichtGefunden, Status
from .storage import Storage

BLATT_KANZLEIEN = "Kanzleien"
BLATT_NICHT_GEFUNDEN = "Nicht gefunden"
BLATT_KEINE_KANZLEI = "Keine Kanzlei"
BLATT_AUSSERHALB = "Außerhalb Bundesland"
BLATT_DUBLETTEN = "Dubletten"
BLATT_PROTOKOLL = "Lauf-Protokoll"

# Status-Dropdown gilt mindestens für so viele Zeilen (Platz für Nachträge)
DROPDOWN_MIN_ZEILEN = 1000

Spalte = tuple[str, int, Callable[[Any], Any]]  # Überschrift, Breite, Wert


def _ap(k: Kanzlei, feld: str) -> str | None:
    h = k.hauptansprechpartner
    return getattr(h, feld) if h else None


KANZLEI_SPALTEN: list[Spalte] = [
    ("Kanzleiname", 38, lambda k: k.kanzleiname),
    ("Rechtsform", 16, lambda k: k.rechtsform),
    ("Straße", 28, lambda k: k.strasse),
    ("Adresszusatz", 16, lambda k: k.adresszusatz),
    ("PLZ", 8, lambda k: k.plz),
    ("Ort", 20, lambda k: k.ort),
    ("Bundesland", 18, lambda k: k.bundesland),
    ("Telefon", 18, lambda k: k.telefon),
    ("E-Mail", 30, lambda k: k.email),
    ("Website", 30, lambda k: k.website),
    ("Website vorhanden", 10, lambda k: "ja" if k.website_vorhanden else "nein"),
    ("Hauptansprechpartner", 26, lambda k: _ap(k, "name")),
    ("Titel", 14, lambda k: _ap(k, "titel")),
    ("Funktion", 22, lambda k: _ap(k, "funktion")),
    ("Weitere Ansprechpartner", 45, lambda k: "; ".join(a.anzeige() for a in k.ansprechpartner[1:]) or None),
    ("Berufsträger (Anzahl)", 11, lambda k: k.berufstraeger_anzahl),
    ("Teamgröße", 10, lambda k: k.teamgroesse_anzeige),
    ("Größenklasse", 12, lambda k: k.teamgroesse_klasse),
    ("Teamgröße-Basis", 30, lambda k: k.teamgroesse_basis),
    ("Konfidenz", 10, lambda k: k.konfidenz.value),
    ("Abweichung", 45, lambda k: k.abweichung),
    ("Quell-URLs", 50, lambda k: "\n".join(k.quell_urls) or None),
    ("Abrufdatum", 12, lambda k: k.abrufdatum),
    ("Status", 14, lambda k: k.status.value),
    ("Domain", 26, lambda k: k.domain),
]  # fmt: skip

NICHT_GEFUNDEN_SPALTEN: list[Spalte] = [
    ("Domain", 30, lambda n: n.domain),
    ("Grund", 40, lambda n: n.grund),
    # Discovery-Daten zum Nachpflegen von Hand
    ("Name (Suche)", 34, lambda n: n.name),
    ("Straße", 26, lambda n: n.strasse),
    ("PLZ", 8, lambda n: n.plz),
    ("Ort", 18, lambda n: n.ort),
    ("Telefon", 18, lambda n: n.telefon),
    ("Website", 30, lambda n: n.website),
    ("URL", 40, lambda n: n.url),
    ("Zeitpunkt", 19, lambda n: n.zeitpunkt.replace(microsecond=0)),
]

# alle Felder wie im Blatt „Kanzleien", Grund vorn – zum Zurückholen von Hand
KEINE_KANZLEI_SPALTEN: list[Spalte] = [
    ("Grund", 50, lambda k: grund_aus_abweichung(k.abweichung)),
    *KANZLEI_SPALTEN,
]

DUBLETTEN_SPALTEN: list[Spalte] = [
    ("Behalten", 34, lambda d: d.behalten),
    ("Dublette", 34, lambda d: d.dublette),
    ("Kriterium", 12, lambda d: d.kriterium),
    ("Art", 16, lambda d: d.art.value),
    ("Kanzleiname", 38, lambda d: d.kanzleiname),
    ("Details", 40, lambda d: d.details),
    ("Zeitpunkt", 19, lambda d: d.zeitpunkt.replace(microsecond=0)),
]

PROTOKOLL_SPALTEN: list[Spalte] = [
    ("Lauf-ID", 18, lambda e: e.lauf_id),
    ("Schritt", 9, lambda e: e.schritt),
    ("Adapter", 9, lambda e: e.adapter),
    ("Ort", 18, lambda e: e.ort),
    ("Suchbegriff", 38, lambda e: e.suchbegriff),
    ("Trefferzahl", 11, lambda e: e.trefferzahl),
    ("Neue Kandidaten", 14, lambda e: e.neue_kandidaten),
    ("Anfragen", 10, lambda e: e.anfragen),
    ("Kosten", 9, lambda e: e.kosten),
    ("Laufzeit (s)", 11, lambda e: e.laufzeit_s),
    ("Fehler", 40, lambda e: e.fehler),
    ("Zeitpunkt", 19, lambda e: e.zeitpunkt.replace(microsecond=0)),
]

_KOPF_FONT = Font(bold=True, color="FFFFFF")
_KOPF_FILL = PatternFill("solid", fgColor="1F4E78")
_TEXT_SPALTEN = {"PLZ", "Telefon"}  # als Text, damit Excel nichts umformatiert


def _blatt(ws: Worksheet, spalten: Sequence[Spalte], zeilen: Sequence[Any]) -> None:
    for i, (titel, breite, _) in enumerate(spalten, start=1):
        zelle = ws.cell(row=1, column=i, value=titel)
        zelle.font = _KOPF_FONT
        zelle.fill = _KOPF_FILL
        zelle.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = breite
    for r, obj in enumerate(zeilen, start=2):
        for c, (titel, _, wert) in enumerate(spalten, start=1):
            zelle = ws.cell(row=r, column=c, value=wert(obj))
            if titel in _TEXT_SPALTEN:
                zelle.number_format = "@"
            elif titel in ("Abrufdatum",):
                zelle.number_format = "DD.MM.YYYY"
            elif titel == "Zeitpunkt":
                zelle.number_format = "DD.MM.YYYY HH:MM:SS"
            elif titel in ("Quell-URLs", "Abweichung"):
                zelle.alignment = Alignment(wrap_text=True, vertical="top")
    letzte_spalte = get_column_letter(len(spalten))
    ws.auto_filter.ref = f"A1:{letzte_spalte}{max(len(zeilen) + 1, 1)}"
    ws.freeze_panes = "A2"


def _status_dropdown(ws: Worksheet, spalten: Sequence[Spalte], anzahl: int) -> DataValidation:
    index = [s[0] for s in spalten].index("Status") + 1
    buchstabe = get_column_letter(index)
    werte = ",".join(s.value for s in Status if s not in (Status.KEINE_KANZLEI, Status.AUSSERHALB))  # automatisch
    dv = DataValidation(
        type="list",
        formula1=f'"{werte}"',
        allow_blank=True,
        showErrorMessage=True,
        errorTitle="Ungültiger Status",
        error=f"Erlaubt: {werte.replace(',', ' / ')}",
    )
    dv.add(f"{buchstabe}2:{buchstabe}{max(anzahl + 1, DROPDOWN_MIN_ZEILEN)}")
    ws.add_data_validation(dv)
    return dv


def build_workbook(
    kanzleien: Sequence[Kanzlei],
    nicht_gefunden: Sequence[NichtGefunden],
    dubletten: Sequence[Dublette],
    protokoll: Sequence[LaufEintrag],
) -> Workbook:
    keine = [k for k in kanzleien if k.status == Status.KEINE_KANZLEI]
    aussen = [k for k in kanzleien if k.status == Status.AUSSERHALB]
    kanzleien = [k for k in kanzleien if k.status not in (Status.KEINE_KANZLEI, Status.AUSSERHALB)]
    wb = Workbook()
    ws = wb.active
    ws.title = BLATT_KANZLEIEN
    _blatt(ws, KANZLEI_SPALTEN, kanzleien)
    _status_dropdown(ws, KANZLEI_SPALTEN, len(kanzleien))
    _blatt(wb.create_sheet(BLATT_NICHT_GEFUNDEN), NICHT_GEFUNDEN_SPALTEN, nicht_gefunden)
    if keine:
        _blatt(wb.create_sheet(BLATT_KEINE_KANZLEI), KEINE_KANZLEI_SPALTEN, keine)
    if aussen:  # bei bundesweitem Lauf später für ihr eigenes Bundesland richtig – nichts wird gelöscht
        _blatt(wb.create_sheet(BLATT_AUSSERHALB), KANZLEI_SPALTEN, aussen)
    _blatt(wb.create_sheet(BLATT_DUBLETTEN), DUBLETTEN_SPALTEN, dubletten)
    _blatt(wb.create_sheet(BLATT_PROTOKOLL), PROTOKOLL_SPALTEN, protokoll)
    return wb


def export_excel(storage: Storage, bundesland: str, pfad: str | Path) -> Path:
    """Schreibt alle Daten eines Bundeslandes in eine Excel-Datei und gibt den Pfad zurück."""
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    wb = build_workbook(
        storage.kanzleien(bundesland),
        storage.nicht_gefunden(bundesland),
        storage.dubletten(bundesland),
        storage.lauf_protokoll(bundesland),
    )
    wb.save(pfad)
    return pfad
