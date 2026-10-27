"""Kanzleiseiten-Fixtures: abholen (fixture-add) und Trefferquote messen (fixture-eval).

Ordner je Domain (tests/fixtures/kanzleiseiten/<beispiele|echt>/<domain>/):
  start.html, impressum.html, kontakt.html, team.html   – Rohseiten (fehlende Seiten fehlen)
  meta.json                                             – URLs je Seitentyp, Abrufdatum
  erwartet.json                                         – von Hand ausgefüllte Sollwerte
  llm/<modell>.json                                     – aufgezeichnete LLM-Antworten (fixture-eval --live)

Echte Kanzleiseiten liegen nur lokal unter echt/ (.gitignore); committet werden nur die
fiktiven Beispiele. Name und Ansprechpartner werden offline ausschließlich über
aufgezeichnete Antworten bewertet – ohne Aufzeichnung werden diese Felder übersprungen.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean

import httpx

from .config import CrawlConfig
from .crawl import Crawler, html_zu_text
from .extract.base import Extractor, ExtraktionsAntwort, LlmPerson
from .extract.lauf import regeln_fuer
from .extract.merge import gleiche_person, zusammenfuehren
from .extract.rules import Ausschluss
from .models import Kandidat, Kanzlei, Seite, Seitentyp
from .normalize import _ortsname, normalize_rechtsform
from .search.dedupe import adresse_key, telefon_key
from .storage import Storage
from .textnorm import text_normalisieren

DATEIEN = {
    Seitentyp.START: "start.html",
    Seitentyp.IMPRESSUM: "impressum.html",
    Seitentyp.KONTAKT: "kontakt.html",
    Seitentyp.TEAM: "team.html",
    Seitentyp.TEAM2: "team2.html",
}
STATUS_OFFEN = "offen"
STATUS_FERTIG = "fertig"
ERWARTET_VORLAGE = {
    "_status": STATUS_OFFEN,
    "_hinweis": "Sollwerte von Hand eintragen, dann _status auf 'fertig' setzen. "
    "null = Angabe steht nicht auf der Website. Feld entfernen = nicht bewerten.",
    "kanzleiname": None,
    "rechtsform": None,
    "strasse": None,
    "plz": None,
    "ort": None,
    "telefon": None,
    "email": None,
    "erster_ansprechpartner": None,
    "berufstraeger_anzahl": None,
    "teamgroesse": None,
    "teamgroesse_klasse": None,
}

# Reihenfolge der Ausgabe; "adresse" = Straße, PLZ und Ort zusammen richtig
FELDER = ["kanzleiname", "adresse", "strasse", "plz", "ort", "telefon", "email", "erster_ansprechpartner",
          "rechtsform", "berufstraeger_anzahl", "teamgroesse"]  # fmt: skip
KERNFELDER = ["kanzleiname", "adresse", "telefon", "erster_ansprechpartner"]
# Felder, die vom LLM abhängen – ohne aufgezeichnete Antwort nicht bewertet
LLM_FELDER = {"kanzleiname", "erster_ansprechpartner", "rechtsform", "berufstraeger_anzahl", "teamgroesse"}


# Vorsilbe handgeschriebener Beispielantworten – echte Aufzeichnungen überschreiben sie nie
HANDGESCHRIEBEN = "handgeschrieben_"


def modell_datei(modell: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", modell) + ".json"


@dataclass
class Fixture:
    pfad: Path
    domain: str
    meta: dict
    erwartet: dict
    seiten: dict[Seitentyp, Seite]

    @classmethod
    def laden(cls, pfad: Path) -> Fixture:
        meta = json.loads((pfad / "meta.json").read_text(encoding="utf-8-sig")) if (pfad / "meta.json").exists() else {}
        domain = meta.get("domain") or pfad.name
        erwartet_pfad = pfad / "erwartet.json"
        erwartet = json.loads(erwartet_pfad.read_text(encoding="utf-8-sig")) if erwartet_pfad.exists() else {}
        abgerufen = datetime.fromisoformat(meta["abgerufen_am"]) if meta.get("abgerufen_am") else datetime.now()
        seiten = {}
        for typ, datei in DATEIEN.items():
            if (pfad / datei).exists():
                html = text_normalisieren((pfad / datei).read_text(encoding="utf-8-sig")) or ""
                url = (meta.get("urls") or {}).get(typ.value) or f"https://{domain}/{'' if typ == Seitentyp.START else typ.value}"
                seiten[typ] = Seite(url=url, domain=domain, seitentyp=typ, http_status=200, html=html,
                                    text=html_zu_text(html), final_url=url, abgerufen_am=abgerufen)  # fmt: skip
        return cls(pfad, domain, meta, erwartet, seiten)

    @property
    def fertig(self) -> bool:
        return self.erwartet.get("_status") == STATUS_FERTIG

    def aufzeichnung(self, modell: str) -> ExtraktionsAntwort | None:
        """Echte Aufzeichnung (llm/<modell>.json), sonst handgeschriebene (llm/handgeschrieben_<modell>.json)."""
        for datei in (modell_datei(modell), HANDGESCHRIEBEN + modell_datei(modell)):
            p = self.pfad / "llm" / datei
            if p.exists():
                return ExtraktionsAntwort(**json.loads(p.read_text(encoding="utf-8-sig")))
        return None

    def aufzeichnen(self, antwort: ExtraktionsAntwort, modell: str) -> Path:
        p = self.pfad / "llm" / modell_datei(modell)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(antwort.model_dump_json(indent=1) + "\n", encoding="utf-8")
        return p

    def modelle(self) -> list[str]:
        ordner = self.pfad / "llm"
        if not ordner.exists():
            return []
        return sorted({json.loads(p.read_text(encoding="utf-8-sig"))["modell"] for p in ordner.glob("*.json")})


def fixtures_finden(ordner: list[Path]) -> list[Fixture]:
    result = []
    for o in ordner:
        if not o.exists():
            continue
        for pfad in sorted(p for p in o.iterdir() if p.is_dir() and any(p.glob("*.html"))):
            result.append(Fixture.laden(pfad))
    return result


# ---------------------------------------------------------------------- fixture-add


async def fixture_holen(
    domain: str, cfg: CrawlConfig, ziel: Path, client: httpx.AsyncClient | None = None
) -> tuple[Path, str | None]:
    """Lädt Start-, Impressum-, Kontakt- und Teamseite (robots.txt und Drosselung wie beim Crawl)
    und legt sie mit meta.json und einer erwartet.json-Vorlage ab. Gibt (Ordner, Fehlergrund) zurück."""
    with Storage(":memory:") as st:
        crawler = Crawler(cfg, st, client=client)
        try:
            erg = await crawler.crawl_kandidat(
                Kandidat(id=0, bundesland="XX", domain=domain, website=f"https://{domain}/")
            )
        finally:
            await crawler.schliessen()
    pfad = ziel / domain
    pfad.mkdir(parents=True, exist_ok=True)
    urls = {}
    for typ, abruf in erg.seiten.items():
        (pfad / DATEIEN[typ]).write_text(abruf.html or "", encoding="utf-8")
        urls[typ.value] = abruf.final_url or abruf.url
    playwright = {typ.value: a.playwright for typ, a in erg.seiten.items() if a.playwright}
    meta = {"domain": domain, "urls": urls, "abgerufen_am": datetime.now().replace(microsecond=0).isoformat(),
            "crawl_hinweis": erg.grund, **({"playwright": playwright} if playwright else {})}  # fmt: skip
    (pfad / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if not (pfad / "erwartet.json").exists():
        (pfad / "erwartet.json").write_text(json.dumps(ERWARTET_VORLAGE, ensure_ascii=False, indent=1) + "\n",
                                            encoding="utf-8")  # fmt: skip
    return pfad, erg.grund


def fixture_holen_sync(domain: str, cfg: CrawlConfig, ziel: Path, client=None) -> tuple[Path, str | None]:
    return asyncio.run(fixture_holen(domain, cfg, ziel, client))


# ---------------------------------------------------------------------- fixture-eval


def _name_norm(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower().replace("&", " und ").replace("+", " und ")
    return re.sub(r"[^a-z0-9äöüß]", "", t)


def istwerte(k: Kanzlei) -> dict:
    return {
        "kanzleiname": k.kanzleiname, "rechtsform": k.rechtsform, "strasse": k.strasse, "plz": k.plz,
        "ort": k.ort, "telefon": k.telefon, "email": k.email,
        "erster_ansprechpartner": k.hauptansprechpartner.name if k.hauptansprechpartner else None,
        "berufstraeger_anzahl": k.berufstraeger_anzahl, "teamgroesse": k.teamgroesse,
        "teamgroesse_klasse": k.teamgroesse_klasse.value if k.teamgroesse_klasse else None,
    }  # fmt: skip


def feld_richtig(feld: str, ist: dict, soll: dict) -> bool | None:
    """True/False; None, wenn das Feld nicht bewertet wird (fehlt in erwartet.json)."""
    if feld == "adresse":
        teile = [feld_richtig(f, ist, soll) for f in ("strasse", "plz", "ort")]
        return None if None in teile else all(teile)
    if feld == "teamgroesse":
        if "teamgroesse" not in soll and "teamgroesse_klasse" not in soll:
            return None
        if soll.get("teamgroesse") is not None:
            return ist["teamgroesse"] == soll["teamgroesse"]
        return ist["teamgroesse"] is None and ist["teamgroesse_klasse"] == soll.get("teamgroesse_klasse")
    if feld not in soll:
        return None
    a, b = ist.get(feld), soll[feld]
    if a is None or b is None:
        return a is None and b is None
    vergleich = {
        "kanzleiname": lambda: _name_norm(a) == _name_norm(b),
        "rechtsform": lambda: _name_norm(normalize_rechtsform(a)) == _name_norm(normalize_rechtsform(b)),
        "strasse": lambda: adresse_key(a, "00000") == adresse_key(b, "00000"),
        "plz": lambda: str(a) == str(b),
        "ort": lambda: _ortsname(a) == _ortsname(b),
        "telefon": lambda: telefon_key(a) == telefon_key(b),
        "email": lambda: a.lower() == b.lower(),
        # Liste zulässiger Namen erlaubt (gleichrangige Partner): Treffer, wenn einer passt
        "erster_ansprechpartner": lambda: any(gleiche_person(a, n) for n in (b if isinstance(b, list) else [b])),
        "berufstraeger_anzahl": lambda: int(a) == int(b),
    }
    return vergleich[feld]()


def _anzeige(feld: str, ist: dict, soll: dict) -> tuple[object, object]:
    """(Ist, Soll) für die Fehlerliste – bei der Teamgröße Zahl und Klasse, damit nie „None / None" steht."""
    if feld == "adresse":
        teile = ("strasse", "plz", "ort")
        return {k: ist.get(k) for k in teile}, {k: soll.get(k) for k in teile}
    if feld == "teamgroesse":
        def fmt(d: dict) -> str:
            return f"Zahl {d.get('teamgroesse')}, Klasse {d.get('teamgroesse_klasse')}"

        return fmt(ist), fmt(soll)
    return ist.get(feld), soll.get(feld)


