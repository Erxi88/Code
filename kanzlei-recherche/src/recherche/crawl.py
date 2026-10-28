"""Crawling der Kandidaten-Websites: Startseite, Impressum, Kontakt, Team.

- Start bei https://<domain>/ (Rückfall http://, dann die gefundene Treffer-URL)
- Unterseiten über Pfad und Linktext erkennen; ohne Impressum-Link wird /impressum geraten
- höchstens 1 Anfrage/Sekunde je Domain (inkl. robots.txt), viele Domains parallel
- Timeout und Retries aus settings.yaml, robots.txt nach RFC 9309
- jede URL wird einmal geladen und als HTML (gzip) + bereinigter Text in SQLite gecacht
- Playwright nur als Rückfall für fast leere Seiten und nur, wenn in settings.yaml aktiviert
- Fehler landen mit Grund und Discovery-Daten im Blatt „Nicht gefunden"

Der gespeicherte Text entsteht aus dem ganzen <body> (Navigation und Fußzeile bleiben
erhalten – dort stehen oft Adresse und Telefon). trafilatura liefert zusätzlich den
Haupttext für den LLM-Prompt (haupttext()).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Comment

from .config import CrawlConfig
from .models import Kandidat, NichtGefunden, SchrittStatus, Seite, Seitentyp
from .robots import Robots, produkt_token
from .search.pipeline import ist_blockiert, normalize_domain
from .storage import Storage
from .textnorm import braucht_normalisierung, text_normalisieren

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------- HTML → Text

_ENTFERNEN = ("script", "style", "noscript", "template", "svg", "iframe", "object", "canvas", "head")
_BLOCK = frozenset(
    "address article aside blockquote br dd div dl dt fieldset figcaption figure footer form h1 h2 h3 h4 h5 "
    "h6 header hr li main nav ol p pre section table tbody tfoot thead tr ul".split()
)


# Cookie-/Consent-Banner (id/class); deren Texte sind oft länger als das Impressum selbst
_COOKIE_ELEMENTE = re.compile(
    r"cookie|consent|gdpr|\bcmp\b|borlabs|usercentrics|cookiebot|klaro|complianz|onetrust|cc-banner|cc-window|"
    r"tarteaucitron|iubenda|cookie-notice|privacy-banner",
    re.I,
)
_NIE_ENTFERNEN = frozenset({"html", "body", "main", "article"})


def _cookie_banner_entfernen(soup: BeautifulSoup) -> int:
    """Entfernt Cookie-/Consent-Elemente; gibt die Zahl entfernter Elemente zurück."""
    treffer = []
    for el in soup.find_all(True):
        if el.name in _NIE_ENTFERNEN or el.attrs is None:
            continue
        kennung = " ".join([el.get("id") or "", *(el.get("class") or [])])
        if kennung.strip() and _COOKIE_ELEMENTE.search(kennung):
            treffer.append(el)
    for el in treffer:
        if not el.decomposed:
            el.decompose()
    return len(treffer)


def _soup(html: str) -> BeautifulSoup:
    try:
        soup = BeautifulSoup(text_normalisieren(html) or "", "lxml")
    except Exception:  # pragma: no cover – lxml fehlt
        soup = BeautifulSoup(text_normalisieren(html) or "", "html.parser")
    _normalisieren(soup)
    return soup


def _normalisieren(soup: BeautifulSoup) -> None:
    """Auch Entities (&shy;, &nbsp;, &#8203;) erst nach dem Parsen bereinigen: Textknoten und Attribute."""
    for s in soup.find_all(string=True):
        if braucht_normalisierung(s):
            s.replace_with(type(s)(text_normalisieren(str(s))))  # Typ erhalten (Kommentar bleibt Kommentar)
    for el in soup.find_all(True):
        for k, v in list(el.attrs.items()):
            if isinstance(v, str) and braucht_normalisierung(v):
                el[k] = text_normalisieren(v)


def cf_email_decode(kodiert: str) -> str | None:
    """Cloudflare-E-Mail-Schutz (data-cfemail) entschlüsseln."""
    try:
        schluessel = int(kodiert[:2], 16)
        return "".join(chr(int(kodiert[i : i + 2], 16) ^ schluessel) for i in range(2, len(kodiert), 2))
    except ValueError:
        return None


def _cf_emails_ersetzen(soup: BeautifulSoup) -> None:
    for el in soup.select("[data-cfemail]"):
        if (mail := cf_email_decode(el.get("data-cfemail", ""))) is not None:
            el.replace_with(mail)
    for a in soup.select('a[href*="/cdn-cgi/l/email-protection#"]'):
        mail = cf_email_decode(a["href"].split("#", 1)[1])
        if mail:
            a["href"] = f"mailto:{mail}"


def html_zu_text(html: str) -> str:
    """Ganzer <body> als Text mit Zeilenumbrüchen je Block; Navigation und Fußzeile bleiben."""
    soup = _soup(html)
    for tag in soup(_ENTFERNEN):
        tag.decompose()
    _cookie_banner_entfernen(soup)
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    _cf_emails_ersetzen(soup)
    for el in soup.find_all(True):
        if el.name in _BLOCK:
            el.insert_before("\n")
            el.insert_after("\n")
        elif el.name in ("td", "th", "span", "a", "strong", "b", "em", "i", "small", "label"):
            el.insert_after(" ")
    wurzel = soup.body or soup
    zeilen: list[str] = []
    for zeile in wurzel.get_text("").splitlines():
        zeile = " ".join(zeile.replace("\xa0", " ").split())
        if zeile and (not zeilen or zeilen[-1] != zeile):
            zeilen.append(zeile)
    return "\n".join(zeilen)


def html_ohne_cookie_banner(html: str) -> str:
    soup = _soup(html)
    _cookie_banner_entfernen(soup)
    return str(soup)


def haupttext(html: str) -> str | None:
    """Haupttext ohne Navigation (trafilatura) – kürzerer Text für den LLM-Prompt."""
    try:
        import trafilatura

        return text_normalisieren(trafilatura.extract(html_ohne_cookie_banner(html), include_comments=False,
                                                      include_tables=True, favor_recall=True))  # fmt: skip
    except Exception:  # pragma: no cover – trafilatura-Fehler bei kaputtem HTML
        return None


def meta_texte(html: str | None) -> list[str]:
    """Inhalte von <meta name="description">, og:description, keywords – manche Kanzleien nennen die
    E-Mail-Adresse nur dort."""
    if not html:
        return []
    soup = _soup(html)
    texte = []
    for m in soup.find_all("meta"):
        art = (m.get("name") or m.get("property") or "").lower()
        if art in ("description", "og:description", "twitter:description", "keywords") and m.get("content"):
            texte.append(m["content"])
    return texte


def links(html: str, basis_url: str) -> list[tuple[str, str]]:
    """(absolute URL, Linktext) aller <a href> – inkl. entschlüsselter Cloudflare-Mails."""
    soup = _soup(html)
    _cf_emails_ersetzen(soup)
    ergebnis = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = " ".join(a.get_text(" ").split()) or (a.get("aria-label") or a.get("title") or "").strip()
        if href.lower().startswith(("mailto:", "tel:")):
            ergebnis.append((href, text))
        elif not href.lower().startswith(("javascript:", "data:", "#")):
            ergebnis.append((urljoin(basis_url, href), text))
    return ergebnis


# ---------------------------------------------------------------------- Unterseiten erkennen

_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_DATEI_ENDUNGEN = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".mp4")

