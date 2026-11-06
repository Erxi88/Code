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

import json
import re
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


def feste_standorte(kette: Kette) -> list[str]:
    """Standorte aus `standorte_fest` (Domain oder URL) als URLs, ohne die Zentrale, je Standort einmal."""
    urls: dict[str, None] = {}
    zentrale = kette.zentrale.lower().removeprefix("www.")
    for eintrag in kette.standorte_fest:
        url = eintrag if "://" in eintrag else f"https://{eintrag.strip().strip('/')}/"
        n = _normal(url)
        if kette.standortmuster != "pfad" and (normalize_domain(n) or "") == zentrale:
            continue
        urls.setdefault(n, None)
    return list(urls)


def standorte_aus_suchtreffern(storage: Storage, bundesland: str, kette: Kette) -> list[str]:
    """Subdomains der Ketten-Domain, die schon als Kandidat aus Suchtreffern oder Verzeichnissen da sind
    („kiel.kette.de") – ohne Zentrale und ohne jobs./karriere./mail. …"""
    zentrale = kette.zentrale.lower().removeprefix("www.")
    return [f"https://{k.domain}/" for k in storage.kandidaten(bundesland)
            if k.domain and k.domain != zentrale and k.domain.endswith("." + zentrale) and kette.enthaelt(k.domain)]


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
        land = bundesland
        k = _kandidat_fuer(storage, land, url, pfad)
        if k is None:
            domain = None if pfad else normalize_domain(url)
            kid = storage.add_kandidat(Kandidat(bundesland=land, domain=domain,
                                                website=url if pfad else f"https://{domain}/", quelle=QUELLE))
            stat.neu += 1
        else:
            kid = k.id
            stat.bekannt += 1
        if not storage.hat_fund(land, QUELLE, url):
            storage.add_fund(Fund(kandidat_id=kid, bundesland=land, quelle=QUELLE, quell_id=url, lauf_id=lauf_id,
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


# ---------------------------------------------------------------------- Standortdaten (06.11.2026)
# Großgesellschaften ohne Link-Liste: die Übersicht (bzw. eine JSON-Datei) nennt je Standort Name, Adresse, Telefon,
# teils E-Mail und Website. Jeder Standort wird ein Kandidat mit diesen Daten (Fund „kette", zählt wie Stufe B) – im
# Land seiner PLZ (LandNachOrt), damit jeder Standort bundesweit genau eine Zeile im Export seines Landes hat.


@dataclass
class StandortDaten:
    name: str
    strasse: str | None
    plz: str | None
    ort: str | None
    telefon: str | None = None
    email: str | None = None
    website: str | None = None

    @property
    def quell_id(self) -> str:
        return self.website or f"{self.name}|{self.plz}|{self.strasse}"


_ECOVIS = re.compile(r'\{"name":"((?:[^"\\]|\\.)*)","address":\{"street":"((?:[^"\\]|\\.)*)","postalCode":"([^"]*)",'
                     r'"town":"((?:[^"\\]|\\.)*)"[^}]*\},"website":"((?:[^"\\]|\\.)*)","email":"((?:[^"\\]|\\.)*)",'
                     r'"phone":"((?:[^"\\]|\\.)*)"\}')


def _js(text: str) -> str:
    try:
        return json.loads(f'"{text}"')
    except ValueError:
        return text


def _deutsch(d: StandortDaten) -> bool:
    return bool(d.plz and re.fullmatch(r"\d{5}", d.plz)) and (not d.telefon or d.telefon.startswith(("+49", "0")))


def ecovis_standorte(html: str) -> list[StandortDaten]:
    """ECOVIS: Standorte als JSON im Quelltext der Übersicht (de.ecovis.com/standorte/, auch Ausland) – nur deutsche."""
    daten = [StandortDaten(_js(n), _js(s) or None, p or None, _js(o) or None, _js(t) or None, _js(e) or None,
                           _js(w) or None) for n, s, p, o, w, e, t in _ECOVIS.findall(html or "")]
    return [d for d in daten if _deutsch(d)]


def ey_standorte(text: str) -> list[StandortDaten]:
    """EY: office-locations.json (Länder → Städte → Büros); nur Deutschland, ohne Innovationszentren („wavespace")."""
    try:
        laender = json.loads(text or "{}").get("countries") or []
    except ValueError:
        return []
    ergebnis = []
    for land in laender:
        if land.get("countryCode") != "DE":
            continue
        for stadt in land.get("cities") or []:
            for b in stadt.get("offices") or []:
                name = b.get("officeName") or b.get("globalOfficeName") or "EY"
                if "wavespace" in name.lower():
                    continue
                tel = (b.get("phoneInformationDirect") or b.get("phoneInformationLandline") or [None])[0]
                ort = b.get("cityName") or stadt.get("name")
                strasse = BeautifulSoup(b.get("officeAddress") or "", "lxml").get_text(" ", strip=True) or None
                ergebnis.append(StandortDaten(f"{name} {ort}" if name == "EY" else name, strasse,
                                              b.get("officePostalCode") or None, ort, tel, b.get("emailAddress") or None))
    return [d for d in ergebnis if _deutsch(d)]


def hlb_standorte(html: str) -> list[StandortDaten]:
    """HLB Deutschland: Karten der Mitgliedskanzleien (Name, „Address:", Straße, „PLZ Ort, Germany", Telefon, Website)."""
    z = BeautifulSoup(html or "", "lxml").get_text("\n", strip=True).split("\n")
    ergebnis = []
    for i, zeile in enumerate(z):
        if not (m := re.match(r"^(\d{5})\s+(.+?), Germany$", zeile)) or i < 3 or z[i - 2] != "Address:":
            continue
        rest = z[i + 1:i + 8]
        tel = next((x.lstrip(": ").strip() for j, x in enumerate(rest) if j and rest[j - 1] == "Telefon"), None)
        web = next((x for j, x in enumerate(rest) if j >= 2 and rest[j - 2] == "Website" and "." in x), None)
        ergebnis.append(StandortDaten(z[i - 3], z[i - 1], m.group(1), m.group(2), tel, None,
                                      f"https://{web.removeprefix('http://').removeprefix('https://').strip('/')}/"
                                      if web else None))
    return ergebnis


FORMATE = {"ecovis_json": ecovis_standorte, "ey_json": ey_standorte, "hlb_karten": hlb_standorte}


def daten_laden(kette: Kette, client: httpx.Client, user_agent: str, cache=None,
                melden: Callable[[str], None] | None = None) -> list[StandortDaten]:
    """Standortdaten nach `format` aus der Übersicht oder der JSON-Datei `daten_url` (robots.txt, Cache)."""
    if kette.format not in FORMATE:
        return []
    quelle = Kette(kette.schluessel, kette.name, kette.zentrale, kette.daten_url or kette.uebersicht)
    text = uebersicht_laden(quelle, client, user_agent, cache, melden)
    return FORMATE[kette.format](text or "")


@dataclass
class DatenStatistik(KettenStatistik):
    je_land: dict[str, int] = field(default_factory=dict)


def standortdaten_anlegen(storage: Storage, bundesland: str, kette: Kette, daten: list[StandortDaten], lauf_id: str,
                          land_von: Callable[[str | None, str | None], str | None] | None = None) -> DatenStatistik:
    """Je Standort ein Kandidat im Land seiner PLZ (sonst dem Lauf-Land) mit Name, Adresse, Telefon, Website; Fund
    „kette" mit denselben Daten (E-Mail im Snippet). Wiederholbar: bekannt über Website bzw. Name+PLZ+Straße."""
    from .search.dedupe import adresse_key

    stat = DatenStatistik(standorte=len(daten))
    zentrale = kette.zentrale.lower().removeprefix("www.")
    adressen: dict[str, dict[str, int]] = {}  # Land → Adressschlüssel → Kandidat (Verzeichnis-Eintrag am selben Ort)

    def an_adresse(land: str, d: StandortDaten) -> Kandidat | None:
        if land not in adressen:
            adressen[land] = {key: c.id for c in storage.kandidaten(land) if (key := adresse_key(c.strasse, c.plz))}
        kid = adressen[land].get(adresse_key(d.strasse, d.plz) or "")
        return storage.get_kandidat(kid) if kid is not None else None

    for d in daten:
        land = (land_von(d.plz, d.ort) if land_von else None) or bundesland
        stat.je_land[land] = stat.je_land.get(land, 0) + 1
        domain = normalize_domain(d.website) if d.website else None
        pfad = bool(domain) and (domain == zentrale or domain.endswith("." + zentrale)) and \
            urlsplit(d.website).path not in ("", "/")
        if domain and pfad:
            domain = None  # Standortseite unter der Domain der Zentrale (www.ecovis.com/kiel) – Domain gehört ihr
        k = (storage.kandidat_nach_domain(domain) if domain else
             _kandidat_fuer(storage, land, d.website, True) if d.website else
             next((c for c in storage.kandidaten(land) if c.id is not None and any(
                 f.quelle == QUELLE and f.quell_id == d.quell_id for f in storage.funde_kandidat(c.id))), None))
        k = k or an_adresse(land, d)  # dieselbe Adresse schon im Bestand (amtliches Verzeichnis) → kein Doppel
        if k is None:
            kid = storage.add_kandidat(Kandidat(bundesland=land, domain=domain, website=d.website, name=d.name,
                                                strasse=d.strasse, plz=d.plz, ort=d.ort, telefon=d.telefon,
                                                quelle=QUELLE))
            stat.neu += 1
        else:
            kid = k.id
            storage.ergaenze_kandidat(kid, name=d.name, strasse=d.strasse, plz=d.plz, ort=d.ort, telefon=d.telefon)
            stat.bekannt += 1
        if not storage.hat_fund(land, QUELLE, d.quell_id):
            storage.add_fund(Fund(kandidat_id=kid, bundesland=land, quelle=QUELLE, quell_id=d.quell_id, lauf_id=lauf_id,
                                  name=d.name, strasse=d.strasse, plz=d.plz, ort=d.ort, telefon=d.telefon,
                                  website=d.website, roh_url=kette.daten_url or kette.uebersicht,
                                  snippet="; ".join(x for x in (f"Standort der Kette {kette.name} (Zentrale {zentrale})",
                                                                f"E-Mail: {d.email}" if d.email else None) if x)))
    if storage.kandidat_nach_domain(zentrale) is None and storage.kandidat_nach_domain(f"www.{zentrale}") is None:
        storage.add_kandidat(Kandidat(bundesland=bundesland, domain=zentrale, website=f"https://www.{zentrale}/",
                                      quelle=QUELLE, name=kette.name))
        stat.neu += 1
    return stat


# ---------------------------------------------------------------------- Abgleich mit dem amtlichen Verzeichnis


KURZ_ZU_KETTE = {"PwC": "pwc", "KPMG": "kpmg", "BDO": "bdo", "Ebner Stolz": "ebnerstolz", "RSM": "ebnerstolz",
                 "Forvis Mazars": "forvismazars", "Rödl": "roedl", "WTS": "wts", "ECOVIS": "ecovis", "EY": "ey",
                 "HLB": "hlb"}
_TREUHAND_HANNOVER = re.compile(r"treuhand\s+hannover", re.I)
GLEICHE_ADRESSE, GLEICHE_PLZ, NICHT_IM_VERZEICHNIS, NUR_IM_VERZEICHNIS, ADRESSE_OFFEN = (
    "im Verzeichnis (gleiche Adresse)", "im Verzeichnis (gleiche PLZ, andere Adresse)", "nicht im Verzeichnis",
    "nur im Verzeichnis (keine Standortzeile)", "Adresse noch unbekannt – erst crawl/extract")


def _falten(text: str | None) -> str:
    t = (text or "").casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]", "", t)


def kette_im_namen(name: str | None) -> str | None:
    """Schlüssel der Kette, deren Kurzform der Name trägt („PricewaterhouseCoopers GmbH" → pwc)."""
    from .search.verzeichnisse import kurzform

    if name and _TREUHAND_HANNOVER.search(name):
        return "treuhand-hannover"
    return KURZ_ZU_KETTE.get(kurzform(name) or "")


@dataclass
class AbgleichZeile:
    kette: str
    standort: str  # Name bzw. Standortseite
    adresse: str
    ergebnis: str
    verzeichnis: str = ""  # Name und Adresse des Verzeichnis-Eintrags


def ketten_abgleich(storage: Storage, bundesland: str, ketten: list[Kette],
                    land_von: Callable[[str | None, str | None], str | None] | None = None) -> list[AbgleichZeile]:
    """Standortzeilen der Ketten im Land gegen die Einträge des amtlichen Verzeichnisses (06.11.2026): je Standort
    gleiche Adresse / gleiche PLZ / nicht im Verzeichnis; Verzeichnis-Einträge mit Kurzform einer Kette ohne
    Standortzeile → „nur im Verzeichnis"."""
    from .search.dedupe import adresse_key
    from .stbv_lauf import QUELLE as AMTLICH_QUELLE

    nach_name = {k.name: k.schluessel for k in ketten}
    amtlich_ids = {f.kandidat_id for f in storage.funde(bundesland, AMTLICH_QUELLE)}
    zentralen = {k.zentrale.lower().removeprefix("www.") for k in ketten}

    def adresse(c: Kandidat) -> tuple[str | None, str | None, str | None]:
        z = storage.kanzlei_fuer_kandidat(c.id)
        return ((z.strasse if z and z.strasse else c.strasse), (z.plz if z and z.plz else c.plz),
                (z.ort if z and z.ort else c.ort))

    verzeichnis = []
    for c in storage.kandidaten(bundesland):
        if c.id in amtlich_ids:
            s, p, o = adresse(c)
            verzeichnis.append((c, s, p, o, adresse_key(s, p), kette_im_namen(c.name)))
    zeilen, getroffen = [], set()
    for c in storage.kandidaten(bundesland):
        if c.domain in zentralen:
            continue
        fund = next((f for f in storage.funde_kandidat(c.id) if f.quelle == QUELLE and f.snippet), None)
        m = re.search(r"Standort der Kette (.+?) \(Zentrale", fund.snippet) if fund else None
        if not m or m.group(1) not in nach_name:
            continue
        kette = nach_name[m.group(1)]
        s, p, o = adresse(c)
        if p and land_von and (land_von(p, o) or bundesland) != bundesland:
            continue  # Standort eines anderen Landes
        text = f"{s or ''}, {p or ''} {o or ''}".strip(", ")
        name = c.name or c.website or c.domain or str(c.id)
        if not p:
            # Standortseite noch nicht gelesen: Verzeichnis-Einträge derselben Kette im Ort aus dem Pfad
            # („…/standorte/kiel.html") gelten als ihr Gegenstück, nicht als „nur im Verzeichnis"
            ort_pfad = _falten(urlsplit(c.website or "").path.rstrip("/").rsplit("/", 1)[-1])
            im_ort = [v for v in verzeichnis if v[5] == kette and v[3] and _falten(v[3]) and _falten(v[3]) in ort_pfad]
            for v in im_ort:
                getroffen.add(v[0].id)
            zeilen.append(AbgleichZeile(kette, name, text, ADRESSE_OFFEN, "; ".join(
                f"im Ort: {v[0].name} ({v[1] or ''}, {v[2] or ''} {v[3] or ''})" for v in im_ort)))
            continue
        key = adresse_key(s, p)
        gleich = [v for v in verzeichnis if v[0].id == c.id or (key and v[4] == key)]
        plz = [v for v in verzeichnis if v[2] == p and v[5] == kette]
        treffer = gleich or plz
        for v in treffer:
            getroffen.add(v[0].id)
        zeilen.append(AbgleichZeile(kette, name, text, GLEICHE_ADRESSE if gleich else GLEICHE_PLZ if plz
                                    else NICHT_IM_VERZEICHNIS,
                                    "; ".join(f"{v[0].name} ({v[1] or ''}, {v[2] or ''} {v[3] or ''})" for v in treffer)))
    for c, s, p, o, _, kette in verzeichnis:
        if kette and c.id not in getroffen:
            zeilen.append(AbgleichZeile(kette, "", "", NUR_IM_VERZEICHNIS, f"{c.name} ({s or ''}, {p or ''} {o or ''})"))
    return sorted(zeilen, key=lambda z: (z.kette, z.ergebnis, z.standort))