@dataclass
class Abweichung:
    domain: str
    feld: str
    ist: object
    soll: object


@dataclass
class EvalErgebnis:
    modell: str | None
    fixtures: int = 0
    richtig: dict[str, int] = field(default_factory=dict)
    bewertet: dict[str, int] = field(default_factory=dict)
    fehler: list[Abweichung] = field(default_factory=list)
    laufzeiten: list[float] = field(default_factory=list)
    prompt_tokens: list[int] = field(default_factory=list)
    antwort_tokens: list[int] = field(default_factory=list)
    ohne_aufzeichnung: list[str] = field(default_factory=list)

    def quote(self, feld: str) -> float | None:
        n = self.bewertet.get(feld, 0)
        return self.richtig.get(feld, 0) / n if n else None

    @property
    def mittlere_laufzeit_s(self) -> float | None:
        return mean(self.laufzeiten) if self.laufzeiten else None

    @property
    def mittlere_tokens(self) -> tuple[float | None, float | None, float | None]:
        """(Prompt, Antwort, gesamt) im Mittel je Kanzlei."""
        if not self.prompt_tokens and not self.antwort_tokens:
            return None, None, None
        p = mean(self.prompt_tokens) if self.prompt_tokens else 0.0
        a = mean(self.antwort_tokens) if self.antwort_tokens else 0.0
        return p, a, p + a