# Schlüsselwort im Pfadsegment bzw. im Linktext → Punkte. "=wort" verlangt genau diesen Linktext.
MUSTER: dict[Seitentyp, dict[str, list[tuple[str, int]]]] = {
    Seitentyp.IMPRESSUM: {
        "pfad": [("impressum", 10), ("imprint", 8), ("anbieterkennzeichnung", 8), ("legal-notice", 6),
                 ("rechtliche-hinweise", 6), ("rechtliches", 5)],
        "text": [("impressum", 10), ("imprint", 8), ("anbieterkennzeichnung", 8), ("legal notice", 8),
                 ("rechtliche hinweise", 6), ("rechtliches", 6), ("pflichtangaben", 6)],
    },
    Seitentyp.KONTAKT: {
        "pfad": [("kontakt", 10), ("contact", 8), ("anfahrt", 5), ("standort", 4)],
        "text": [("kontakt", 10), ("contact", 8), ("anfahrt", 5), ("so finden sie uns", 5), ("standort", 4)],
    },
    Seitentyp.TEAM: {
        "pfad": [("team", 10), ("mitarbeiter", 9), ("ansprechpartner", 9), ("berufstraeger", 9), ("ueber-uns", 7),
                 ("uber-uns", 7), ("about", 5), ("wir", 6), ("partner", 6), ("experten", 6), ("personen", 5),
                 ("unsere-steuerberater", 10), ("koepfe", 8),
                 ("kanzlei", 5), ("menschen", 5)],
        "text": [("unser team", 12), ("team", 10), ("mitarbeiter", 9), ("ansprechpartner", 9), ("berufstraeger", 9),
                 ("ueber uns", 7), ("=partner", 6), ("unsere partner", 6), ("experten", 6), ("berater", 9),
                 ("unsere steuerberater", 11), ("=steuerberater", 9), ("=steuerberaterinnen", 9), ("beraterinnen", 9),
                 ("koepfe", 8), ("=menschen", 7), ("wir ueber uns", 8), ("die kanzlei", 5),
                 ("=kanzlei", 5), ("=wir", 6), ("menschen", 5)],
    },
}  # fmt: skip
# Abzüge: Schlüsselwort in Pfad oder Linktext → Punkte (nicht, wenn das Typ-Wort selbst auch vorkommt)
ABZUEGE: dict[Seitentyp, list[tuple[str, int]]] = {
    Seitentyp.IMPRESSUM: [("datenschutz", -15)],
    Seitentyp.KONTAKT: [("datenschutz", -15), ("impressum", -6)],
    Seitentyp.TEAM: [("karriere", -8), ("jobs", -8), ("stellen", -8), ("ausbildung", -6), ("datenschutz", -15),
                     ("kooperationspartner", -8), ("netzwerk", -4), ("honorar", -8), ("preise", -6),
                     ("gebuehren", -6), ("empfehlungen", -8), ("digitale", -5), ("leistungen", -8)],
}
TYP_WORT = {Seitentyp.IMPRESSUM: "impressum", Seitentyp.KONTAKT: "kontakt", Seitentyp.TEAM: "team"}
MINDESTPUNKTE = 5


def _norm(text: str) -> str:
    return text.lower().translate(_UMLAUTE)


