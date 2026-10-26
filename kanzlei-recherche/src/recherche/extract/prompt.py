"""Prompt-Aufbau für die LLM-Extraktion: gezielt reduzieren statt hinten abschneiden.

1. HTML → Text ohne Cookie-/Consent-Banner (crawl.html_zu_text)
2. Navigation und Fußzeile nur einmal: Zeilen, die auf mehreren Seiten derselben Kanzlei
   stehen, wandern in einen Abschnitt RAHMEN
3. Cookie-Zeilen und Textbausteine raus (Datenschutzerklärung, Haftung für Inhalte/Links,
   Urheberrecht, Streitschlichtung) – sie stehen oft im Impressum und verdrängen den Rest
4. Impressum-Schlüsselstellen (vertretungsberechtigt, Partner, Geschäftsführer, Inhaber,
   Berufsbezeichnung …) immer vollständig vorn
5. erkannte Personen (extract/personen.py) als kompakte Liste
6. Budget je Abschnitt; was ein Abschnitt nicht braucht, bekommen die übrigen
Ein Protokoll je Seite hält fest, wie viel Text in jeder Stufe übrig blieb.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from ..config import LlmConfig
from ..crawl import haupttext, html_zu_text
from ..models import Seite, Seitentyp
from ..textnorm import text_normalisieren
from .personen import (
    ErkanntePerson,
    Funktionen,
    erkenne_personen,
    lade_funktionen,
    personen_aus_json_ld,
)

ZEICHEN_JE_TOKEN = 3.0  # vorsichtige Schätzung für deutschen Text
RESERVE_SYSTEM_TOKEN = 700
RESERVE_ANTWORT_TOKEN = 2000

REIHENFOLGE = (Seitentyp.IMPRESSUM, Seitentyp.TEAM, Seitentyp.TEAM2, Seitentyp.KONTAKT, Seitentyp.START)
TEAM_TYPEN = (Seitentyp.TEAM, Seitentyp.TEAM2)

_COOKIE_ZEILE = re.compile(
    r"cookie|consent|alle akzeptieren|akzeptieren und|nur notwendige|einstellungen speichern|ablehnen|"
    r"externe medien|google analytics|matomo|tracking|youtube.{0,30}laden|google maps.{0,30}laden",
    re.I,
)
# Überschriften typischer Textbausteine: ab hier bis zur nächsten Schlüsselstelle weglassen
_BAUSTEIN = re.compile(
    r"^(?:\d+\.\s*)?(datenschutz(?:erklärung|hinweise|bestimmungen)?|hinweise zum datenschutz|privacy policy|"
    r"haftung für inhalte|haftung für links|haftungsausschluss|disclaimer|urheberrecht|"
    r"(?:eu-|verbraucher)?streitschlichtung|online-streitbeilegung|bildnachweise?|quellenangaben?|"
    r"verwendung von cookies|cookies)\b",
    re.I,
)
SCHLUESSEL = re.compile(
    r"vertretungsberechtigt|vertreten durch|vertretung|gesellschafter|geschäftsführ|geschaeftsfuehr|inhaber|"
    r"\bpartner(?:in|innen)?\b|berufsbezeichnung|verantwortlich|vorstand|kanzleiinhaber",
    re.I,
)
SCHLUESSEL_FENSTER = 4


@dataclass
class SeitenProtokoll:
    typ: Seitentyp
    url: str
    html_zeichen: int  # Größe des HTML
    text_zeichen: int  # nach HTML → Text (ohne Cookie-Banner)
    bereinigt_zeichen: int  # ohne Rahmen, Cookie-Zeilen, Textbausteine
    im_prompt_zeichen: int = 0
    schluesselstellen: int = 0
    personen: int = 0

    @property
    def gekuerzt(self) -> bool:
        return self.im_prompt_zeichen < self.bereinigt_zeichen


@dataclass
class Prompt:
    text: str
    budget: int
    seiten: list[SeitenProtokoll] = field(default_factory=list)
    rahmen_zeichen: int = 0
    rahmen_im_prompt: int = 0
    personen: list[ErkanntePerson] = field(default_factory=list)

    @property
    def gekuerzt(self) -> bool:
        return any(s.gekuerzt for s in self.seiten) or self.rahmen_im_prompt < self.rahmen_zeichen

    @property
    def geschaetzte_tokens(self) -> int:
        return round(len(self.text) / ZEICHEN_JE_TOKEN)

    def kuerzung_text(self) -> str | None:
        teile = [f"{s.typ.value} {s.im_prompt_zeichen}/{s.bereinigt_zeichen}" for s in self.seiten if s.gekuerzt]
        if self.rahmen_im_prompt < self.rahmen_zeichen:
            teile.append(f"rahmen {self.rahmen_im_prompt}/{self.rahmen_zeichen}")
        return ", ".join(teile) or None


def text_budget(cfg: LlmConfig) -> int:
    """llm.max_text_chars oder – bei 'auto' – passend zu num_ctx."""
    if isinstance(cfg.max_text_chars, int):
        return cfg.max_text_chars
    tokens = max(1000, cfg.num_ctx - RESERVE_SYSTEM_TOKEN - RESERVE_ANTWORT_TOKEN)
    return int(tokens * ZEICHEN_JE_TOKEN)


def _seitentext(s: Seite) -> str:
    return html_zu_text(s.html) if s.html else (text_normalisieren(s.text) or "")


def _norm(z: str) -> str:
    return " ".join(z.lower().split())


def rahmen_normiert(texte: list[str]) -> set[str]:
    """Normalisierte Zeilen, die auf mindestens drei Vierteln der Seiten (mind. 2) stehen –
    Navigation und Fußzeile. Nicht schon bei zwei von vier Seiten: Partnernamen stehen oft in
    Impressum und Teamseite."""
    if len(texte) < 2:
        return set()
    vorkommen = Counter(n for text in texte for n in {_norm(z) for z in text.splitlines() if z.strip()})
    schwelle = max(2, math.ceil(0.75 * len(texte)))
    return {n for n, k in vorkommen.items() if k >= schwelle}


def seiten_rahmen(seiten: dict[Seitentyp, Seite]) -> set[str]:
    """Rahmen über die Seiten einer Kanzlei (jede URL einmal)."""
    texte, gesehen = [], set()
    for s in seiten.values():
        if s is not None and s.url not in gesehen and (s.html or s.text):
            gesehen.add(s.url)
            texte.append(_seitentext(s))
    return rahmen_normiert(texte)


def _ohne_bausteine(zeilen: list[str]) -> list[str]:
    """Textbausteine (Datenschutz, Haftung …) bis zur nächsten Schlüsselstelle weglassen."""
    ergebnis, im_baustein = [], False
    for z in zeilen:
        if len(z) <= 80 and _BAUSTEIN.match(z.strip()):
            im_baustein = True
            continue
        if im_baustein and SCHLUESSEL.search(z) and len(z) <= 200:
            im_baustein = False
        if not im_baustein:
            ergebnis.append(z)
    return ergebnis


def schluesselstellen(zeilen: list[str], fenster: int = SCHLUESSEL_FENSTER) -> list[str]:
    """Zeilen um Schlüsselwörter (±fenster), zusammenhängende Bereiche verschmolzen."""
    nummern: set[int] = set()
    for i, z in enumerate(zeilen):
        if len(z) <= 300 and SCHLUESSEL.search(z):
            nummern.update(range(max(0, i - fenster), min(len(zeilen), i + fenster + 1)))
    ergebnis, vorher = [], None
    for i in sorted(nummern):
        if vorher is not None and i != vorher + 1:
            ergebnis.append("…")
        ergebnis.append(zeilen[i])
        vorher = i
    return ergebnis


def _kuerzen(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    stueck = text[:budget]
    return stueck[: stueck.rfind("\n")] if "\n" in stueck else stueck


def baue_prompt(seiten: dict[Seitentyp, Seite], budget: int, funktionen: Funktionen | None = None) -> Prompt:
    funktionen = funktionen or lade_funktionen()
    # ---- je URL einmal, in fester Reihenfolge
    geordnet: list[tuple[Seitentyp, Seite]] = []
    gesehen: set[str] = set()
    for typ in REIHENFOLGE:
        s = seiten.get(typ)
        if s is not None and s.url not in gesehen and (s.html or s.text):
            gesehen.add(s.url)
            geordnet.append((typ, s))
    roh = {typ: [z for z in _seitentext(s).splitlines() if z.strip()] for typ, s in geordnet}

    # ---- Rahmen: Navigation, Fußzeile (Zeilen auf ≥ ¾ der Seiten)
    rahmen_norm = rahmen_normiert(["\n".join(z) for z in roh.values()])
    rahmen: list[str] = []
    for zeilen in roh.values():
        for z in zeilen:
            if _norm(z) in rahmen_norm and z not in rahmen and not _COOKIE_ZEILE.search(z):
                rahmen.append(z)

    protokolle: list[SeitenProtokoll] = []
    abschnitte: list[tuple[str, str, int]] = []  # (Überschrift, Text, Anteil am Budget in %)
    personen: list[ErkanntePerson] = []
    for typ, s in geordnet:
        zeilen = roh[typ]
        p = SeitenProtokoll(typ, s.url, len(s.html or ""), len("\n".join(zeilen)), 0)
        if typ == Seitentyp.IMPRESSUM:
            # ohne Rahmen und Cookie-Zeilen: die Fußzeile steht sonst zweimal im Prompt
            ohne_rahmen = [z for z in zeilen if _norm(z) not in rahmen_norm and not (len(z) <= 160 and _COOKIE_ZEILE.search(z))]
            ohne_rahmen = _ohne_bausteine(ohne_rahmen)
            stellen = schluesselstellen(ohne_rahmen)
            p.schluesselstellen = sum(1 for z in ohne_rahmen if len(z) <= 300 and SCHLUESSEL.search(z))
            if stellen:
                abschnitte.append((f"IMPRESSUM – Schlüsselstellen ({s.url})", "\n".join(stellen), 100))
        eigene = [z for z in zeilen if _norm(z) not in rahmen_norm and not (len(z) <= 160 and _COOKIE_ZEILE.search(z))]
        eigene = _ohne_bausteine(eigene)
        if typ in TEAM_TYPEN:
            gefunden = erkenne_personen("\n".join(zeilen), funktionen, rahmen_norm)
            gefunden += personen_aus_json_ld(s.html, funktionen)
            p.personen = len(gefunden)
            for person in gefunden:
                if not any(set(person.name.lower().split()) == set(q.name.lower().split()) for q in personen):
                    personen.append(person)
            # Vollständiger Text bevorzugt (trafilatura verwirft in Kachel-Layouts oft die Namen);
            # Haupttext nur, wenn die Seite ihr Budget sprengt
            team_budget = budget * (30 if typ == Seitentyp.TEAM else 20) // 100
            if s.html and len("\n".join(eigene)) > team_budget and (kurz := haupttext(s.html)):
                kurz_zeilen = [z for z in kurz.splitlines() if z.strip() and _norm(z) not in rahmen_norm
                               and not (len(z) <= 160 and _COOKIE_ZEILE.search(z))]  # fmt: skip
                if 0.15 * len("\n".join(eigene)) <= len("\n".join(kurz_zeilen)) < len("\n".join(eigene)):
                    eigene = kurz_zeilen
        text = "\n".join(eigene)
        p.bereinigt_zeichen = len(text)
        protokolle.append(p)
        anteil = {Seitentyp.IMPRESSUM: 30, Seitentyp.TEAM: 30, Seitentyp.TEAM2: 20, Seitentyp.KONTAKT: 8,
                  Seitentyp.START: 12}[typ]  # fmt: skip
        abschnitte.append((f"{typ.value.upper()} ({s.url})", text, anteil))
    if personen:
        liste = "\n".join(f"{q.name} | {q.titel or '-'} | {q.funktion}" for q in personen)
        abschnitte.insert(1 if abschnitte and abschnitte[0][0].startswith("IMPRESSUM – ") else 0,
                          ("PERSONEN – Vorschlag der automatischen Erkennung (Name | Titel | Funktion); "
                           "alle prüfen und vollständig in das Feld personen übernehmen", liste, 100))
    rahmen_text = "\n".join(rahmen)
    if rahmen_text:
        abschnitte.append(("RAHMEN (Navigation und Fußzeile, auf mehreren Seiten gleich)", rahmen_text, 8))

    # ---- Budget: zuerst feste Anteile, Rest an die noch gekürzten Abschnitte in Reihenfolge
    zuteilung = [min(len(t), budget * a // 100) for _, t, a in abschnitte]
    rest = budget - sum(zuteilung) - sum(len(u) + 6 for u, _, _ in abschnitte)
    for i, (_, t, _) in enumerate(abschnitte):
        if rest <= 0:
            break
        mehr = min(rest, len(t) - zuteilung[i])
        zuteilung[i] += mehr
        rest -= mehr
    teile = []
    for (ueberschrift, t, _), n in zip(abschnitte, zuteilung, strict=True):
        stueck = _kuerzen(t, n)
        if stueck.strip():
            teile.append(f"### {ueberschrift}\n{stueck}")
        for p in protokolle:
            if ueberschrift == f"{p.typ.value.upper()} ({p.url})":
                p.im_prompt_zeichen = len(stueck)
    prompt = Prompt("\n\n".join(teile), budget, protokolle, len(rahmen_text), personen=personen)
    if rahmen_text:
        prompt.rahmen_im_prompt = len(_kuerzen(rahmen_text, zuteilung[-1]))
    return prompt
