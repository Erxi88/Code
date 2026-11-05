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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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


_HOST_SPERRE = threading.Lock()
_HOST_LETZTE: dict[str, float] = {}


def _host_abstand(url: str, mindestens: float, sleep: Callable[[float], None]) -> None:
    """Mindestabstand zwischen zwei Anfragen an denselben Host über alle Threads (mehrere Länder parallel)."""
    if mindestens <= 0:
        return
    host = urlsplit(url).netloc
    with _HOST_SPERRE:
        warten = _HOST_LETZTE.get(host, 0.0) + mindestens - time.monotonic()
        _HOST_LETZTE[host] = time.monotonic() + max(warten, 0.0)
    if warten > 0:
        sleep(warten)


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
        # je Abruf (für den Pilot): Antwortzeit ohne Pause, aus dem Cache, Trefferzahl „X von Y" der Seite
        self.abrufe: list[dict] = []

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

    def _hole(self, url: str, daten: dict[str, str] | None = None, pause: tuple[float, float] | None = None) -> str:
        schluessel = f"{url} POST {urlencode(daten)}" if daten is not None else url
        if self.cache is not None and (gecacht := self.cache.cache_get(self.name, schluessel)) is not None:
            self.abrufe.append({"url": url, "sekunden": 0.0, "cache": True})
            return gecacht["html"]
        if not self._erlaubt(url):
            raise SearchError(f"{self.name}: robots.txt verbietet {url}")
        letzter = ""
        for versuch in range(1, self.cfg.retries + 2):
            if self.anfragen:
                basis, zufall = pause or (self.cfg.pause_s, self.cfg.pause_jitter_s)
                self._sleep(basis + self._rng.uniform(0, zufall))
            self._anfrage_zaehlen()
            _host_abstand(url, self.cfg.min_abstand_host_s, self._sleep)
            start = time.monotonic()
            try:
                resp = self.client.post(url, data=daten) if daten is not None else self.client.get(url)
            except httpx.HTTPError as e:
                letzter = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    self.abrufe.append({"url": url, "sekunden": round(time.monotonic() - start, 2), "cache": False,
                                        "versuch": versuch})
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

    def _weiter(self, html: str) -> bool:
        """Gibt es eine nächste Ergebnisseite? Standard: ja, solange neue Einträge kommen."""
        return True

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
                if not neu or not self._blaettert() or not self._weiter(html):
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


OHNE_GESELLSCHAFT = "Person ohne Gesellschaft an dieser Adresse"
NICHT_ZUGEORDNET = "Einzelperson an Mehrkanzlei-Adresse – zuordnen"
# Regel 4 (SH 05.11.2026): Person ohne Firmenzeile, einer Kanzlei an ihrer Adresse als Berufsträger zugeordnet –
# Vermerk für das Kontrollblatt, damit echte Einzelkanzleien im selben Haus erkennbar bleiben
ANGESTELLT = "angestellt (vermutet)"
ANGESTELLT_PRAEFIX = "Angestellt vermutet (Regel 4): "  # im Snippet ohne Klammern (Berufsträger-Liste steht in Klammern)
# Berufsträger-Liste im Snippet: bis zur schließenden Klammer vor „;" bzw. Ende – Namen mit „(FH)" bleiben ganz
BERUFSTRAEGER_SNIPPET = re.compile(r"Amtliches Steuerberaterverzeichnis: (\d+) Berufsträger \((.*?)\)(?=;|$)")


SOZIETAET = "Sozietät/Bürogemeinschaft – Name über Website prüfen"


def sozietaet_name(nachnamen: list[str]) -> str:
    """Vorläufiger Name (Regel 3): „Steuerberater Lund & Ott", ab drei Namen „Steuerberater Lund, Ott & Rau"."""
    namen = list(dict.fromkeys(nachnamen))
    return "Steuerberater " + (" & ".join(namen) if len(namen) <= 2 else f"{', '.join(namen[:-1])} & {namen[-1]}")


def ist_einzeln(snippet: str | None) -> bool:
    """Treffer ist eine einzelne Person (keine Gesellschaft, keine Firmenzeile)."""
    return bool(snippet) and (OHNE_GESELLSCHAFT in snippet or NICHT_ZUGEORDNET in snippet)
FIRMA_LAUT_VERZEICHNIS = "Firma laut Verzeichnis: "


