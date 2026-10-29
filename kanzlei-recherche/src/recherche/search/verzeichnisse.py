"""Stufe B: Branchenverzeichnisse (gelbeseiten, dasoertliche, elf880) und amtliches Steuerberaterverzeichnis.

Jeder Adapter bekommt einen Ort (search_ort) und liefert alle Einträge der Rubrik „Steuerberater" im
gemeinsamen Ergebnisformat (Name, Straße, PLZ, Ort, Telefon, Website, Quelle, Quell-ID). Ablauf:

- Ergebnisseiten aus `url_vorlage` ({rubrik} {ort} {plz} {seite}); `je_plz` sucht PLZ für PLZ
  (amtliches Verzeichnis), sonst je Ort; bis `max_seiten`, Ende bei einer Seite ohne neue Einträge.
- **Stark gedrosselt**: pause_s + Zufall 0..pause_jitter_s je Anfrage (Verzeichnisse 10–15 s, amtlich 30 s),
  robots.txt der Seite wird beachtet, jede Antwort in api_cache (Wiederaufnahme ohne neue Abrufe).
- Parser: zuerst der verzeichnisspezifische (`_parse`, aus den Seiten in tests/fixtures/verzeichnisse),
  sonst die schema.org-Daten der Seite (JSON-LD, Microdata: LocalBusiness/AccountingService …).
- Abgleich gegen den Bestand übernimmt die gemeinsame Dubletten-Logik (Name+PLZ, Adresse, Telefon);
  neue Einträge ohne Website bekommen danach eine Brave-Nachsuche (search/nachsuche.py).

Nutzungsbedingungen und robots.txt der Verzeichnisse vor dem Einsatz prüfen.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..config import VerzeichnisConfig
from ..robots import Robots
from .base import Cache, OrtsAdapter, SearchError, SearchHit

if TYPE_CHECKING:
    from .pipeline import Ort

log = logging.getLogger(__name__)

_FIRMEN_TYPEN = re.compile(r"LocalBusiness|AccountingService|ProfessionalService|LegalService|Organization|"
                           r"FinancialService|Attorney|Notary", re.I)


def _text(v) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value")
    return " ".join(str(v).split()) or None if v else None


def _json_ld_objekte(soup: BeautifulSoup) -> list[dict]:
    objekte: list[dict] = []

    def sammeln(o) -> None:
        if isinstance(o, list):
            for x in o:
                sammeln(x)
        elif isinstance(o, dict):
            objekte.append(o)
            for k in ("@graph", "itemListElement", "item", "mainEntity"):
                if k in o:
                    sammeln(o[k])

    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            sammeln(json.loads(s.string or s.get_text() or ""))
        except ValueError:
            continue
    return objekte


def schema_org_eintraege(html: str, basis_url: str, quelle: str) -> list[SearchHit]:
    """Firmeneinträge aus JSON-LD und Microdata (schema.org) – Name, Anschrift, Telefon, Website."""
    soup = BeautifulSoup(html or "", "lxml")
    roh: list[dict] = []
    for o in _json_ld_objekte(soup):
        typ = o.get("@type")
        if _FIRMEN_TYPEN.search(" ".join(typ) if isinstance(typ, list) else str(typ or "")):
            adr = o.get("address") or {}
            adr = adr[0] if isinstance(adr, list) and adr else adr
            roh.append({"name": _text(o.get("name")), "strasse": _text(adr.get("streetAddress")) if isinstance(adr, dict) else None,
                        "plz": _text(adr.get("postalCode")) if isinstance(adr, dict) else None,
                        "ort": _text(adr.get("addressLocality")) if isinstance(adr, dict) else None,
                        "telefon": _text(o.get("telephone")), "website": _text(o.get("url")),
                        "id": _text(o.get("@id")) or _text(o.get("url"))})
    for el in soup.find_all(attrs={"itemtype": _FIRMEN_TYPEN}):
        def prop(name, wurzel=el):
            t = wurzel.find(attrs={"itemprop": name})
            if t is None:
                return None
            if name == "url":
                return _text(t.get("content") or t.get("href") or t.get_text(" "))
            return _text(t.get("content") or t.get_text(" ") or t.get("href"))
        adr = el.find(attrs={"itemprop": "address"}) or el
        roh.append({"name": prop("name"), "strasse": prop("streetAddress", adr), "plz": prop("postalCode", adr),
                    "ort": prop("addressLocality", adr), "telefon": prop("telephone"), "website": prop("url"),
                    "id": el.get("itemid") or el.get("id")})
    hits, gesehen = [], set()
    for e in roh:
        if not e["name"]:
            continue
        website = e["website"]
        if website and urlsplit(urljoin(basis_url, website)).netloc == urlsplit(basis_url).netloc:
            website = None  # Link auf den Verzeichniseintrag selbst ist keine Kanzlei-Website
        quell_id = e["id"] or f"{e['name']}|{e['plz'] or ''}|{e['strasse'] or ''}"
        if quell_id in gesehen:
            continue
        gesehen.add(quell_id)
        hits.append(SearchHit(quelle=quelle, quell_id=quell_id, name=e["name"], strasse=e["strasse"], plz=e["plz"],
                              ort=e["ort"], telefon=e["telefon"], website=website, roh_url=basis_url))
    return hits


class _Verzeichnis(OrtsAdapter):
    """Gemeinsamer Ablauf: Ergebnisseiten abrufen (gedrosselt, robots.txt, Cache) und parsen."""

    cfg: VerzeichnisConfig

    def __init__(self, cfg: VerzeichnisConfig | None = None, cache: Cache | None = None,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None, **_):
        super().__init__(cfg or VerzeichnisConfig(), cache)
        self._client = client
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._robots: dict[str, Robots | None] = {}
        self.melden: Callable[[str], None] | None = None

    def nicht_konfiguriert_grund(self) -> str | None:
        if not self.cfg.url_vorlage:
            return (f"url_vorlage fehlt (discovery.quellen.{self.name}.url_vorlage) – Seitenaufbau aus "
                    "tests/fixtures/verzeichnisse übernehmen, Nutzungsbedingungen prüfen")
        return super().nicht_konfiguriert_grund()

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.cfg.timeout_s, follow_redirects=True, headers={
                "User-Agent": self.cfg.user_agent, "Accept-Language": "de-DE,de;q=0.9"})
        return self._client

    # -------------------------------------------------------------- Abruf

    def _erlaubt(self, url: str) -> bool:
        teile = urlsplit(url)
        host = f"{teile.scheme}://{teile.netloc}"
        if host not in self._robots:
            try:
                resp = self.client.get(f"{host}/robots.txt")
                self._robots[host] = Robots.parse(resp.text) if resp.status_code == 200 else None
            except httpx.HTTPError:
                self._robots[host] = None  # nicht abrufbar: wie beim Crawler lesen, echte Verbote gelten
        regeln = self._robots[host]
        return regeln is None or regeln.erlaubt(self.cfg.user_agent, url)

    def _hole(self, url: str) -> str:
        if self.cache is not None and (gecacht := self.cache.cache_get(self.name, url)) is not None:
            return gecacht["html"]
        if not self._erlaubt(url):
            raise SearchError(f"{self.name}: robots.txt verbietet {url}")
        letzter = ""
        for versuch in range(1, self.cfg.retries + 2):
            if self.anfragen:
                self._sleep(self.cfg.pause_s + self._rng.uniform(0, self.cfg.pause_jitter_s))
            self._anfrage_zaehlen()
            try:
                resp = self.client.get(url)
            except httpx.HTTPError as e:
                letzter = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    if self.cache is not None:
                        self.cache.cache_put(self.name, url, {"html": resp.text})
                    return resp.text
                if resp.status_code == 404:
                    return ""
                letzter = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    raise SearchError(f"{self.name}: {letzter} für {url}")
            if self.melden:
                self.melden(f"{self.name}: {letzter} – Versuch {versuch}/{self.cfg.retries + 1}")
            if versuch <= self.cfg.retries:
                self._sleep(self.cfg.backoff_s * 2 ** (versuch - 1))
        raise SearchError(f"{self.name}: {letzter} für {url}")

    def _url(self, ort: Ort, plz: str, seite: int) -> str:
        return self.cfg.url_vorlage.format(rubrik=quote(self.cfg.rubrik), ort=quote(ort.gemeinde or ort.name),
                                           plz=plz, seite=seite)

    def _parse(self, html: str, url: str) -> list[SearchHit] | None:
        """Verzeichnisspezifischer Parser (aus den Fixture-Seiten); None = schema.org-Rückfall."""
        return None

    def search_ort(self, ort: Ort) -> list[SearchHit]:
        einheiten = list(ort.alle_plz) if self.cfg.je_plz else [ort.plz]
        alle: dict[str, SearchHit] = {}
        for plz in einheiten:
            for seite in range(1, self.cfg.max_seiten + 1):
                url = self._url(ort, plz, seite)
                html = self._hole(url)
                hits = self._parse(html, url)
                if hits is None:
                    hits = schema_org_eintraege(html, url, self.name)
                neu = [h for h in hits if h.quell_id not in alle]
                for h in neu:
                    alle[h.quell_id] = h
                if not neu or "{seite}" not in (self.cfg.url_vorlage or ""):
                    break
        return list(alle.values())


class GelbeSeitenAdapter(_Verzeichnis):
    name = "gelbeseiten"


class DasOertlicheAdapter(_Verzeichnis):
    name = "dasoertliche"


class Elf880Adapter(_Verzeichnis):
    name = "elf880"


class StbVerzeichnisAdapter(_Verzeichnis):
    """Amtliches Steuerberaterverzeichnis: Suche je PLZ des Ortes (Grundliste), 30 s Pause."""

    name = "stb_verzeichnis"
