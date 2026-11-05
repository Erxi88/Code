"""`recherche kuerzung -b SH`: Was schneidet der Prompt-Aufbau ab, und betrifft es die Kernfelder? (SH 05.11.2026:
137 Kanzleien „gekürzt" bei extract.)

Ohne LLM und ohne Netz: Für jede Kanzlei mit gecachten Seiten wird der Prompt wie bei extract gebaut (`baue_prompt`,
Budget aus llm.num_ctx); ist er gekürzt, wird für die gespeicherte Zeile geprüft, ob Kanzleiname, Straße, PLZ,
Telefon, E-Mail und Hauptansprechpartner im Prompt stehen, nur im abgeschnittenen Teil oder gar nicht im Seitentext.
Straße, PLZ, Telefon und E-Mail lesen die Regeln ohnehin aus dem vollständigen Seitentext – für sie ist nur der
Rückfall über das LLM betroffen; Name und Ansprechpartner kommen (auch) vom LLM.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Kanzlei
from ..storage import Storage
from .prompt import baue_prompt

KERNFELDER = ("kanzleiname", "strasse", "plz", "telefon", "email", "ansprechpartner")
IM_PROMPT, NUR_GEKUERZT, NICHT_IM_TEXT = "im Prompt", "nur im abgeschnittenen Teil", "nicht im Seitentext"
_NUR_LLM = {"kanzleiname", "ansprechpartner"}  # Felder, die (auch) vom LLM kommen


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold())


def _wert(k: Kanzlei, feld: str) -> str | None:
    if feld == "ansprechpartner":
        return k.ansprechpartner[0].name if k.ansprechpartner else None
    return getattr(k, feld)


def _enthalten(feld: str, wert: str, text: str) -> bool:
    if feld == "telefon":  # Gruppierung ist im Text anders als in der Zeile (+49 …)
        ziffern = re.sub(r"\D", "", wert)[-6:]
        return bool(ziffern) and ziffern in re.sub(r"\D", "", text)
    if feld == "ansprechpartner":
        return all(t in _norm(text) for t in _norm(wert).split())
    return _norm(wert) in _norm(text)


@dataclass
class KuerzungZeile:
    domain: str
    kuerzung: str  # „team 4800/12000, start 900/3000"
    felder: dict[str, str] = field(default_factory=dict)  # Feld → im Prompt / nur gekürzt / nicht im Text / leer

    @property
    def betroffen(self) -> list[str]:
        return [f for f, s in self.felder.items() if s == NUR_GEKUERZT]


@dataclass
class KuerzungBericht:
    geprueft: int = 0
    zeilen: list[KuerzungZeile] = field(default_factory=list)
    abschnitte: Counter = field(default_factory=Counter)  # Seitentyp → gekürzte Kanzleien

    def je_feld(self, status: str) -> Counter:
        return Counter(f for z in self.zeilen for f, s in z.felder.items() if s == status)


def pruefen(storage: Storage, bundesland: str, budget: int) -> KuerzungBericht:
    b = KuerzungBericht()
    for k in storage.kanzleien(bundesland):
        if k.kandidat_id is None:
            continue
        seiten = storage.kandidat_seiten(k.kandidat_id)
        if not seiten:
            continue
        b.geprueft += 1
        p = baue_prompt(seiten, budget)
        if not p.gekuerzt:
            continue
        voll = baue_prompt(seiten, 10**8).text
        z = KuerzungZeile(k.domain or str(k.kandidat_id), p.kuerzung_text() or "")
        for teil in z.kuerzung.split(", "):
            if teil:
                b.abschnitte[teil.split()[0]] += 1
        for feld in KERNFELDER:
            wert = _wert(k, feld)
            if not wert:
                z.felder[feld] = "leer"
            elif _enthalten(feld, wert, p.text):
                z.felder[feld] = IM_PROMPT
            elif _enthalten(feld, wert, voll):
                z.felder[feld] = NUR_GEKUERZT
            else:
                z.felder[feld] = NICHT_IM_TEXT
        b.zeilen.append(z)
    return b


def als_csv(b: KuerzungBericht, pfad: str | Path) -> Path:
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Domain", "gekürzt (im Prompt/bereinigt, Zeichen)", *KERNFELDER, "Kernfeld nur im gekürzten Teil"])
        for z in b.zeilen:
            w.writerow([z.domain, z.kuerzung, *(z.felder.get(f) for f in KERNFELDER), ", ".join(z.betroffen)])
    return pfad