@dataclass
class StbvKarte:
    """Eine Karte der Ergebnisliste des amtlichen Verzeichnisses (Berufsträger oder Gesellschaft)."""
    name: str  # Personenname bzw. Firmenname (Zeilen verbunden)
    beruf: str | None  # None = Gesellschaft
    titel: str | None
    firma: str | None  # Firmenzeile(n) vor der Straße (Person) bzw. Firma (Gesellschaft); c/o aufgelöst
    strasse: str | None
    plz: str | None
    ort: str | None
    url: str

    @property
    def gesellschaft(self) -> bool:
        return self.beruf is None


def stbv_karten(html: str, basis_url: str) -> tuple[list[StbvKarte], tuple[int, int] | None]:
    """Alle Karten der Ergebnisseite – ohne Gruppierung; dazu (angezeigt, gesamt) aus „Treffer: X von Y"."""
    soup = BeautifulSoup(html or "", "lxml")
    karten = []
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
        beruf = _knoten_text(karte.select_one("p.beruf"))
        if beruf is None:
            name = firma = _firma(name_zeilen + adresse)
        else:
            name, firma = zeilen_verbinden(name_zeilen), _firma(adresse)
        karten.append(StbvKarte(name or "", beruf, _knoten_text(karte.select_one("p.akademische-grade")), firma,
                                strasse, plz, ort, urljoin(basis_url, karte["href"])))
    zahl = _TREFFER.search(" ".join(soup.get_text(" ").split()))
    return karten, (int(zahl.group(1)), int(zahl.group(2))) if zahl else None


# Steuerberatungsbezug einer Firmenzeile – sonst Syndikus/Unternehmen („Siemens AG", „Sparkasse Holstein")
_STEUERBEZUG = re.compile(
    r"steuer|\bstb|kanzlei|treuhand|revision|wirtschaftspr|buchpr|buchf(?:ü|ue)hr|buchstelle|beratung|berater|consult|"
    r"audit|\btax|partner|partg|\bmbb\b|sozietät|sozietaet|collegen|kollegen|rechtsanw|\bwpg\b|\bstbg\b|\bwp\b|"
    r"\bpwc\b|pricewaterhouse|\bkpmg\b|deloitte|\bbdo\b|ecovis|\betl\b|ernst\s*&\s*young|\bey\b", re.IGNORECASE)
_RECHTSFORM_ENDE = re.compile(
    r"gmbh\s*&\s*co\.?\s*kg|partg\s*mbb|partnerschaft(?:sgesellschaft)?\s*mbb|partnerschaft|gmbh|mbh|mbb|\bag\b|"
    r"\bkg\b|\bgbr\b|\bohg\b|e\.\s?v\.", re.IGNORECASE)
SYNDIKUS = "Syndikus/Unternehmen"
FIRMENZEILE_OHNE_KARTE = "Kanzlei aus Firmenzeile (ohne Gesellschaftskarte)"
KURZFORM_ZUSAMMEN = "zusammengeführt über die Kurzform "


def hat_steuerbezug(firma: str | None) -> bool:
    return bool(firma and _STEUERBEZUG.search(firma))


def firmen_schluessel(firma: str) -> str:
    """Vergleichsschlüssel einer Firmenzeile: bis zur ersten Rechtsform, ohne Umlaut-/Satzzeichen-Unterschiede –
    „rt Revision + Treuhand GmbH & Co. KG Wirtschaftsprüfungsgesellschaft …" = „rt Revision & Treuhand GmbH & Co. KG",
    „Oelerking Bröcker Hamann Partnerschaft mbB …" = „Oelerking Broecker Hamann Partnerschaft MbB"."""
    f = firma.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    if m := _RECHTSFORM_ENDE.search(f):
        f = f[: m.end()]
    return re.sub(r"[^0-9a-z]", "", f)


def _kanzlei_schluessel(karten: list[StbvKarte]) -> set[str]:
    """Firmenschlüssel aller Gesellschaften und Firmenzeilen mit Steuerberatungsbezug der Seite."""
    return {firmen_schluessel(k.firma or k.name) for k in karten
            if k.gesellschaft or hat_steuerbezug(k.firma)}


def ist_syndikus(k: StbvKarte, kanzleien: set[str]) -> bool:
    """Firmenzeile ohne Steuerberatungsbezug, die auch keiner Kanzlei der Seite entspricht („Treurat GmbH" =
    „Treurat GmbH Wirtschaftsprüfungsgesellschaft …" ist eine Kanzlei; „Siemens AG" nicht)."""
    if k.gesellschaft or not k.firma or hat_steuerbezug(k.firma):
        return False
    fs = firmen_schluessel(k.firma)
    return not any(fs == x or x.startswith(fs) or fs.startswith(x) for x in kanzleien if len(x) >= 4)