def _segmente(pfad: str) -> list[str]:
    segmente = []
    for s in _norm(pfad).strip("/").split("/"):
        s = re.sub(r"\.(html?|php|aspx?)$", "", s)
        if s:
            segmente.append(s)
    return segmente


def _wort_in(wort: str, text: str) -> bool:
    """Wort als eigenes Teilwort (getrennt durch Nicht-Buchstaben) – 'wir' trifft nicht 'wirtschaft'."""
    return re.search(rf"(?<![a-z]){re.escape(wort)}(?![a-z])", text) is not None


def punkte(typ: Seitentyp, url: str, text: str) -> int:
    """Punkte eines Links für einen Seitentyp (Pfad und Linktext; Bonus, wenn beides passt)."""
    segmente = _segmente(urlsplit(url).path)
    linktext = " ".join(_norm(text).split())

    def im_pfad(wort: str) -> bool:
        return any(_wort_in(wort, s) or (len(wort) > 5 and wort in s) for s in segmente)

    def im_text(wort: str) -> bool:
        return linktext == wort[1:] if wort.startswith("=") else _wort_in(wort, linktext)

    pfad_p = max((w for wort, w in MUSTER[typ]["pfad"] if im_pfad(wort)), default=0)
    # langer Linktext ist Fließtext („07 Lohnbuchhaltung Unsere Experten übernehmen …"): nur der Pfad zählt
    text_p = 0 if len(linktext.split()) > 5 else max((w for wort, w in MUSTER[typ]["text"] if im_text(wort)), default=0)
    p = max(pfad_p, text_p) + (2 if pfad_p and text_p else 0)
    typ_wort = TYP_WORT[typ]
    if not (im_pfad(typ_wort) or im_text(typ_wort)) or typ != Seitentyp.IMPRESSUM:
        for wort, abzug in ABZUEGE[typ]:
            if wort != typ_wort and (im_pfad(wort) or im_text(wort)):
                p += abzug
    return p


def gleiche_site(url: str, domain: str) -> bool:
    d = normalize_domain(url)
    return d is not None and (d == domain or d.endswith("." + domain))


def finde_unterseiten(html: str, basis_url: str, domain: str) -> dict[Seitentyp, str]:
    """Beste URL je Seitentyp (Impressum, Kontakt, Team) auf derselben Website; dazu eine zweite
    Team-Kandidatin (TEAM2, z. B. /berufstraeger neben /ueber-uns), wenn es eine gibt."""
    kandidaten = link_kandidaten(html, basis_url, domain)
    ergebnis = {typ: liste[0][2] for typ, liste in kandidaten.items() if liste}
    team = kandidaten.get(Seitentyp.TEAM, [])
    zweite = [u for _, _, u, _ in team[1:] if u not in ergebnis.values()]
    if zweite:
        ergebnis[Seitentyp.TEAM2] = zweite[0]
    return ergebnis


def link_kandidaten(html: str, basis_url: str, domain: str) -> dict[Seitentyp, list[tuple[int, int, str, str]]]:
    """Alle passenden Links je Typ, beste zuerst: (Punkte, -Länge, URL, Linktext)."""
    beste: dict[Seitentyp, dict[str, tuple[int, int, str, str]]] = {}
    start = _ohne_fragment(basis_url)
    for url, text in links(html, basis_url):
        if url.lower().startswith(("mailto:", "tel:")) or not gleiche_site(url, domain):
            continue
        url = _ohne_fragment(url)
        if url == start or urlsplit(url).path.lower().endswith(_DATEI_ENDUNGEN):
            continue
        for typ in MUSTER:
            p = punkte(typ, url, text)
            if p < MINDESTPUNKTE:
                continue
            je_url = beste.setdefault(typ, {})
            if url not in je_url or p > je_url[url][0]:
                je_url[url] = (p, -len(url), url, text)
    return {typ: sorted(je_url.values(), reverse=True) for typ, je_url in beste.items()}


def _ohne_fragment(url: str) -> str:
    teile = urlsplit(url)
    return urlunsplit((teile.scheme, teile.netloc, teile.path or "/", teile.query, ""))


IMPRESSUM_RATEN = ("/impressum", "/impressum/", "/impressum.html")
# zusätzlich bei crawl --wiederholen
IMPRESSUM_RATEN_GRUENDLICH = ("/impressum.php", "/imprint", "/kontakt/impressum", "/rechtliches/",
                              "/impressum-datenschutz/", "/legal-notice")
# Seite ist ein Impressum (Soft-404-Prüfung beim Raten)
_IMPRESSUM_INHALT = re.compile(
    r"impressum|imprint|anbieterkennzeichnung|legal notice|angaben gemäß\s*§\s*5|§\s*5\s*(?:tmg|ddg)|§\s*18\s*mstv", re.I
)


# ---------------------------------------------------------------------- Abruf


@dataclass
class Abruf:
    url: str
    status: int | None = None
    html: str | None = None
    text: str | None = None
    final_url: str | None = None
    fehler: str | None = None  # gesetzt, wenn kein brauchbares HTML
    aus_cache: bool = False
    playwright: str | None = None  # Ergebnis des Playwright-Rückfalls (für fixture-add und Diagnose)

    @property
    def ok(self) -> bool:
        return self.fehler is None and self.html is not None


