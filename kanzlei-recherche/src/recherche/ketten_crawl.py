"""Standortlisten-Crawler für Ketten und Großgesellschaften (Prüfkatalog 9, `recherche ketten`).

Je Kette aus `config/ketten.yaml`: die Übersichtsseite laden (robots.txt beachtet, Antwort gecacht), alle Links
sammeln, die auf `link_muster` passen, und je Standort einen Kandidaten anlegen – verknüpft mit der Zentrale über
einen Fund der Quelle „kette" („Standort der Kette … (Zentrale …)") und die Kette in ketten.yaml (Spalten
„Zentrale/Standort", „Zentrale", „Kanzleityp"). Die Standorte laufen danach durch crawl und extract; dort gilt die
Standortseiten-Regel (Standortseite vor Zentrale).

Standortmuster: `subdomain` / `domain` → Kandidat mit eigener Domain; `pfad` → Kandidat ohne Domain mit der
Standort-URL als Website (die Domain gehört der Zentrale und ist je Kandidat eindeutig).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .ketten import Kette
from .models import Fund, Kandidat
from .robots import Robots
from .search.pipeline import normalize_domain
from .storage import Storage

QUELLE = "kette"


def _normal(url: str) -> str:
    t = urlsplit(url)
    pfad = t.path.rstrip("/") or "/"
    return urlunsplit((t.scheme.lower() or "https", t.netloc.lower(), pfad, "", ""))


def standort_links(html: str, basis_url: str, kette: Kette) -> list[str]:
    """Absolute Standort-URLs der Übersichtsseite (Reihenfolge erhalten, ohne Fragment/Query, ohne Zentrale)."""
    gefunden: dict[str, None] = {}
    zentrale = kette.zentrale.lower().removeprefix("www.")
    for a in BeautifulSoup(html or "", "lxml").find_all("a", href=True):
        url = urljoin(basis_url, a["href"].strip())
        if not url.lower().startswith(("http://", "https://")):
            continue
        n = _normal(url)
        if kette.link_muster is not None and not kette.link_muster.match(n):
            continue
        if kette.standortmuster != "pfad" and (normalize_domain(n) or "") == zentrale:
            continue  # Link auf die Zentrale selbst
        if kette.standortmuster == "pfad" and urlsplit(n).path in ("", "/"):
            continue
        gefunden.setdefault(n, None)
    return list(gefunden)


@dataclass
class KettenStatistik:
    standorte: int = 0
    neu: int = 0
    bekannt: int = 0
    fehler: list[str] = field(default_factory=list)


def _kandidat_fuer(storage: Storage, bundesland: str, url: str, pfad: bool) -> Kandidat | None:
    if not pfad:
        return storage.kandidat_nach_domain(normalize_domain(url) or "")
    return next((k for k in storage.kandidaten(bundesland) if k.domain is None and k.website
                 and _normal(k.website) == _normal(url)), None)


def standorte_anlegen(storage: Storage, bundesland: str, kette: Kette, urls: list[str], lauf_id: str) -> KettenStatistik:
    stat = KettenStatistik(standorte=len(urls))
    pfad = kette.standortmuster == "pfad"
    for url in urls:
        k = _kandidat_fuer(storage, bundesland, url, pfad)
        if k is None:
            domain = None if pfad else normalize_domain(url)
            kid = storage.add_kandidat(Kandidat(bundesland=bundesland, domain=domain,
                                                website=url if pfad else f"https://{domain}/", quelle=QUELLE))
            stat.neu += 1
        else:
            kid = k.id
            stat.bekannt += 1
        if not storage.hat_fund(bundesland, QUELLE, url):
            storage.add_fund(Fund(kandidat_id=kid, bundesland=bundesland, quelle=QUELLE, quell_id=url, lauf_id=lauf_id,
                                  website=url, roh_url=kette.uebersicht,
                                  snippet=f"Standort der Kette {kette.name} (Zentrale {kette.zentrale})"))
    # die Zentrale selbst als Kandidat (Rückfall-Quelle und Spalte „Zentrale")
    if storage.kandidat_nach_domain(kette.zentrale) is None and storage.kandidat_nach_domain(f"www.{kette.zentrale}") is None:
        storage.add_kandidat(Kandidat(bundesland=bundesland, domain=kette.zentrale, website=f"https://www.{kette.zentrale}/",
                                      quelle=QUELLE, name=kette.name))
        stat.neu += 1
    return stat


def uebersicht_laden(kette: Kette, client: httpx.Client, user_agent: str, cache=None,
                     melden: Callable[[str], None] | None = None) -> str | None:
    """Übersichtsseite (robots.txt beachtet; Antwort in api_cache unter „kette")."""
    url = kette.uebersicht
    if not url:
        return None
    if cache is not None and (gecacht := cache.cache_get(QUELLE, url)) is not None:
        return gecacht["html"]
    teile = urlsplit(url)
    try:
        r = client.get(f"{teile.scheme}://{teile.netloc}/robots.txt")
        if r.status_code == 200 and not Robots.parse(r.text).erlaubt(user_agent, url):
            if melden:
                melden(f"{kette.schluessel}: robots.txt verbietet {url}")
            return None
        resp = client.get(url)
    except httpx.HTTPError as e:
        if melden:
            melden(f"{kette.schluessel}: {url} nicht erreichbar ({type(e).__name__})")
        return None
    if resp.status_code != 200:
        if melden:
            melden(f"{kette.schluessel}: HTTP {resp.status_code} für {url}")
        return None
    if cache is not None:
        cache.cache_put(QUELLE, url, {"html": resp.text})
    return resp.text