def stbv_syndikus(karten: list[StbvKarte]) -> list[StbvKarte]:
    """Berufsträger, deren Firmenzeile keinen Steuerberatungsbezug hat (Syndikus-Steuerberater eines Unternehmens) –
    nie als Kanzlei, aber in der Lückenliste (Vorgabe 05.11.2026)."""
    kanzleien = _kanzlei_schluessel(karten)
    return [k for k in karten if ist_syndikus(k, kanzleien)]


def _falten(text: str) -> str:
    return text.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")


def _namenswoerter(name: str) -> list[str]:
    """„SIGMA - Kühne + Partner" → [sigma, kuehne, partner] (Satzzeichen und „+"/„&" zählen nicht)."""
    return re.findall(r"[a-z0-9]+", _falten(name))


# Zusätze am Ende einer Firmenzeile, die im Namen der Gesellschaft anders geschrieben sein können
_ZUSATZ_ENDE = re.compile(
    r"(?:\s|[,.&+-])*(?:wpg|stbg|rag|gmbh|mbh|mbb|partg|partgmbb|ag|kg|co|se|steuerberatungsgesellschaft|"
    r"wirtschaftspruefungsgesellschaft|rechtsanwaltsgesellschaft|partnerschaft(?:sgesellschaft)?|steuerberater|"
    r"rechtsanwaelte|wirtschaftspruefer)\.?$")


# Berufsbezeichnungen am Anfang („StB Kruse Jäckel Groß …" = „Steuerberater Kruse Jäckel Groß …")
_BERUF_ANFANG = re.compile(r"^(?:(?:stb|steuerberater(?:in)?|wp|wirtschaftspruefer(?:in)?|ra|rechtsanwa(?:lt|eltin|elte))"
                           r"\.?(?:\s|[,&+/-])+)+")


def _ohne_beruf_vorn(text: str) -> str:
    return _BERUF_ANFANG.sub("", _falten(text).strip())


def _firmenkern(firma: str) -> str:
    """„Cordes + Partner WPG" → „cordespartner": ohne Berufsbezeichnung vorn und Rechtsform-/Berufszusätze am Ende,
    nur Buchstaben/Ziffern."""
    f = _ohne_beruf_vorn(firma)
    while (kurz := _ZUSATZ_ENDE.sub("", f).strip()) != f and kurz:
        f = kurz
    return re.sub(r"[^0-9a-z]", "", f)


def _name_passt(firma: str, name: str) -> bool:
    """Regel 2: gleiche erste zwei Namenswörter (ohne Berufsbezeichnung vorn) oder die Firmenzeile ohne Zusätze ist im
    Namen enthalten."""
    woerter = _namenswoerter(_ohne_beruf_vorn(firma))[:2]
    kern = _firmenkern(firma)
    return ((len(woerter) == 2 and _namenswoerter(_ohne_beruf_vorn(name))[:2] == woerter)
            or (len(kern) >= 6 and kern in re.sub(r"[^0-9a-z]", "", _falten(name))))


def _passende_kanzlei(firma: str, vor_ort: list[dict]) -> dict | None:
    """Regel 2 (HH 05.11.2026): Kanzlei an derselben Adresse – Gesellschaft oder (Freigabe 05.11.2026) Kanzlei aus
    einer anderen Firmenzeile –, die zur Firmenzeile passt (`_name_passt`, in beide Richtungen): „PKF Fasselt" →
    „PKF Fasselt Partnerschaft mbB …", „Cordes + Partner WPG" → „CFH Cordes + Partner …", „StB Kruse Jäckel Groß
    Partnerschaft mbB" = „Steuerberater Kruse Jäckel Groß PartG mbB". Mehrere passende → kürzester Name
    (Hauptgesellschaft)."""
    passend = [g for g in vor_ort if _name_passt(firma, g["name"]) or _name_passt(g["name"], firma)]
    return min(passend, key=lambda g: len(g["name"])) if passend else None