def auswerten(
    fixtures: list[Fixture],
    ausschluss: Ausschluss,
    modell: str | None,
    extractor: Extractor | None = None,
    plz_pruefen=None,
) -> EvalErgebnis:
    """Trefferquote je Feld. Mit `extractor` (live) werden die Antworten zugleich aufgezeichnet."""
    erg = EvalErgebnis(modell)
    for fx in fixtures:
        if not fx.fertig:
            continue
        erg.fixtures += 1
        antwort = None
        if extractor is not None:
            antwort = extractor.extract(fx.seiten)
            fx.aufzeichnen(antwort, modell or antwort.modell)
        elif modell:
            antwort = fx.aufzeichnung(modell)
        if antwort is None:
            erg.ohne_aufzeichnung.append(fx.domain)
        else:
            erg.laufzeiten.append(antwort.laufzeit_s)
            if antwort.prompt_tokens is not None:
                erg.prompt_tokens.append(antwort.prompt_tokens)
            if antwort.antwort_tokens is not None:
                erg.antwort_tokens.append(antwort.antwort_tokens)
        regel = regeln_fuer(fx.seiten, ausschluss, fx.domain)
        start = fx.seiten.get(Seitentyp.START)
        kandidat = Kandidat(bundesland="XX", domain=fx.domain, website=start.url if start else None)
        kanzlei = zusammenfuehren(kandidat, [], fx.seiten, regel, antwort.ergebnis if antwort else None,
                                  plz_pruefen, ausschluss)  # fmt: skip
        ist = istwerte(kanzlei)
        for feld in FELDER:
            if feld in LLM_FELDER and antwort is None:
                continue
            richtig = feld_richtig(feld, ist, fx.erwartet)
            if richtig is None:
                continue
            erg.bewertet[feld] = erg.bewertet.get(feld, 0) + 1
            if richtig:
                erg.richtig[feld] = erg.richtig.get(feld, 0) + 1
            else:
                erg.fehler.append(Abweichung(fx.domain, feld, *_anzeige(feld, ist, fx.erwartet)))
    return erg


