"""Stufe B: Branchenverzeichnisse (gelbeseiten, dasoertliche, elf880) und amtliches Steuerberaterverzeichnis.

Jeder Adapter bekommt einen Ort (search_ort) und liefert alle Einträge der Rubrik „Steuerberater" im
gemeinsamen Ergebnisformat (Name, Straße, PLZ, Ort, Telefon, Website, Quelle, Quell-ID). Ablauf:

- Ergebnisseiten aus `url_vorlage` ({rubrik} {ort} {ort_klein} {plz} {seite} {start}), mit `formular` per POST;
  `je_plz` sucht PLZ für PLZ (amtliches Verzeichnis), sonst je Ort; bis `max_seiten`, Ende bei einer Seite
  ohne neue Einträge.
- **Stark gedrosselt**: pause_s + Zufall 0..pause_jitter_s je Anfrage (Verzeichnisse 10–15 s, amtlich 30 s),
  robots.txt der Seite wird beachtet, jede Antwort in api_cache (Wiederaufnahme ohne neue Abrufe).
- Parser: zuerst der verzeichnisspezifische (`_parse`, aus den Seiten in tests/fixtures/verzeichnisse),
  sonst die schema.org-Daten der Seite (JSON-LD, Microdata: LocalBusiness/AccountingService …).
  gelbeseiten: eigene Kacheln (Telefon/Website base64 in data-Attributen); dasoertliche und 11880: schema.org;
  amtliches Verzeichnis: Karten je Berufsträger und je Gesellschaft, zu Kanzleien gruppiert (stbv_kanzleien).
- Branchenverzeichnisse (`nur_ergaenzen`): nur Telefon, Adresse, Website bekannter Kandidaten ohne Website.
- Abgleich gegen den Bestand übernimmt die gemeinsame Dubletten-Logik (Name+PLZ, Adresse, Telefon);
  neue Einträge ohne Website bekommen danach eine Brave-Nachsuche (search/nachsuche.py).

Nutzungsbedingungen und robots.txt der Verzeichnisse vor dem Einsatz prüfen.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import random
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..config import VerzeichnisConfig
from ..robots import Robots
from .base import Cache, OrtsAdapter, SearchError, SearchHit
from .dedupe import adresse_key

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

    def _hole(self, url: str, daten: dict[str, str] | None = None) -> str:
        schluessel = f"{url} POST {urlencode(daten)}" if daten is not None else url
        if self.cache is not None and (gecacht := self.cache.cache_get(self.name, schluessel)) is not None:
            return gecacht["html"]
        if not self._erlaubt(url):
            raise SearchError(f"{self.name}: robots.txt verbietet {url}")
        letzter = ""
        for versuch in range(1, self.cfg.retries + 2):
            if self.anfragen:
                self._sleep(self.cfg.pause_s + self._rng.uniform(0, self.cfg.pause_jitter_s))
            self._anfrage_zaehlen()
            try:
                resp = self.client.post(url, data=daten) if daten is not None else self.client.get(url)
            except httpx.HTTPError as e:
                letzter = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    if self.cache is not None:
                        self.cache.cache_put(self.name, schluessel, {"html": resp.text})
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

    def _werte(self, ort: Ort, plz: str, seite: int) -> dict[str, str | int]:
        name = ort.gemeinde or ort.name
        return dict(rubrik=self.cfg.rubrik, ort=name, ort_klein=name.lower(), plz=plz, seite=seite,
                    start=(seite - 1) * self.cfg.seitengroesse + 1)

    def _url(self, ort: Ort, plz: str, seite: int) -> str:
        werte = self._werte(ort, plz, seite)
        return self.cfg.url_vorlage.format(**{k: quote(str(v)) for k, v in werte.items()})

    def _formular(self, ort: Ort, plz: str, seite: int) -> dict[str, str] | None:
        if self.cfg.formular is None:
            return None
        werte = self._werte(ort, plz, seite)
        return {k: v.format(**werte) for k, v in self.cfg.formular.items()}

    def _blaettert(self) -> bool:
        vorlagen = [self.cfg.url_vorlage or "", *(self.cfg.formular or {}).values()]
        return any("{seite}" in v or "{start}" in v for v in vorlagen)

    def _parse(self, html: str, url: str) -> list[SearchHit] | None:
        """Verzeichnisspezifischer Parser (aus den Fixture-Seiten); None = schema.org-Rückfall."""
        return None

    def search_ort(self, ort: Ort) -> list[SearchHit]:
        einheiten = list(ort.alle_plz) if self.cfg.je_plz else [ort.plz]
        alle: dict[str, SearchHit] = {}
        for plz in einheiten:
            for seite in range(1, self.cfg.max_seiten + 1):
                url = self._url(ort, plz, seite)
                html = self._hole(url, self._formular(ort, plz, seite))
                hits = self._parse(html, url)
                if hits is None:
                    hits = schema_org_eintraege(html, url, self.name)
                neu = [h for h in hits if h.quell_id not in alle]
                for h in neu:
                    alle[h.quell_id] = h
                if not neu or not self._blaettert():
                    break
        return list(alle.values())


def _b64(wert: str | None) -> str | None:
    try:
        return base64.b64decode(wert or "", validate=True).decode("utf-8").strip() or None
    except (binascii.Error, UnicodeDecodeError):
        return None


def _knoten_text(el) -> str | None:
    return " ".join(el.get_text(" ").split()) or None if el is not None else None


_PLZ_ORT = re.compile(r"^(\d{5})\s+(.+)$")


def gelbeseiten_eintraege(html: str, basis_url: str, quelle: str = "gelbeseiten") -> list[SearchHit]:
    """Trefferkacheln (article.mod-Treffer): Name, Straße, PLZ/Ort, Telefon; Website base64 in data-webseitelink."""
    soup = BeautifulSoup(html or "", "lxml")
    hits = []
    for art in soup.select("article.mod-Treffer"):
        name = _knoten_text(art.select_one(".mod-Treffer__name"))
        if not name:
            continue
        strasse = plz = ort = None
        if (adr := art.select_one(".mod-AdresseKompakt__adress-text")) is not None:
            ort_el = adr.select_one(".mod-AdresseKompakt__adress__ort")
            if ort_el is not None and (m := _PLZ_ORT.match(_knoten_text(ort_el) or "")):
                plz, ort = m.group(1), m.group(2)
                vorher = [t for t in ort_el.find_previous_siblings(string=True)]
                strasse = " ".join(" ".join(vorher[::-1]).split()).rstrip(", ") or None
        tel_el = art.select_one(".mod-TelefonnummerKompakt__phoneNumber")
        telefon = (_knoten_text(tel_el) or _b64(tel_el.get("data-prg"))) if tel_el is not None else None
        web_el = art.select_one("[data-webseitelink]")
        website = _b64(web_el.get("data-webseitelink")) if web_el is not None else None
        link = art.find("a", href=True)
        hits.append(SearchHit(quelle=quelle, quell_id=art.get("data-realid") or art.get("id") or f"{name}|{plz}",
                              name=name, strasse=strasse, plz=plz, ort=ort, telefon=telefon, website=website,
                              roh_url=urljoin(basis_url, link["href"]) if link else basis_url))
    return hits


# ---------------------------------------------------------------------- amtliches Verzeichnis

_ZWEIGSTELLE = re.compile(r"^(?:Beratungsstelle|Beratungszentrum|Zweigniederlassung|Niederlassung|Geschäftsstelle|"
                          r"Außenstelle|Zweigstelle|Standort|Filiale|Büro)\b", re.I)
_TREFFER = re.compile(r"Treffer:\s*(\d+)\s*von\s*(\d+)")


def _zeilen(el) -> list[str]:
    if el is None:
        return []
    for br in el.find_all("br"):
        br.replace_with("\n")
    return [" ".join(z.split()) for z in el.get_text().split("\n") if z.strip()]


def zeilen_verbinden(zeilen: list[str]) -> str:
    """Umbrochene Namen zusammensetzen: „Steuerberatungs-" + „gesellschaft mbH" → ein Wort; „Wirtschafts- und",
    „Treuhand-" + „und Revisions-…" bleiben getrennt (Ergänzungsstrich)."""
    text = ""
    for z in zeilen:
        trennung = text.endswith("-") and z[:1].islower() and not re.match(r"(?:und|oder|u\.|bzw\.)\s", z)
        text = text[:-1] + z if trennung else f"{text} {z}".strip()  # „Treuhand- und" bleibt
    return text


def _schluessel_name(name: str) -> str:
    return re.sub(r"[^0-9a-zäöüß]", "", name.casefold())


def _erstes_wort(name: str) -> str:
    return _schluessel_name(name.split()[0]) if name.split() else ""


def _firma(zeilen: list[str]) -> str | None:
    """Kanzlei aus den Zeilen vor der Straße: Zweigstellen-Zeilen raus, „c/o X" → X (Kanzlei, in der gearbeitet wird)."""
    zeilen = [z for z in zeilen if not _ZWEIGSTELLE.match(z)]
    if co := [i for i, z in enumerate(zeilen) if z.lower().startswith("c/o ")]:
        zeilen = [zeilen[co[0]][4:].strip(), *zeilen[co[0] + 1:]]
    return zeilen_verbinden(zeilen) or None