# Kurzformen großer Gesellschaften und Netzwerke (Vorgabe 05.11.2026): gleiche Kurzform an derselben Adresse = eine
# Kanzlei („PwC GmbH WPG" = „PricewaterhouseCoopers GmbH WPG"). Muster auf dem gefalteten Namen (ä → ae …).
GROSSGESELLSCHAFTEN: dict[str, str] = {
    "PwC": r"\bpwc\b|pricewaterhouse",
    "EY": r"\bey\b|ernst\s*(?:&|\+|und)\s*young",
    "KPMG": r"\bkpmg\b",
    "BDO": r"\bbdo\b",
    "Deloitte": r"deloitte",
    "RSM": r"\brsm\b",
    "Forvis Mazars": r"\bmazars\b|\bforvis\b",
    "Rödl": r"\broedl\b",
    "Baker Tilly": r"baker\s*tilly",
    "Grant Thornton": r"grant\s*thornton",
    "PKF": r"\bpkf\b",
    "HLB": r"\bhlb\b",
    "Ebner Stolz": r"ebner\s*stolz",
    "WTS": r"\bwts\b",
    "ETL": r"\betl\b",
    "ECOVIS": r"\becovis\b",
}
_GROSS = [(kurz, re.compile(muster)) for kurz, muster in GROSSGESELLSCHAFTEN.items()]


def kurzform(name: str | None) -> str | None:
    """Kurzform einer Großgesellschaft im Namen („PricewaterhouseCoopers GmbH" → „PwC"), sonst None."""
    f = _falten(name or "")
    return next((kurz for kurz, m in _GROSS if m.search(f)), None)


def _kanzlei_zur_person(name: str, vor_ort: list[dict]) -> dict | None:
    """Regel 1 (HH 05.11.2026, freigegeben): Person ohne Firmenzeile – genau eine Kanzlei an der Adresse → diese;
    mehrere → die eine, deren Name den Nachnamen enthält (Bartsch → „ETL Bartsch & Kollegen"); sonst keine."""
    if len(vor_ort) == 1:
        return vor_ort[0]
    teile = {t for t in _namenswoerter(name.split()[-1]) if len(t) >= 3} if name.split() else set()
    treffer = [g for g in vor_ort if teile & set(_namenswoerter(g["name"]))]
    return treffer[0] if len(treffer) == 1 else None


