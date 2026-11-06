"""Ketten und Großgesellschaften (Prüfkatalog Abschnitt 9, `config/ketten.yaml`).

- **Großgesellschaft**: Name passt auf ein Muster oder die Domain auf eine Domain der Liste `grossgesellschaften`
  (BDO, EY, PwC, KPMG …). Zielkanzlei, keine Ausschlusskategorie (Entscheidung 30.10.2026).
- **Kette**: Mehrstandort-Gesellschaft – ein Eintrag in `ketten` (Zentrale + Standortmuster) oder automatisch
  erkannt: mindestens 3 Standorte (Zeilen) mit demselben Impressum-Anbieter, d. h. derselben Impressum-URL, oder
  einer Standort-Gruppe aus extract/standorte.py (gleiche Stammdomain, gleiches Telefon + Ansprechpartner) mit
  mindestens 3 Zeilen.
- Spalte „Kanzleityp" im Export: Großgesellschaft vor Kette vor Einzelkanzlei (Rechtsform Einzelkanzlei) vor
  Mittelstand (alle übrigen).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .models import Kanzlei

KETTE_AB_STANDORTEN = 3
# Subdomains einer Ketten-Domain, die keine Standorte sind
KEINE_STANDORTE = frozenset({"www", "jobs", "karriere", "career", "careers", "mail", "webmail", "portal", "login",
                             "cloud", "intranet", "shop", "blog", "news", "akademie", "service", "kundenportal"})


def _domain(eintrag: str) -> str:
    from .search.pipeline import normalize_domain

    return normalize_domain(eintrag) or eintrag.lower()
GROSSGESELLSCHAFT, KETTE, EINZELKANZLEI, MITTELSTAND = "Großgesellschaft", "Kette", "Einzelkanzlei", "Mittelstand"
_IMPRESSUM_PFAD = re.compile(r"impressum|imprint|anbieterkennzeichnung|rechtliche?s?|legal", re.IGNORECASE)


@dataclass
class Grossgesellschaft:
    name: str
    muster: list[re.Pattern] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)


@dataclass
class Kette:
    schluessel: str
    name: str
    zentrale: str
    uebersicht: str | None = None
    link_muster: re.Pattern | None = None
    standortmuster: str = "subdomain"  # subdomain | pfad | domain
    # feste Standortliste (Domains oder URLs) – wenn es keine Übersichtsseite gibt (wetreu: Standortsuche als JS-Widget)
    standorte_fest: list[str] = field(default_factory=list)
    # Telefon der Zentrale: an Standorten nur Rückfall (westerland.shbb.de, fixture-eval 03.11.2026)
    telefon: list[str] = field(default_factory=list)
    # Standortdaten statt Link-Liste (06.11.2026): ecovis_json (JSON im Quelltext der Übersicht), ey_json (eigene
    # JSON-Datei `daten_url`), hlb_karten (Karten mit Adresse, Telefon, Website im HTML) – siehe ketten_crawl.py
    format: str | None = None
    daten_url: str | None = None

    def enthaelt(self, domain: str | None, website: str | None = None) -> bool:
        """Zentrale oder Standort dieser Kette: Subdomain der Zentrale (außer jobs., karriere., mail. …), Eintrag der
        festen Standortliste oder Treffer des Link-Musters."""
        d = (domain or "").lower().removeprefix("www.")
        z = self.zentrale.lower().removeprefix("www.")
        if d and (d == z or (d.endswith("." + z) and d.split(".")[0] not in KEINE_STANDORTE)):
            return True
        if d and d in {_domain(x) for x in self.standorte_fest}:
            return True
        return bool(self.link_muster and website and self.link_muster.match(website))


@dataclass
class KettenConfig:
    grossgesellschaften: list[Grossgesellschaft] = field(default_factory=list)
    ketten: list[Kette] = field(default_factory=list)

    @classmethod
    def laden(cls, pfad: str | Path | None) -> KettenConfig:
        if not pfad or not Path(pfad).exists():
            return cls()
        return cls.laden_aus(yaml.safe_load(Path(pfad).read_text(encoding="utf-8-sig")) or {})

    @classmethod
    def laden_aus(cls, daten: dict) -> KettenConfig:
        gross = [Grossgesellschaft(g["name"], [re.compile(m, re.IGNORECASE) for m in g.get("muster") or []],
                                   [d.lower() for d in g.get("domains") or []])
                 for g in daten.get("grossgesellschaften") or []]
        ketten = [Kette(s, k.get("name") or s, k["zentrale"], k.get("uebersicht"),
                        re.compile(k["link_muster"], re.IGNORECASE) if k.get("link_muster") else None,
                        k.get("standortmuster") or "subdomain", [str(x) for x in k.get("standorte_fest") or []],
                        [str(x) for x in ([k["telefon"]] if isinstance(k.get("telefon"), (str, int)) else k.get("telefon") or [])],
                        k.get("format"), k.get("daten_url"))
                  for s, k in (daten.get("ketten") or {}).items()]
        return cls(gross, ketten)

    def grossgesellschaft(self, k: Kanzlei) -> str | None:
        domain = (k.domain or "").lower().removeprefix("www.")
        for g in self.grossgesellschaften:
            if any(domain == d or domain.endswith("." + d) for d in g.domains) or any(
                    m.search(k.kanzleiname or "") for m in g.muster):
                return g.name
        return None

    def kette(self, k: Kanzlei) -> Kette | None:
        return next((c for c in self.ketten if c.enthaelt(k.domain, k.website)), None)


def impressum_url(k: Kanzlei) -> str | None:
    """Die Impressum-Quelle der Zeile (gleiche URL bei mehreren Domains = gleicher Anbieter)."""
    for u in k.quell_urls:
        teile = urlsplit(u)
        if _IMPRESSUM_PFAD.search(teile.path):
            return f"{teile.netloc.lower().removeprefix('www.')}{teile.path.rstrip('/')}"
    return None


def _schluessel(k: Kanzlei) -> str:
    return k.domain or f"id:{k.id}"


def erkannte_ketten(kanzleien: Sequence[Kanzlei], cfg: KettenConfig | None = None) -> dict[str, str]:
    """Schlüssel (Domain bzw. id) → Name der Kette für alle Zeilen einer Kette (Zentrale eingeschlossen)."""
    from .extract.standorte import gruppen, spiegel_domains, zentrale

    cfg = cfg or KettenConfig()
    ergebnis: dict[str, str] = {}
    for k in kanzleien:
        if c := cfg.kette(k):
            ergebnis[_schluessel(k)] = c.name
    spiegel = {id(x) for liste in spiegel_domains(list(kanzleien)).values() for x in liste}
    for g in gruppen(list(kanzleien))[0]:
        standorte = [k for k in g if id(k) not in spiegel]  # weitere Domains derselben Kanzlei zählen nicht
        if len(standorte) >= KETTE_AB_STANDORTEN:
            z = zentrale(g)
            for k in g:
                ergebnis.setdefault(_schluessel(k), z.kanzleiname or z.domain or "")
    je_impressum: dict[str, list[Kanzlei]] = defaultdict(list)
    for k in kanzleien:
        if (u := impressum_url(k)) is not None:
            je_impressum[u].append(k)
    for u, liste in je_impressum.items():
        if len({_schluessel(k) for k in liste if id(k) not in spiegel}) >= KETTE_AB_STANDORTEN:
            name = next((k.kanzleiname for k in liste if k.kanzleiname), u.split("/")[0])
            for k in liste:
                ergebnis.setdefault(_schluessel(k), name)
    return ergebnis


def kanzleityp(k: Kanzlei, ketten: dict[str, str], cfg: KettenConfig | None = None) -> str:
    if (cfg or KettenConfig()).grossgesellschaft(k):
        return GROSSGESELLSCHAFT
    if _schluessel(k) in ketten:
        return KETTE
    if k.rechtsform == "Einzelkanzlei":
        return EINZELKANZLEI
    return MITTELSTAND


_LEITUNG = re.compile(r"\b(?:leiter(?:in)?|kanzleileitung|standortleit\w*|niederlassungsleit\w*|beratungsstellenleit\w*)\b",
                      re.IGNORECASE)


def ist_standort_leitung(funktion: str | None) -> bool:
    """„Leiter", „Kanzleileitung", „Standortleitung", „Niederlassungsleitung" (Prüfkatalog 9 / Q11)."""
    return bool(funktion and _LEITUNG.search(funktion))


def standortseite(domain: str | None, website: str | None = None, impressum_url: str | None = None) -> Kette | bool:
    """Liegt die Seite auf einem Standort einer Kette (Subdomain/Pfad laut ketten.yaml, nicht die Zentrale)?
    Ohne Eintrag: Impressum auf einem anderen Host derselben Stammdomain (www.kette.de/impressum für
    heide.kette.de) → True."""
    from .extract.kanzlei import stammdomain

    d = (domain or "").lower().removeprefix("www.")
    if c := config().kette(Kanzlei(lauf_bundesland="", domain=domain, website=website)):
        return c if d != c.zentrale.lower().removeprefix("www.") else False
    if impressum_url and d:
        host = urlsplit(impressum_url).netloc.lower().removeprefix("www.")
        return bool(host and host != d and stammdomain(host) == stammdomain(d) and d != stammdomain(d))
    return False


_aktiv = KettenConfig()


def setze_config(cfg: KettenConfig) -> None:
    global _aktiv
    _aktiv = cfg


def config() -> KettenConfig:
    return _aktiv