def stbv_kanzleien(html: str, basis_url: str, quelle: str = "stb_verzeichnis") -> tuple[list[SearchHit], tuple[int, int] | None]:
    """Karten des amtlichen Verzeichnisses → ein Treffer je Kanzlei; dazu (angezeigt, gesamt) aus „Treffer: X von Y".

    Gesellschaften (Karte ohne Beruf) sind Kanzleien. Berufsträger gehören zur Firma in ihren Adresszeilen (vor der
    Straße); dieselbe Firma an derselben Adresse wie eine Gesellschaft (gleicher Name oder eindeutig gleiches erstes
    Wort: „RBB v.Reden … mbB" → „RBB Büchl & Partner mbB") wird ihr zugeordnet. Ohne Firmenzeile ist der Berufsträger
    eine Einzelkanzlei („Steuerberaterin Imke Thordsen"). Die Berufsträger stehen im Snippet.
    """
    soup = BeautifulSoup(html or "", "lxml")
    gruppen: dict[tuple, dict] = {}
    personen: list[tuple[str, str, str | None, str | None, str | None, str]] = []
    for karte in soup.select("a.link-to-detail"):
        name_zeilen = _zeilen(karte.select_one("p.name"))
        adresse = _zeilen(karte.select_one("p.address-display"))
        if not name_zeilen:
            continue
        plz = ort = strasse = None
        if adresse and (m := _PLZ_ORT.match(adresse[-1])):
            plz, ort = m.group(1), m.group(2)
            adresse = adresse[:-1]
        if adresse:
            strasse, adresse = adresse[-1], adresse[:-1]
        url = urljoin(basis_url, karte["href"])
        beruf = _knoten_text(karte.select_one("p.beruf"))
        if beruf is None:  # Gesellschaft
            firma = _firma(name_zeilen + adresse)
            key = (adresse_key(strasse, plz) or f"{strasse}|{plz}", _schluessel_name(firma))
            gruppen.setdefault(key, dict(name=firma, strasse=strasse, plz=plz, ort=ort, url=url, personen=[],
                                         gesellschaft=True))
        else:
            titel = _knoten_text(karte.select_one("p.akademische-grade"))
            person = " ".join(x for x in (beruf, titel, zeilen_verbinden(name_zeilen)) if x)
            personen.append((person, f"{beruf} {zeilen_verbinden(name_zeilen)}", _firma(adresse), strasse, plz, ort, url))
    for person, einzelname, firma, strasse, plz, ort, url in personen:
        adr = adresse_key(strasse, plz) or f"{strasse}|{plz}"
        ziel = None
        if firma:
            ziel = gruppen.get((adr, _schluessel_name(firma)))
            vor_ort = [(k, g) for (a, k), g in gruppen.items() if a == adr and g["gesellschaft"]]
            fk, fw = _schluessel_name(firma), _erstes_wort(firma)
            praefix = [g for k, g in vor_ort if k.startswith(fk) or fk.startswith(k)]  # „rt Revision + Treuhand GmbH"
            wort = [g for _, g in vor_ort if _erstes_wort(g["name"]) == fw]
            ziel = ziel or (praefix[0] if len(praefix) == 1 else wort[0] if len(wort) == 1 else None)
        if ziel is None:
            name = firma or einzelname
            ziel = gruppen.setdefault((adr, _schluessel_name(name)), dict(
                name=name, strasse=strasse, plz=plz, ort=ort, url=url, personen=[], gesellschaft=False))
        ziel["personen"].append(person)
    hits = []
    for (adr, key), g in gruppen.items():
        snippet = (f"Amtliches Steuerberaterverzeichnis: {len(g['personen'])} Berufsträger ({'; '.join(g['personen'])})"
                   if g["personen"] else "Amtliches Steuerberaterverzeichnis: Berufsausübungsgesellschaft")
        hits.append(SearchHit(quelle=quelle, quell_id=f"{g['plz']}|{adr}|{key}", name=g["name"], strasse=g["strasse"],
                              plz=g["plz"], ort=g["ort"], snippet=snippet, roh_url=g["url"]))
    zahl = _TREFFER.search(" ".join(soup.get_text(" ").split()))
    return hits, (int(zahl.group(1)), int(zahl.group(2))) if zahl else None