def stbv_gruppieren(karten: list[StbvKarte], quelle: str = "stb_verzeichnis") -> list[SearchHit]:
    """Karten → ein Treffer je Kanzlei (Vorgabe 05.11.2026). Aus dem Cache neu berechenbar (`stbv_karten` + diese
    Funktion), ohne das Verzeichnis erneut abzufragen.

    - Gesellschaften (Karte ohne Beruf) sind Kanzleien.
    - Ein Berufsträger mit Firmenzeile gehört zur Gesellschaft an derselben Adresse, deren Name passt (Firmenschlüssel
      bis zur Rechtsform gleich, Namensanfang oder eindeutig gleiches erstes Wort: „RBB v.Reden … mbB" → „RBB Büchl &
      Partner mbB"; „c/o X" → X).
    - Sonst bilden Berufsträger mit gleicher Firmenzeile (Firmenschlüssel) an derselben Adresse eine Kanzlei – auch ohne
      Gesellschaftskarte („Sozietät Sierck & Remuß Steuerberater"); verschiedene Firmenzeilen an einer Adresse bleiben
      getrennte Kanzleien (Bürogemeinschaft).
      Ausnahme (Regel 2, HH 05.11.2026): beginnt eine Gesellschaft an derselben Adresse mit denselben ersten zwei
      Namenswörtern oder enthält sie die Firmenzeile ohne Zusätze, gehört die Person zu ihr (`_gesellschaft_zur_firmenzeile`).
    - Ohne Firmenzeile (Regel 1, HH 05.11.2026): genau eine Kanzlei (Gesellschaft oder Firmenzeile) an der Adresse →
      dieser zugeordnet; mehrere → die, deren Name den Nachnamen enthält, sonst eigener Kandidat (Kennzeichen „… nicht
      zugeordnet"). Ohne Kanzlei an der Adresse (Regel 3, SH 05.11.2026): mehrere solche Personen sind eine Kanzlei
      (Inhaber und Angestellte), vorläufig „Steuerberater Nachname1 & Nachname2", Kennzeichen „Sozietät/
      Bürogemeinschaft – Name über Website prüfen"; allein → eigener Kandidat („Steuerberaterin Imke Thordsen",
      „Person ohne Gesellschaft an dieser Adresse").
    - Firmenzeile ohne Steuerberatungsbezug („Siemens AG") → Syndikus/Unternehmen, kein Kandidat (`stbv_syndikus`).
    """
    gruppen: dict[tuple, dict] = {}

    def kurz_gruppe(adr: str, kz: str, name: str, k: StbvKarte, art: str) -> dict:
        """Gruppe der Großgesellschaft `kz` an der Adresse; Name = kürzester (Gesellschaft vor Firmenzeile)."""
        g = gruppen.setdefault((adr, "kurz:" + kz), dict(name=name, strasse=k.strasse, plz=k.plz, ort=k.ort, url=k.url,
                                                         personen=[], art=art, namen=[]))
        if name not in g["namen"]:
            g["namen"].append(name)
        rang = lambda art_, n: (art_ != "gesellschaft", len(n))  # noqa: E731
        if rang(art, name) < rang(g["art"], g["name"]):
            g.update(name=name, art=art, url=k.url if art == "gesellschaft" else g["url"])
        return g

    for k in karten:
        if k.gesellschaft:
            adr = adresse_key(k.strasse, k.plz) or f"{k.strasse}|{k.plz}"
            if kz := kurzform(k.firma or k.name):
                kurz_gruppe(adr, kz, k.firma or k.name, k, "gesellschaft")
                continue
            gruppen.setdefault((adr, _schluessel_name(k.firma or k.name)), dict(
                name=k.firma or k.name, strasse=k.strasse, plz=k.plz, ort=k.ort, url=k.url, personen=[],
                art="gesellschaft"))
    kanzleien = _kanzlei_schluessel(karten)
    # erst Personen mit Firmenzeile (bilden ggf. Kanzleien aus Firmenzeile), dann ohne – Regel 1 sieht alle Kanzleien
    for k in sorted(karten, key=lambda k: not k.firma):
        if k.gesellschaft or ist_syndikus(k, kanzleien):
            continue
        person = " ".join(x for x in (k.beruf, k.titel, k.name) if x)
        einzelname = f"{k.beruf} {k.name}"
        adr = adresse_key(k.strasse, k.plz) or f"{k.strasse}|{k.plz}"
        ziel = None
        if k.firma and (kz := kurzform(k.firma)):
            ziel = kurz_gruppe(adr, kz, k.firma, k, "firmenzeile")
        elif k.firma:
            vor_ort = [(key, g) for (a, key), g in gruppen.items() if a == adr and g["art"] == "gesellschaft"]
            fk, fw, fs = _schluessel_name(k.firma), _erstes_wort(k.firma), firmen_schluessel(k.firma)
            gleich = [g for key, g in vor_ort if key == fk or firmen_schluessel(g["name"]) == fs]
            praefix = [g for key, g in vor_ort if key.startswith(fk) or fk.startswith(key)]
            wort = [g for _, g in vor_ort if _erstes_wort(g["name"]) == fw]
            ziel = gleich[0] if gleich else praefix[0] if len(praefix) == 1 else wort[0] if len(wort) == 1 else None
            ziel = ziel or _passende_kanzlei(k.firma, [g for _, g in vor_ort])
            if ziel is None:  # gleiche bzw. passende Firmenzeile an derselben Adresse = eine Kanzlei (ohne Karte)
                ziel = gruppen.get((adr, "firma:" + fs)) or _passende_kanzlei(k.firma, [
                    g for (a, _), g in gruppen.items() if a == adr and g["art"] == "firmenzeile"])
                if ziel is None:
                    ziel = gruppen.setdefault((adr, "firma:" + fs), dict(
                        name=k.firma, strasse=k.strasse, plz=k.plz, ort=k.ort, url=k.url, personen=[],
                        art="firmenzeile"))
                elif len(k.firma) < len(ziel["name"]):  # Hauptname = kürzester
                    ziel["name"] = k.firma
        else:
            vor_ort = [g for (a, _), g in gruppen.items() if a == adr and g["art"] not in ("person", "personen")]
            ziel = _kanzlei_zur_person(k.name, vor_ort)
            if ziel is not None:  # Regel 1/4: als Berufsträger der Kanzlei an der Adresse, Vermerk „angestellt"
                ziel.setdefault("angestellt", []).append(person)
            if ziel is None and vor_ort:  # mehrere Kanzleien, kein Nachname passt → einzeln
                ziel = gruppen.setdefault((adr, _schluessel_name(einzelname)), dict(
                    name=einzelname, strasse=k.strasse, plz=k.plz, ort=k.ort, url=k.url, personen=[], art="person",
                    mehrere=True))
            elif ziel is None:  # Regel 3: Personen ohne Firmenzeile an einer Adresse ohne Kanzlei = eine Kanzlei
                ziel = gruppen.setdefault((adr, "personen:"), dict(
                    name=einzelname, strasse=k.strasse, plz=k.plz, ort=k.ort, url=k.url, personen=[], art="personen",
                    nachnamen=[]))
                if k.name.split():
                    ziel["nachnamen"].append(k.name.split()[-1])
        ziel["personen"].append(person)
    hits = []
    for (adr, key), g in gruppen.items():
        n = len(g["personen"])
        if g["art"] == "personen":
            if n == 1:  # allein an der Adresse – wie bisher Person ohne Gesellschaft (Schlüssel unverändert)
                g["art"], key = "person", _schluessel_name(g["name"])
            else:
                g["name"], key = sozietaet_name(g["nachnamen"]), "sozietaet"
        if g["art"] == "personen":
            snippet = (f"Amtliches Steuerberaterverzeichnis: {n} Berufsträger ({'; '.join(g['personen'])}); "
                       f"{SOZIETAET}")
        elif g["art"] == "gesellschaft":
            snippet = (f"Amtliches Steuerberaterverzeichnis: {n} Berufsträger ({'; '.join(g['personen'])})"
                       if n else "Amtliches Steuerberaterverzeichnis: Berufsausübungsgesellschaft")
        elif g["art"] == "firmenzeile":
            snippet = (f"Amtliches Steuerberaterverzeichnis: {n} Berufsträger ({'; '.join(g['personen'])}); "
                       f"{FIRMENZEILE_OHNE_KARTE}")
        else:
            snippet = (f"Amtliches Steuerberaterverzeichnis: 1 Berufsträger ({'; '.join(g['personen'])}); "
                       f"{NICHT_ZUGEORDNET if g.get('mehrere') else OHNE_GESELLSCHAFT}")
        if g.get("angestellt"):
            snippet += f"; {ANGESTELLT_PRAEFIX}{' | '.join(g['angestellt'])}"
        if len(g.get("namen") or []) > 1:
            snippet += f"; {KURZFORM_ZUSAMMEN}{key.removeprefix('kurz:')}: {' | '.join(g['namen'])}"
        hits.append(SearchHit(quelle=quelle, quell_id=f"{g['plz']}|{adr}|{key}", name=g["name"], strasse=g["strasse"],
                              plz=g["plz"], ort=g["ort"], snippet=snippet, roh_url=g["url"]))
    return hits