def aufgezeichnete_modelle(fixtures: list[Fixture]) -> list[str]:
    return sorted({m for fx in fixtures for m in fx.modelle()})


# ---------------------------------------------------------------------- fixture-prompt


@dataclass
class PromptDiagnose:
    """Was vom HTML einer Fixture beim Modell ankommt – nur Größen und Zählungen, keine Inhalte."""

    domain: str
    prompt: object  # extract.prompt.Prompt
    system_zeichen: int
    links: dict  # Seitentyp → [(Punkte, Pfad, Linktext, gewählt)]
    personen_je_art: dict[str, int]
    vertreter: int
    berufsbezeichnung: str | None
    mitarbeiter_beleg: str | None
    regel_felder: dict[str, bool]
    aufzeichnung: ExtraktionsAntwort | None
    vertreter_namen: list[str] = field(default_factory=list)
    # Berufsträger nach der Zusammenführung (mit Aufzeichnung, sonst nur Regeln): (Name, Funktion, Herkunft)
    berufstraeger: list[tuple[str, str, str]] = field(default_factory=list)
    erster_ansprechpartner: str | None = None
    versteckt: dict = field(default_factory=dict)  # Seitentyp → personen.VersteckteQuellen (Teamseiten)
    vertretung_protokoll: list[str] = field(default_factory=list)
    # alle Personen nach der Zusammenführung: (Name, Funktion, Herkunft, Berufsträger?)
    personen: list[tuple[str, str, str, bool]] = field(default_factory=list)
    nur_im_rahmen: list[str] = field(default_factory=list)  # Personen, die nur in Navigation/Fußzeile stehen
    playwright: dict = field(default_factory=dict)  # meta.json: Ergebnis des Playwright-Nachlaufs je Seite

    @property
    def prompt_zeichen(self) -> int:
        return self.system_zeichen + len(self.prompt.text)


def fixture_finden(domain: str, ordner: list[Path]) -> Fixture | None:
    for o in ordner:
        if (o / domain).is_dir():
            return Fixture.laden(o / domain)
    return None