class DomainDrossel:
    """Mindestabstand zwischen zwei Anfragen an dieselbe Domain."""

    def __init__(self, abstand_s: float, clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]]):
        self.abstand_s = abstand_s
        self._clock = clock
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._letzte: dict[str, float] = {}

    async def warten(self, domain: str) -> None:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            if domain in self._letzte:
                rest = self.abstand_s - (self._clock() - self._letzte[domain])
                if rest > 0:
                    await self._sleep(rest)
            self._letzte[domain] = self._clock()


Renderer = Callable[[str], Awaitable[str | None]]


class _Verboten(Exception):
    """robots.txt verbietet das Ziel einer Weiterleitung."""


async def playwright_render(url: str, timeout_s: float = 20.0, user_agent: str | None = None) -> str | None:
    """HTML nach Ausführen von JavaScript (nur wenn crawl.playwright aktiviert ist)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            seite = await browser.new_page(user_agent=user_agent)
            await seite.goto(url, timeout=timeout_s * 1000, wait_until="networkidle")
            return await seite.content()
        finally:
            await browser.close()


@dataclass
class CrawlErgebnis:
    kandidat_id: int | None
    domain: str
    seiten: dict[Seitentyp, Abruf] = field(default_factory=dict)
    grund: str | None = None  # gesetzt → Nicht gefunden
    url: str | None = None  # betroffene URL für das Blatt „Nicht gefunden"
    anfragen: int = 0
    hinweis: str | None = None  # gelesen, aber z. B. robots.txt nicht abrufbar

    @property
    def ok(self) -> bool:
        return self.grund is None


DNS_GRUND = "Domain nicht erreichbar (DNS)"
_DNS_MELDUNG = re.compile(
    r"name or service not known|getaddrinfo|nodename nor servname|no address associated|could not resolve|"
    r"name resolution|temporary failure in name|errno -[235]\b|errno 11001|errno 11004|no such host",
    re.I,
)


def ist_dns_fehler(e: BaseException) -> bool:
    """Domain existiert nicht (mehr): DNS-Auflösung gescheitert – keine robots-Sperre."""
    return isinstance(e, httpx.ConnectError) and bool(_DNS_MELDUNG.search(f"{e} {e.__cause__ or ''}"))


def _robots_varianten(host: str) -> list[str]:
    """https://x.de → [https://x.de, http://x.de, https://www.x.de, http://www.x.de] (bzw. ohne www)."""
    teile = urlsplit(host)
    netloc = teile.netloc
    anders = netloc[4:] if netloc.startswith("www.") else f"www.{netloc}"
    schema2 = "http" if teile.scheme == "https" else "https"
    return list(dict.fromkeys([host, f"{schema2}://{netloc}", f"{teile.scheme}://{anders}", f"{schema2}://{anders}"]))