def stbv_kanzleien(html: str, basis_url: str, quelle: str = "stb_verzeichnis") -> tuple[list[SearchHit], tuple[int, int] | None]:
    """Karten der Ergebnisseite → ein Treffer je Kanzlei (`stbv_gruppieren`); dazu (angezeigt, gesamt)."""
    karten, zahl = stbv_karten(html, basis_url)
    return stbv_gruppieren(karten, quelle), zahl


_HANDLER_DATA = re.compile(r"handlerData\s*=\s*(\[\[.*?\]\])\s*;", re.S)
# Spalten des handlerData-Arrays (aus der echten Seite, 05.11.2026)
_HD_ID, _HD_WEB, _HD_ORT, _HD_PLZ, _HD_STRASSE, _HD_NR, _HD_TEL, _HD_NAME, _HD_MAIL = 0, 3, 4, 8, 9, 10, 11, 14, 17


def dasoertliche_eintraege(html: str, basis_url: str, quelle: str = "dasoertliche") -> list[SearchHit] | None:
    """Einträge aus `handlerData` (je Zeile ein Eintrag), Detaillink aus der Trefferliste; None ohne das Array."""
    m = _HANDLER_DATA.search(html or "")
    if not m:
        return None
    try:
        zeilen = json.loads(m.group(1))
    except ValueError:
        return None
    details = {}
    for a in BeautifulSoup(html, "lxml").select("a.hitlnk_name[href]"):
        if mid := re.search(r"[?&]id=(\d+)", a["href"]):
            details[mid.group(1)] = urljoin(basis_url, a["href"])
    hits, gesehen = [], set()
    for z in zeilen:
        if len(z) <= _HD_MAIL or not z[_HD_NAME] or z[_HD_ID] in gesehen:
            continue
        gesehen.add(z[_HD_ID])
        detail = next((u for i, u in details.items() if i.startswith(z[_HD_ID])), basis_url)
        strasse = " ".join(x for x in (z[_HD_STRASSE], z[_HD_NR]) if x).strip() or None
        hits.append(SearchHit(quelle=quelle, quell_id=z[_HD_ID], name=z[_HD_NAME], strasse=strasse,
                              plz=z[_HD_PLZ] or None, ort=z[_HD_ORT] or None, telefon=z[_HD_TEL] or None,
                              website=z[_HD_WEB] or None, email=z[_HD_MAIL] or None, roh_url=detail))
    return hits