class GelbeSeitenAdapter(_Verzeichnis):
    """Branchenverzeichnis, nur Ergänzung bekannter Kandidaten (Telefon, Adresse, Website)."""

    name = "gelbeseiten"

    def _parse(self, html: str, url: str) -> list[SearchHit] | None:
        return gelbeseiten_eintraege(html, url, self.name)


class DasOertlicheAdapter(_Verzeichnis):
    """schema.org (JSON-LD ItemList), 25 Einträge je Seite, Blättern über recFrom={start}."""

    name = "dasoertliche"


class Elf880Adapter(_Verzeichnis):
    """11880.com: schema.org (JSON-LD SearchResultsPage), 50 je Seite, ?page={seite}."""

    name = "elf880"


class StbVerzeichnisAdapter(_Verzeichnis):
    """Amtliches Steuerberaterverzeichnis: Suche (POST) je PLZ des Ortes (Grundliste), 30–35 s Pause."""

    name = "stb_verzeichnis"

    def _parse(self, html: str, url: str) -> list[SearchHit] | None:
        if "link-to-detail" not in (html or "") and "Treffer:" not in (html or ""):
            return None  # anderer Seitenaufbau (Tests mit schema.org-Seiten)
        hits, zahl = stbv_kanzleien(html, url, self.name)
        if zahl and zahl[0] < zahl[1]:
            meldung = f"{self.name}: nur {zahl[0]} von {zahl[1]} Treffern angezeigt ({url}) – Rest fehlt"
            log.warning(meldung)
            if self.melden:
                self.melden(meldung)
        return hits