class Crawler:
    def __init__(
        self,
        cfg: CrawlConfig,
        storage: Storage,
        blocklist: set[str] | None = None,
        client: httpx.AsyncClient | None = None,
        renderer: Renderer | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        gruendlich: bool = False,
    ):
        self.cfg = cfg
        self.storage = storage
        self.blocklist = blocklist or set()
        self._client = client
        self._renderer = renderer
        self._sleep = sleep
        self.drossel = DomainDrossel(1.0 / max(cfg.requests_per_second_per_domain, 0.01), clock, sleep)
        # str = robots.txt nicht abrufbar (Grund); DNS_GRUND = Domain existiert nicht
        self._robots: dict[str, Robots | bool | str] = {}
        self.gruendlich = gruendlich  # crawl --wiederholen: mehr Impressum-Pfade raten
        self.anfragen = 0

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Weiterleitungen folgt hole() selbst – jede Anfrage läuft über die Drossel
            self._client = httpx.AsyncClient(
                timeout=self.cfg.timeout_s,
                follow_redirects=False,
                # vollständiger Kopfsatz wie ein Browser (manche Hoster lehnen Minimal-Anfragen ab);
                # der User-Agent bleibt ehrlich
                headers={"User-Agent": self.cfg.user_agent, "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                         "Accept-Encoding": "gzip, deflate"},
            )
        return self._client

    async def schliessen(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # -------------------------------------------------------------- robots.txt

    async def erlaubt(self, url: str) -> bool:
        """Nur echte Regeln sperren. Ist die robots.txt nach allen Varianten (http/https, mit/ohne www)
        nicht abrufbar (5xx, Timeout, Verbindungsfehler), wird die Seite trotzdem gelesen und markiert –
        RFC 9309 sieht die Sperre dann nur vorübergehend vor. Existiert die Domain nicht (DNS), nein."""
        if not self.cfg.respect_robots:
            return True
        regeln = await self._regeln(url)
        if isinstance(regeln, Robots):
            return regeln.erlaubt(self.cfg.user_agent, url)
        return regeln is True or (isinstance(regeln, str) and regeln != DNS_GRUND)

    async def robots_meldung(self, url: str) -> str:
        """Grund fürs Blatt „Nicht gefunden": echte Sperre oder tote Domain."""
        if (regeln := await self._regeln(url)) == DNS_GRUND:
            return DNS_GRUND
        return "robots.txt verbietet den Zugriff"

    async def robots_hinweis(self, url: str) -> str | None:
        """„robots.txt nicht abrufbar (HTTP 500)", wenn trotzdem gelesen wurde."""
        regeln = await self._regeln(url) if self.cfg.respect_robots else True
        return f"robots.txt nicht abrufbar ({regeln}), Seite gelesen" if isinstance(regeln, str) and regeln != DNS_GRUND else None

    async def _regeln(self, url: str) -> Robots | bool | str:
        teile = urlsplit(url)
        host = f"{teile.scheme}://{teile.netloc}"
        if host not in self._robots:
            self._robots[host] = await self._lade_robots(host)
        return self._robots[host]

    async def _robots_abrufen(self, url: str) -> dict:
        """Ein Abruf von <url>/robots.txt → {status, text, fehler, url} (Weiterleitungen folgen)."""
        await self.drossel.warten(normalize_domain(url) or url)
        self.anfragen += 1
        try:
            resp = await self.client.get(f"{url}/robots.txt", follow_redirects=True)
            return {"status": resp.status_code, "text": resp.text if resp.status_code == 200 else "", "url": url}
        except httpx.TimeoutException:
            return {"status": None, "text": "", "fehler": "Timeout", "url": url}
        except httpx.HTTPError as e:
            return {"status": None, "text": "", "fehler": "DNS" if ist_dns_fehler(e) else type(e).__name__, "url": url}

    async def _lade_robots(self, host: str) -> Robots | bool | str:
        """RFC 9309: 200 → Regeln; 4xx → alles erlaubt; Weiterleitungen folgen. Bei 5xx, Timeout oder
        Verbindungsfehler werden http/https und mit/ohne www probiert; bleibt sie unerreichbar → Grund-Text
        (Seite wird mit Hinweis gelesen); scheitert überall DNS → DNS_GRUND. Fehler werden gecacht,
        bei `crawl --wiederholen` aber neu versucht."""
        gecacht = self.storage.cache_get("robots", host)
        neu_versuchen = gecacht is not None and self.gruendlich and (gecacht["status"] is None or gecacht["status"] >= 500)
        if gecacht is None or neu_versuchen:
            versuche = []
            for url in _robots_varianten(host):
                versuche.append(v := await self._robots_abrufen(url))
                if v["status"] is not None and v["status"] < 500:
                    break
            gecacht = versuche[-1] if versuche[-1]["status"] is not None and versuche[-1]["status"] < 500 else (
                next((v for v in versuche if v.get("fehler") != "DNS"), versuche[0]))
            gecacht["versuche"] = [f"{v['url']}: {v.get('fehler') or 'HTTP ' + str(v['status'])}" for v in versuche]
            self.storage.cache_put("robots", host, gecacht)
        status = gecacht["status"]
        if status is None:
            fehler = gecacht.get("fehler") or "nicht erreichbar"
            return DNS_GRUND if fehler == "DNS" else fehler
        if status >= 500:
            return f"HTTP {status}"
        if status != 200:
            return True
        return Robots.parse(gecacht["text"])

    # -------------------------------------------------------------- Seiten

    async def hole(self, url: str, domain: str, typ: Seitentyp) -> Abruf:
        """Lädt eine Seite (oder nimmt sie aus dem Cache). Fehler stehen in Abruf.fehler."""
        if (seite := self.storage.get_seite(url)) is not None:
            fehler = None if seite.html is not None else f"HTTP {seite.http_status}"
            return Abruf(url, seite.http_status, seite.html, seite.text, seite.final_url, fehler, aus_cache=True)
        if not await self.erlaubt(url):
            return Abruf(url, fehler=await self.robots_meldung(url))
        abruf = await self._mit_retries(url, domain)
        if abruf.status == 403 and typ == Seitentyp.START and self.cfg.playwright:
            abruf = await self._rendern_nach_403(abruf)
        wenig_text = abruf.ok and (len(abruf.text or "") < self.cfg.min_text_zeichen or self._team_ohne_text(abruf, typ))
        if wenig_text and self.cfg.playwright:
            abruf = await self._rendern(abruf)
        elif wenig_text:
            abruf.playwright = "nötig, aber crawl.playwright ist aus"
        if abruf.ok or (abruf.status is not None and abruf.status < 500 and abruf.status != 429):
            # endgültige Antworten cachen (auch 404); Netzfehler, 429 und 5xx sind vorübergehend
            # und werden beim nächsten Lauf (crawl --wiederholen) erneut versucht
            self.storage.save_seite(
                Seite(url=url, domain=domain, seitentyp=typ, http_status=abruf.status, text=abruf.text,
                      html=abruf.html, final_url=abruf.final_url)
            )  # fmt: skip
        return abruf

    async def _mit_retries(self, url: str, domain: str) -> Abruf:
        versuche = self.cfg.retries + 1
        abruf = Abruf(url)
        for versuch in range(1, versuche + 1):
            try:
                resp = await self._get_mit_weiterleitungen(url, domain)
            except httpx.TimeoutException:
                abruf = Abruf(url, fehler=f"Timeout nach {self.cfg.timeout_s:.0f} s")
            except httpx.HTTPError as e:
                if ist_dns_fehler(e):
                    return Abruf(url, fehler=DNS_GRUND)  # kein Retry: die Domain gibt es nicht
                abruf = Abruf(url, fehler=f"nicht erreichbar ({type(e).__name__})")
            except _Verboten:
                return Abruf(url, fehler="robots.txt verbietet den Zugriff (nach Weiterleitung)")
            else:
                abruf = self._auswerten(url, resp)
                if abruf.status is None or abruf.status < 500:
                    return abruf
            if versuch < versuche:
                await self._sleep(self.cfg.backoff_s * 2 ** (versuch - 1))
        return abruf

    async def _get_mit_weiterleitungen(self, url: str, domain: str, max_weiterleitungen: int = 5) -> httpx.Response:
        """GET mit selbst verfolgten Weiterleitungen: jede Anfrage gedrosselt, robots.txt je Ziel geprüft."""
        aktuell = url
        for _ in range(max_weiterleitungen + 1):
            ziel_domain = normalize_domain(aktuell) or domain
            if aktuell != url and not await self.erlaubt(aktuell):
                raise _Verboten(aktuell)
            await self.drossel.warten(domain if gleiche_site(aktuell, domain) else ziel_domain)
            self.anfragen += 1
            resp = await self.client.get(aktuell)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                aktuell = urljoin(aktuell, resp.headers["location"])
                continue
            return resp
        raise httpx.TooManyRedirects(f"mehr als {max_weiterleitungen} Weiterleitungen", request=resp.request)

    def _auswerten(self, url: str, resp: httpx.Response) -> Abruf:
        final = str(resp.url)
        if resp.status_code >= 400:
            return Abruf(url, resp.status_code, final_url=final, fehler=f"HTTP {resp.status_code}")
        art = resp.headers.get("content-type", "text/html").lower()
        if "html" not in art and "xml" not in art:
            return Abruf(url, resp.status_code, final_url=final, fehler=f"kein HTML ({art.split(';')[0]})")
        html = resp.text[: self.cfg.max_bytes]
        return Abruf(url, resp.status_code, html, html_zu_text(html), final)

    async def _rendern_nach_403(self, abruf: Abruf) -> Abruf:
        """Bot-Schutz: Startseite mit HTTP 403 im echten Browser versuchen."""
        renderer = self._renderer or (
            lambda u: playwright_render(u, self.cfg.playwright_timeout_s, self.cfg.user_agent)
        )
        try:
            html = await renderer(abruf.url)
        except Exception as e:  # Playwright nicht installiert, Timeout …
            abruf.playwright = f"nach HTTP 403 fehlgeschlagen ({type(e).__name__})"
            return abruf
        text = html_zu_text(html) if html else ""
        if len(text) >= self.cfg.min_text_zeichen:
            return Abruf(abruf.url, 200, html, text, abruf.url, playwright="gerendert nach HTTP 403 (Bot-Schutz)")
        abruf.playwright = "nach HTTP 403 gerendert, aber ohne Text"
        return abruf

    def _team_ohne_text(self, abruf: Abruf, typ: Seitentyp) -> bool:
        """Teamseite mit großem HTML, aber kaum Text (Namen per JavaScript nachgeladen)."""
        html = len(abruf.html or "")
        return (typ in (Seitentyp.TEAM, Seitentyp.TEAM2) and html >= self.cfg.playwright_team_min_html
                and len(abruf.text or "") < self.cfg.playwright_team_min_anteil * html)  # fmt: skip

    async def _rendern(self, abruf: Abruf) -> Abruf:
        renderer = self._renderer or (
            lambda u: playwright_render(u, self.cfg.playwright_timeout_s, self.cfg.user_agent)
        )
        try:
            html = await renderer(abruf.final_url or abruf.url)
        except Exception as e:  # Playwright nicht installiert, Timeout …
            log.warning("Playwright-Rückfall für %s fehlgeschlagen: %s", abruf.url, e)
            text = str(e).splitlines()[0][:120] if str(e) else ""
            tipp = " – Browser fehlt: setup.sh --mit-playwright bzw. playwright install chromium" if (
                isinstance(e, ImportError) or "Executable doesn't exist" in str(e) or "playwright install" in str(e)) else ""
            abruf.playwright = f"fehlgeschlagen ({type(e).__name__}: {text}){tipp}"
            return abruf
        vorher = len(abruf.text or "")
        if html and len(text := html_zu_text(html)) > vorher:
            abruf.html, abruf.text = html, text
            abruf.playwright = f"gerendert ({vorher} → {len(text)} Zeichen Text)"
        else:
            abruf.playwright = f"gerendert, aber nicht mehr Text ({vorher} Zeichen) – ursprüngliches HTML behalten"
        return abruf

    # -------------------------------------------------------------- Kandidat

    async def crawl_kandidat(self, k: Kandidat) -> CrawlErgebnis:
        domain = k.domain or normalize_domain(k.website or "") or ""
        erg = CrawlErgebnis(k.id, domain)
        anfragen_vorher = self.anfragen
        try:
            await self._crawl(k, domain, erg)
        finally:
            erg.anfragen = self.anfragen - anfragen_vorher
        return erg

    async def _crawl(self, k: Kandidat, domain: str, erg: CrawlErgebnis) -> None:
        startkandidaten = [f"https://{domain}/", f"http://{domain}/"]
        if k.website and _ohne_fragment(k.website) not in startkandidaten:
            startkandidaten.append(_ohne_fragment(k.website))
        start: Abruf | None = None
        for url in startkandidaten:
            abruf = await self.hole(url, domain, Seitentyp.START)
            if abruf.ok:
                start = abruf
                break
            start = start or abruf
            if abruf.fehler and ("robots.txt" in abruf.fehler or abruf.fehler == DNS_GRUND):
                break
        if start is None or not start.ok:
            erg.grund = f"Startseite: {start.fehler if start else 'nicht erreichbar'}"
            erg.url = startkandidaten[0]
            return
        erg.seiten[Seitentyp.START] = start
        erg.hinweis = await self.robots_hinweis(start.url)
        ziel = normalize_domain(start.final_url or start.url) or domain
        if ziel != domain and not ziel.endswith("." + domain) and ist_blockiert(ziel, self.blocklist):
            erg.grund, erg.url = f"Weiterleitung auf Portal ({ziel})", start.final_url
            return
        basis = start.final_url or start.url
        site = ziel if ziel != domain else domain
        if len(start.text or "") < self.cfg.min_text_zeichen and not finde_unterseiten(start.html, basis, site):
            erg.grund, erg.url = "Seite ohne Text (vermutlich JavaScript)", basis
            return

        gefunden = finde_unterseiten(start.html, basis, site)
        geladen: dict[str, Abruf] = {start.url: start}
        budget = self.cfg.max_seiten_je_domain - 1

        async def laden(url: str, typ: Seitentyp) -> Abruf | None:
            nonlocal budget
            if url not in geladen:
                if budget <= 0:
                    return None
                geladen[url] = await self.hole(url, site, typ)
                budget -= 1
            return geladen[url]

        # Impressum zuerst: über den Link, sonst geraten – vor Kontakt/Team, damit das Budget reicht
        if (url := gefunden.get(Seitentyp.IMPRESSUM)) and (abruf := await laden(url, Seitentyp.IMPRESSUM)) and abruf.ok:
            erg.seiten[Seitentyp.IMPRESSUM] = abruf
        probiert: list[str] = []
        if Seitentyp.IMPRESSUM not in erg.seiten:
            raten = IMPRESSUM_RATEN + (IMPRESSUM_RATEN_GRUENDLICH if self.gruendlich else ())
            for pfad in raten:
                url = urljoin(basis, pfad)
                if url in geladen:
                    continue
                if (abruf := await laden(url, Seitentyp.IMPRESSUM)) is None:
                    break
                probiert.append(pfad)
                # Soft-404: manche Server liefern für unbekannte Pfade die Startseite aus
                if abruf.ok and _IMPRESSUM_INHALT.search(abruf.text or "") and abruf.text != start.text:
                    erg.seiten[Seitentyp.IMPRESSUM] = abruf
                    break
        for typ in (Seitentyp.KONTAKT, Seitentyp.TEAM, Seitentyp.TEAM2):
            if (url := gefunden.get(typ)) and (abruf := await laden(url, typ)) and abruf.ok:
                erg.seiten[typ] = abruf
        if Seitentyp.IMPRESSUM not in erg.seiten:
            link = f"Link {urlsplit(gefunden[Seitentyp.IMPRESSUM]).path} ohne Erfolg, " if Seitentyp.IMPRESSUM in gefunden else ""
            erg.grund = f"Kein Impressum gefunden ({link}probiert: {', '.join(probiert) or '–'})"
            erg.url = basis

    # -------------------------------------------------------------- Lauf

    async def run(
        self, kandidaten: list[Kandidat], bundesland: str, fortschritt: Callable[[str], None] | None = None
    ) -> CrawlStatistik:
        stat = CrawlStatistik()
        sem = asyncio.Semaphore(max(1, self.cfg.parallel_domains))

        async def einer(k: Kandidat) -> None:
            async with sem:
                try:
                    erg = await self.crawl_kandidat(k)
                except Exception as e:  # unerwarteter Fehler: Kandidat markieren, Lauf geht weiter
                    log.exception("Crawl %s fehlgeschlagen", k.domain)
                    erg = CrawlErgebnis(k.id, k.domain or "", grund=f"Fehler: {type(e).__name__}: {e}")
            self.speichern(k, erg, bundesland)
            stat.zaehlen(erg)
            if fortschritt:
                zusatz = f", {erg.hinweis}" if erg.hinweis else ""
                fortschritt(f"{erg.domain}: {'ok' if erg.ok else erg.grund} ({len(erg.seiten)} Seiten{zusatz})")

        await asyncio.gather(*(einer(k) for k in kandidaten))
        stat.anfragen = self.anfragen
        return stat

    def speichern(self, k: Kandidat, erg: CrawlErgebnis, bundesland: str) -> None:
        for typ, abruf in erg.seiten.items():
            self.storage.setze_kandidat_seite(k.id, typ, abruf.url)
        if erg.ok:
            self.storage.set_crawl_status(k.id, SchrittStatus.FERTIG)
            if k.domain:
                self.storage.entferne_nicht_gefunden(k.domain)
            return
        self.storage.set_crawl_status(k.id, SchrittStatus.FEHLER, erg.grund)
        self.storage.add_nicht_gefunden(
            NichtGefunden(
                domain=k.domain or erg.domain, bundesland=bundesland, grund=erg.grund, url=erg.url,
                kandidat_id=k.id, name=_discovery_name(self.storage, k), strasse=k.strasse, plz=k.plz,
                ort=k.ort, telefon=k.telefon, website=k.website,
            )
        )  # fmt: skip


def _discovery_name(storage: Storage, k: Kandidat) -> str | None:
    """Name aus der Discovery – Websuche-Titel gelten nicht als Name (siehe extract.merge)."""
    from .extract.merge import discovery_werte

    return discovery_werte(storage.funde_kandidat(k.id)).get("name") or k.name


@dataclass
class CrawlStatistik:
    fertig: int = 0
    nicht_gefunden: int = 0
    gruende: dict[str, int] = field(default_factory=dict)
    anfragen: int = 0
    trotz_robots: int = 0  # gelesen, obwohl robots.txt nicht abrufbar war
    dns: int = 0  # tote Domains

    def zaehlen(self, erg: CrawlErgebnis) -> None:
        if erg.ok:
            self.fertig += 1
            self.trotz_robots += erg.hinweis is not None
            return
        self.dns += erg.grund is not None and DNS_GRUND in erg.grund
        self.nicht_gefunden += 1
        kurz = re.sub(r"\s*\(.*\)$", "", erg.grund or "")
        self.gruende[kurz] = self.gruende.get(kurz, 0) + 1


def run_crawl(
    storage: Storage,
    cfg: CrawlConfig,
    bundesland: str,
    blocklist: set[str],
    limit: int | None = None,
    client: httpx.AsyncClient | None = None,
    renderer: Renderer | None = None,
    fortschritt: Callable[[str], None] | None = None,
    gruendlich: bool = False,
) -> CrawlStatistik:
    """Crawlt alle offenen Kandidaten mit Website (wiederaufnehmbar über crawl_status).
    `gruendlich` (crawl --wiederholen): zusätzliche Impressum-Pfade raten."""
    kandidaten = storage.kandidaten(bundesland, crawl_status=SchrittStatus.OFFEN, nur_mit_website=True)
    if limit:
        kandidaten = kandidaten[:limit]

    async def lauf() -> CrawlStatistik:
        crawler = Crawler(cfg, storage, blocklist, client=client, renderer=renderer, gruendlich=gruendlich)
        try:
            return await crawler.run(kandidaten, bundesland, fortschritt)
        finally:
            await crawler.schliessen()

    return asyncio.run(lauf())


# ---------------------------------------------------------------------- Diagnose


async def robots_check(cfg: CrawlConfig, domain: str, client: httpx.AsyncClient | None = None) -> list[str]:
    """`recherche robots-check --domain X`: robots.txt aller Varianten roh abrufen (Status, Fehler,
    Weiterleitungskette), Gruppe und entscheidende Regel für die Startseite, dann die Startseite selbst.
    Ohne Cache und ohne Drossel-Wartezeit über mehrere Domains – nur zur Diagnose einer Domain."""
    zeilen: list[str] = []
    eigener = client is None
    client = client or httpx.AsyncClient(timeout=cfg.timeout_s, follow_redirects=False, headers={
        "User-Agent": cfg.user_agent, "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5"})

    async def abrufen(url: str) -> tuple[httpx.Response | None, list[str], str | None]:
        kette: list[str] = []
        try:
            for _ in range(6):
                resp = await client.get(url)
                kette.append(f"{resp.status_code} {url}")
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    url = urljoin(url, resp.headers["location"])
                    continue
                return resp, kette, None
            return None, kette, "zu viele Weiterleitungen"
        except httpx.HTTPError as e:
            art = "DNS – Domain existiert nicht" if ist_dns_fehler(e) else type(e).__name__
            return None, kette, f"{art}: {e}"

    try:
        domain = normalize_domain(domain) or domain
        entscheidung = None
        for host in _robots_varianten(f"https://{domain}"):
            resp, kette, fehler = await abrufen(f"{host}/robots.txt")
            zeilen.append(f"robots.txt {host}:")
            zeilen += [f"    {k}" for k in kette]
            if fehler:
                zeilen.append(f"    Fehler: {fehler}")
                continue
            if resp.status_code != 200:
                zeilen.append(f"    HTTP {resp.status_code} → {'alles erlaubt (4xx)' if resp.status_code < 500 else 'nicht abrufbar (5xx)'}")
                if resp.status_code < 500 and entscheidung is None:
                    entscheidung = f"erlaubt (robots.txt {host}: HTTP {resp.status_code})"
                continue
            robots = Robots.parse(resp.text)
            ok, regel = robots.entscheidung(cfg.user_agent, f"{host}/")
            token = produkt_token(cfg.user_agent)
            zeilen.append(f"    {len(resp.text)} Zeichen, {len(robots.gruppen)} Gruppen; für „{token}\" gilt Gruppe "
                          f"„{robots.gruppe_fuer(token)}\"; Startseite: {'erlaubt' if ok else 'VERBOTEN'}"
                          f"{f' durch {regel}' if regel else ' (keine passende Regel)'}")
            if entscheidung is None:
                entscheidung = f"{'erlaubt' if ok else 'verboten'} (robots.txt {host}{f', {regel}' if regel else ''})"
        zeilen.append(f"Entscheidung: {entscheidung or 'robots.txt nirgends abrufbar → Seite wird mit Hinweis gelesen (außer bei DNS-Fehler)'}")
        resp, kette, fehler = await abrufen(f"https://{domain}/")
        zeilen.append(f"Startseite https://{domain}/:")
        zeilen += [f"    {k}" for k in kette]
        if fehler:
            zeilen.append(f"    Fehler: {fehler}")
        elif resp is not None:
            zeilen.append(f"    HTTP {resp.status_code}, {len(resp.text)} Zeichen, {resp.headers.get('content-type', '?')}")
    finally:
        if eigener:
            await client.aclose()
    return zeilen