def stadt_schluessel(ort: str) -> str:
    """„Bad Bramstedt" → „bad_bramstedt" (Schreibweise der Gelbe-Seiten-Sitemap)."""
    f = ort.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "_", f).strip("_")


class GelbeSeitenAdapter(_Verzeichnis):
    """Branchenverzeichnis, nur Ergänzung bekannter Kandidaten (Telefon, Adresse, Website)."""

    name = "gelbeseiten"

    def _parse(self, html: str, url: str) -> list[SearchHit] | None:
        return gelbeseiten_eintraege(html, url, self.name)

    def stadt_bekannt(self, ort: str) -> bool:
        """Hat die Stadt eine Stadtseite laut Sitemap (staedte_sitemap)? Ohne Sitemap oder bei Fehler: nein."""
        if not self.cfg.staedte_sitemap:
            return False
        if getattr(self, "_staedte", None) is None:
            try:
                xml = self._hole(self.cfg.staedte_sitemap)
            except SearchError:
                xml = ""
            self._staedte = set(re.findall(r"sitemap_trefferliste_([a-z0-9_]+?)_alle_\d+", xml))
        return stadt_schluessel(ort) in self._staedte


class DasOertlicheAdapter(_Verzeichnis):
    """Trefferliste mit Skript-Array `handlerData` (Name, Straße, PLZ, Ort, Telefon, Website, E-Mail je Eintrag; Website
    und E-Mail stehen sonst nur hinter Schaltflächen), Rückfall schema.org; 25 je Seite, weiter über „zur nächsten
    Seite" (recFrom={start})."""

    name = "dasoertliche"

    def _parse(self, html: str, url: str) -> list[SearchHit] | None:
        return dasoertliche_eintraege(html, url, self.name)

    def _weiter(self, html: str) -> bool:
        # Trefferliste mit handlerData: weiter nur mit Link „zur nächsten Seite"; schema.org-Seiten ohne Seitenzähler:
        # weiter, solange neue Einträge kommen
        return "zur nächsten Seite" in (html or "") or "handlerData" not in (html or "")


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
        if self.abrufe:
            self.abrufe[-1].update(angezeigt=zahl[0] if zahl else None, gesamt=zahl[1] if zahl else None,
                                   eintraege=len(BeautifulSoup(html or "", "lxml").select("a.link-to-detail")))
        if zahl and zahl[0] < zahl[1]:
            meldung = f"{self.name}: nur {zahl[0]} von {zahl[1]} Treffern angezeigt ({url}) – Rest fehlt"
            log.warning(meldung)
            if self.melden:
                self.melden(meldung)
        return hits

    def detail_erlaubt(self, url: str) -> bool:
        """robots.txt erlaubt die Detailseiten (HH-Lauf 05.11.2026: /details/ ist gesperrt – Verbot bleibt respektiert)."""
        return self._erlaubt(url)

    def detail(self, url: str) -> StbvDetail | None:
        """Detailseite eines Eintrags – eigener, kürzerer Abstand (detail_pause_s + Zufall), gecacht."""
        html = self._hole(url, pause=(self.cfg.detail_pause_s, self.cfg.detail_pause_jitter_s))
        return stbv_detail(html, url)


# ---------------------------------------------------------------------- amtliches Verzeichnis: Detailseite