def prompt_diagnose(fx: Fixture, llm_cfg, ausschluss: Ausschluss, modell: str | None = None) -> PromptDiagnose:
    from urllib.parse import urlsplit

    from .crawl import link_kandidaten
    from .extract.merge import (
        _text,
        ansprechpartner,
        gleiche_person,
        ist_berufstraeger,
        ist_leitung,
        personen_vereinigen,
        vertretung,
    )
    from .extract.ollama import SYSTEM_PROMPT
    from .extract.personen import (
        erkenne_personen,
        mitarbeiterzahl,
        personen_aus_json_ld,
        versteckte_quellen,
    )
    from .extract.prompt import baue_prompt, seiten_rahmen, text_budget

    prompt = baue_prompt(fx.seiten, text_budget(llm_cfg))
    links: dict = {}
    start = fx.seiten.get(Seitentyp.START)
    if start and start.html:
        gewaehlt = {s.url for s in fx.seiten.values()}
        for typ, liste in link_kandidaten(start.html, start.url, fx.domain).items():
            links[typ] = [(p, urlsplit(u).path or "/", text[:40], u in gewaehlt) for p, _, u, text in liste[:3]]
    arten = {"Berufsträger": 0, "Leitung (ohne Berufstitel)": 0, "weitere Mitarbeitende": 0}

    for q in prompt.personen:
        lp = LlmPerson(name=q.name, titel=q.titel, funktion=q.funktion)
        art = "Berufsträger" if ist_berufstraeger(lp) else "Leitung (ohne Berufstitel)" if ist_leitung(lp) else "weitere Mitarbeitende"
        arten[art] += 1
    impressum = _text(fx.seiten[Seitentyp.IMPRESSUM]) if Seitentyp.IMPRESSUM in fx.seiten else ""
    fremd = ausschluss.fremd_zeilen(impressum.splitlines())
    rahmen = seiten_rahmen(fx.seiten)
    v = vertretung(impressum, fremd, rahmen)
    vertreter, bezeichnung = v.namen, v.bezeichnung
    angabe = mitarbeiterzahl([_text(s) for s in fx.seiten.values()])
    regel = regeln_fuer(fx.seiten, ausschluss, fx.domain)
    felder = {k: bool(getattr(regel, k)) for k in ("strasse", "adresszusatz", "plz", "ort", "telefon", "email")} if regel else {}
    aufzeichnung = fx.aufzeichnung(modell) if modell else None
    # Berufsträger wie in der Zusammenführung – mit Herkunft, um Fehlzählungen zu finden
    llm = aufzeichnung.ergebnis if aufzeichnung else None
    erkannte: list = []
    versteckt = {}
    nur_im_rahmen: list[str] = []
    for typ in (Seitentyp.TEAM, Seitentyp.TEAM2):
        if (s := fx.seiten.get(typ)) is not None:
            versteckt[typ] = versteckte_quellen(s.html, _text(s))
            for q in erkenne_personen(_text(s), rahmen=rahmen) + personen_aus_json_ld(s.html):
                if not any(gleiche_person(q.name, x.name) for x in erkannte):
                    erkannte.append(q)
            for q in erkenne_personen(_text(s)):  # ohne Rahmen-Filter: was ginge nur dort verloren?
                if not any(gleiche_person(q.name, x.name) for x in erkannte) and q.name not in nur_im_rahmen:
                    nur_im_rahmen.append(f"{q.name} | {q.funktion}")
    aps = ansprechpartner(llm, erkannte, impressum or None, fremd, rahmen)
    berufstraeger = []
    for a in aps:
        if not ist_berufstraeger(a):
            continue
        herkunft = [quelle for quelle, treffer in (
            ("LLM", llm and any(gleiche_person(a.name, x.name) and ist_berufstraeger(x) for x in llm.personen)),
            ("Teamseite", any(gleiche_person(a.name, x.name) and ist_berufstraeger(LlmPerson(name=x.name, titel=x.titel,
                                                                                            funktion=x.funktion))
                              for x in erkannte)),
            ("Impressum-Berufsbezeichnung", "(laut Impressum)" in (a.funktion or "")),
        ) if treffer]  # fmt: skip
        berufstraeger.append((a.name, a.funktion or "", ", ".join(herkunft) or "Titel"))
    # alle Personen wie in ansprechpartner() zusammengeführt – auch die, die nicht als Ansprechpartner zählen
    roh = [LlmPerson(name=q.name, titel=q.titel, funktion=q.funktion) for q in erkannte]
    roh += [LlmPerson(name=n, funktion=v.funktionen.get(n) or "laut Impressum vertretungsberechtigt") for n in vertreter]
    roh += list(llm.personen) if llm else []
    personen = []
    for q in personen_vereinigen(roh):
        herkunft = [quelle for quelle, treffer in (
            ("Teamseite", any(gleiche_person(q.name, x.name) for x in erkannte)),
            ("Impressum", any(gleiche_person(q.name, n) for n in vertreter)),
            ("LLM", llm and any(gleiche_person(q.name, x.name) for x in llm.personen)),
        ) if treffer]  # fmt: skip
        personen.append((q.name, q.funktion or "", ", ".join(herkunft), ist_berufstraeger(q)))
    return PromptDiagnose(
        fx.domain, prompt, len(SYSTEM_PROMPT), links, arten, len(vertreter), bezeichnung,
        angabe.beleg if angabe else None, felder, aufzeichnung, vertreter, berufstraeger,
        aps[0].name if aps else None, versteckt, v.protokoll, personen, nur_im_rahmen,
        fx.meta.get("playwright") or {},
    )  # fmt: skip