@dataclass
class StbvDetail:
    """Felder der Detailseite eines Eintrags (Gesellschaft oder Berufsträger) – Quelle „amtlich"."""
    url: str
    name: str | None = None  # Firmenname (Zeilen verbunden) bzw. Personenname
    beruf: str | None = None  # Berufsbezeichnung (Person) bzw. Berufszeile im Firmennamen
    titel: str | None = None
    rechtsform: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    telefon: str | None = None
    mobil: str | None = None
    fax: str | None = None
    email: str | None = None
    internet: str | None = None
    safe_id: str | None = None
    kammer: str | None = None
    beratungsstellen: list[tuple[str | None, str | None, str | None]] = field(default_factory=list)  # (Straße, PLZ, Ort)
    geschaeftsfuehrung: list[str] = field(default_factory=list)  # „Dipl.-Kauffrau Anke Anders"
    gesellschafter: list[str] = field(default_factory=list)  # vertretungsberechtigte Gesellschafter

    def als_dict(self) -> dict:
        return {k: (list(map(list, v)) if k == "beratungsstellen" else v) for k, v in self.__dict__.items()}

    @classmethod
    def aus_dict(cls, d: dict) -> StbvDetail:
        d = dict(d)
        d["beratungsstellen"] = [tuple(x) for x in d.get("beratungsstellen") or []]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _feld(soup, id_: str) -> str | None:
    """Wert hinter dem Label („Telefon:" ⏎ „0431 670090") eines <p id=…>."""
    el = soup.find(id=id_)
    if el is None:
        return None
    spans = el.find_all("span", class_="text-wrap")
    return _knoten_text(spans[-1]) if spans else _knoten_text(el)


def _liste(soup, id_: str) -> list[str]:
    el = soup.find(id=id_)
    return [t for li in el.find_all("li") if (t := _knoten_text(li))] if el is not None else []


def _adresse_teilen(zeilen: list[str]) -> tuple[str | None, str | None, str | None]:
    plz = ort = strasse = None
    if zeilen and (m := _PLZ_ORT.match(zeilen[-1])):
        plz, ort = m.group(1), m.group(2)
        zeilen = zeilen[:-1]
    if zeilen:
        strasse = zeilen[-1]
    return strasse, plz, ort


def stbv_detail(html: str, url: str) -> StbvDetail | None:
    """Detailseite des amtlichen Verzeichnisses (IDs der Seite: firmenname, rechtsform, adresse, telefon, mobil, fax,
    email, internetseite, safeid, regionalkammerSection, other-advisory-offices-list, management-body-list,
    authorized-partner-list). None, wenn die Seite keinen Eintrag enthält."""
    soup = BeautifulSoup(html or "", "lxml")
    d = StbvDetail(url)
    if (firma := soup.find(id="firmenname")) is not None:
        zeilen = _zeilen(firma)
        # „Koch, Bolz, … & von Heyer" ⏎ „Steuerberater" ⏎ „Partnerschaftsgesellschaft mbB": alles ist der Name
        d.name = zeilen_verbinden(zeilen) or None
        d.beruf = next((z for z in zeilen[1:] if _BERUF_ZEILE.match(z)), None)
    else:
        d.name = _knoten_text(soup.select_one("p.name"))
        d.beruf = _knoten_text(soup.select_one("p.beruf"))
        d.titel = _knoten_text(soup.select_one("p.akademische-grade"))
    if not d.name:
        return None
    d.rechtsform = _feld(soup, "rechtsform")
    d.strasse, d.plz, d.ort = _adresse_teilen(_zeilen(soup.find(id="adresse")))
    d.telefon, d.mobil, d.fax = _feld(soup, "telefon"), _feld(soup, "mobil"), _feld(soup, "fax")
    if (mail := soup.select_one("#email a[href^='mailto:']")) is not None:
        d.email = mail["href"][7:].split("?")[0].strip().lower() or None
    else:
        d.email = _feld(soup, "email")
    if (web := soup.select_one("#internetseite [data-url]")) is not None:
        d.internet = web["data-url"].strip() or None
    else:
        d.internet = _feld(soup, "internetseite")
    d.safe_id = _feld(soup, "safeid")
    if (kammer := soup.find(id="regionalkammerSection")) is not None and (p := kammer.find("p")) is not None:
        d.kammer = _knoten_text(p)
    for eintrag in _liste(soup, "other-advisory-offices-list"):
        teile = [t.strip() for t in eintrag.split(",")]
        d.beratungsstellen.append(_adresse_teilen([", ".join(teile[:-1]), teile[-1]] if len(teile) > 1 else teile))
    d.geschaeftsfuehrung = _liste(soup, "management-body-list")
    d.gesellschafter = _liste(soup, "authorized-partner-list")
    return d


_BERUF_ZEILE = re.compile(r"^(?:steuerberater|steuerbevollmächtigte|wirtschaftsprüfer|rechtsanw|vereidigte)", re.I)
