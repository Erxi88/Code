"""Website-Nachsuche für Verzeichnis-Einträge ohne Website (Stufe B → Stufe A, eine Brave-Seite je Eintrag).

Das amtliche Verzeichnis (und die Branchenverzeichnisse) liefern Name und Anschrift, oft aber keine
Website. Für jeden neuen Kandidaten ohne Website: Websuche „{Name} {Ort}", eine Seite. Der erste Treffer,
dessen Domain nicht auf der Blocklist steht und zum Namen passt (gemeinsames unterscheidendes Wort in
Domain oder Titel), wird Website des Kandidaten – er läuft dann durch Crawl und Extraktion. Ohne passenden
Treffer bleibt der Eintrag mit der amtlichen Anschrift („Website vorhanden" = nein); das Telefon kommt
über die Branchenverzeichnisse (Abgleich ergänzt leere Felder). Jeder Kandidat wird einmal nachgesucht
(gemerkt in api_cache), ein erneuter Aufruf macht nur die offenen.

Regeln nach dem SH-Lauf 05.11.2026 (596 Zusammenführungen, viele falsch: online-handelsregister.de, steuerfinder.com,
haendlerschutz.com, kanzlei.etl.de, sylt-web.de):
1. Treffer werden wie Suchtreffer gefiltert – Blocklist, Portal-Erkennung (Domain-Muster, Stammdomain in vielen
   Suchorten) und Domains, deren Kandidat „keine Kanzlei"/ungültig ist oder als Portal nicht gelesen wurde; dann zählt
   der nächste Treffer.
2. Gehört die Domain schon einem Kandidaten, wird nur zusammengeführt, wenn dessen Zeile eine Kanzlei ist und Name
   oder Adresse passen (Nachname bzw. unterscheidendes Namenswort in Domain oder Kanzleiname, oder gleiche
   PLZ+Straße); sonst wird der Treffer verworfen („keine passende Website").
3. Domains von Ketten und Großgesellschaften (ketten.yaml, z. B. kanzlei.etl.de) werden nie übernommen – Standorte
   nur über `recherche ketten`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..models import Fund, Kandidat, SchrittStatus, Status
from ..storage import Storage
from .base import LimitErreicht, QueryAdapter, SearchError, SearchHit
from .dedupe import adresse_key, name_tokens
from .pipeline import website_domain
from .verzeichnisse import FIRMA_LAUT_VERZEICHNIS

MERKER = "website_nachsuche"


@dataclass
class NachsucheStatistik:
    geprueft: int = 0
    gefunden: int = 0
    ohne_website: int = 0
    schon_vergeben: int = 0  # Domain gehört schon einem anderen Kandidaten (Dublette)
    verworfen: int = 0  # Domain gehört einem Kandidaten ohne Kanzlei-Status bzw. ohne Namens-/Adressbezug (Regel 2)
    gefiltert: int = 0  # Treffer übersprungen: Portal, keine Kanzlei, Kette (Regeln 1 und 3)
    fehler: int = 0
    abbruch: str | None = None


def _falten(text: str) -> str:
    return text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")


def passender_treffer(name: str, ort: str | None, hits: list[SearchHit], blocklist: set[str],
                      zulassen: Callable[[str], bool] | None = None) -> tuple[SearchHit, str] | None:
    """Erster Treffer mit Kanzlei-Domain, die zum Namen passt; `zulassen(domain)` False → nächster Treffer."""
    soll = name_tokens(name, ort)
    if not soll:
        return None
    for h in hits:
        domain, _ = website_domain(h, blocklist)
        if not domain or (zulassen and not zulassen(domain)):
            continue
        domain_woerter = set(re.split(r"[^a-z0-9]+", domain.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")))
        if soll & (domain_woerter | name_tokens(h.name, ort)) or any(t in domain.replace("-", "") for t in soll if len(t) >= 5):
            return h, domain
    return None


def ketten_domain(domain: str) -> str | None:
    """Name der Kette/Großgesellschaft, zu der die Domain gehört (ketten.yaml: Zentrale, Subdomains, feste Standorte,
    Domains der Großgesellschaften) – sonst None."""
    from ..extract.kanzlei import stammdomain
    from ..ketten import config

    cfg, d = config(), domain.lower().removeprefix("www.")
    for c in cfg.ketten:
        if c.enthaelt(d) or stammdomain(d) == stammdomain(c.zentrale.lower().removeprefix("www.")):
            return c.name
    return next((g.name for g in cfg.grossgesellschaften if any(d == x or d.endswith("." + x) for x in g.domains)), None)


def domain_abgelehnt(storage: Storage, domain: str, blocklist: set[str], suchorte: dict[str, int]) -> str | None:
    """Regeln 1 und 3: Grund, warum die Domain nie Website eines Nachsuche-Eintrags wird – sonst None."""
    from ..extract.kanzlei import _PORTAL_DOMAIN, PORTAL_AB_ORTEN, stammdomain
    from .pipeline import ist_blockiert

    d = domain.lower().removeprefix("www.")
    if ist_blockiert(d, blocklist):
        return "Blocklist"
    if _PORTAL_DOMAIN.search(d) or (d.split(".")[0] in ("jobs", "karriere") and d.count(".") >= 2):
        return "Portal"
    stamm = stammdomain(d)
    if suchorte.get(stamm, 0) >= PORTAL_AB_ORTEN:
        return f"Portal (in {suchorte[stamm]} Suchorten)"
    if kette := ketten_domain(d):
        return f"Kette/Großgesellschaft {kette} – Standorte nur über ketten.yaml"
    for kid, fehler in storage.conn.execute(
            "SELECT id, fehler FROM kandidaten WHERE domain = ? OR domain = ? OR domain LIKE ?",
            (d, stamm, f"%.{stamm}")):
        if fehler and "Portal" in fehler:
            return "Portal (beim Lesen erkannt)"
        z = storage.kanzlei_fuer_kandidat(kid)
        if z is not None and z.status in (Status.KEINE_KANZLEI, Status.UNGUELTIG):
            return f"Kandidat {kid} ist „{z.status.value}“"
    return None


def _namenswoerter(k: Kandidat, firma: str | None, ort: str | None) -> set[str]:
    """Wörter, die in Domain oder Kanzleiname vorkommen müssen: bei Personen der Nachname, sonst die unterscheidenden
    Namenswörter (dazu die der Firmenzeile)."""
    person = bool(re.match(r"(?:Steuerberater(?:in)?|Steuerbevollmächtigte[rn]?|Wirtschaftsprüfer(?:in)?)\s",
                           k.name or ""))
    woerter = ({_falten((k.name or "").split()[-1])} if person and (k.name or "").split()
               else set(name_tokens(k.name, ort)))
    if firma:
        woerter |= set(name_tokens(firma, ort))
    return {re.sub(r"[^a-z0-9]", "", w) for w in woerter if len(w) >= 3}


def _zusammenfuehren_erlaubt(storage: Storage, k: Kandidat, anderer: Kandidat, domain: str, firma: str | None) -> str | None:
    """Regel 2: None, wenn zusammengeführt werden darf; sonst der Grund für das Verwerfen."""
    z = storage.kanzlei_fuer_kandidat(anderer.id)
    if z is None or z.status in (Status.KEINE_KANZLEI, Status.UNGUELTIG, Status.AUSSERHALB):
        return f"Kandidat {anderer.id} ist {'nicht extrahiert' if z is None else f'„{z.status.value}“'}"
    woerter = _namenswoerter(k, firma, k.ort)
    kompakt = re.sub(r"[^a-z0-9]", "", _falten(domain))
    kanzlei = {re.sub(r"[^a-z0-9]", "", w) for w in name_tokens(z.kanzleiname, k.ort)}
    if any(w in kompakt for w in woerter if len(w) >= 4) or woerter & kanzlei:
        return None
    eigene = adresse_key(k.strasse, k.plz)
    if eigene and eigene in {adresse_key(z.strasse, z.plz), adresse_key(anderer.strasse, anderer.plz)}:
        return None
    return f"Kandidat {anderer.id} („{z.kanzleiname or anderer.name}“) ohne Namens- oder Adressbezug"


def website_nachsuche(storage: Storage, adapter: QueryAdapter, bundesland: str, quelle: str, blocklist: set[str],
                      fortschritt: Callable[[str], None] | None = None) -> NachsucheStatistik:
    from ..extract.kanzlei import suchorte_je_stammdomain

    stat = NachsucheStatistik()
    suchorte = suchorte_je_stammdomain(storage, bundesland)
    offen = [k for k in storage.kandidaten(bundesland) if k.quelle == quelle and not (k.domain or k.website)
             and k.name and storage.cache_get(MERKER, str(k.id)) is None]
    for k in offen:
        # Person ohne Gesellschaft an ihrer Adresse: mit der Firmenzeile aus dem Verzeichnis suchen („Nordlicht
        # Treuhand … Kiel") – die Website der Kanzlei, in der die Person arbeitet
        firma = next((m.group(1).strip() for f in storage.funde_kandidat(k.id) if f.snippet
                      and (m := re.search(rf"{re.escape(FIRMA_LAUT_VERZEICHNIS)}(.+?)(?:;|$)", f.snippet))), None)
        suchname = firma or k.name
        query = f"{suchname} {k.ort or ''}".strip()
        try:
            hits = adapter.search(query, adapter.seitengroesse)
        except LimitErreicht as e:
            stat.abbruch = str(e)
            break
        except SearchError as e:
            stat.fehler += 1
            if fortschritt:
                fortschritt(f"{query}: Fehler – {e}")
            continue
        stat.geprueft += 1
        abgelehnt: dict[str, str] = {}

        def zulassen(domain: str, abgelehnt: dict[str, str] = abgelehnt) -> bool:
            if domain not in abgelehnt:
                abgelehnt[domain] = domain_abgelehnt(storage, domain, blocklist, suchorte) or ""
            return not abgelehnt[domain]

        treffer = passender_treffer(suchname, k.ort, hits, blocklist, zulassen) or (
            passender_treffer(k.name, k.ort, hits, blocklist, zulassen) if firma else None)
        stat.gefiltert += sum(1 for g in abgelehnt.values() if g)
        ergebnis = None
        if treffer:
            h, domain = treffer
            anderer = storage.kandidat_nach_domain(domain)
            if anderer and anderer.id != k.id and (grund := _zusammenfuehren_erlaubt(storage, k, anderer, domain, firma)):
                stat.verworfen += 1
                treffer = None
                ergebnis = f"keine passende Website – {domain} verworfen: {grund}"
            elif anderer and anderer.id != k.id:
                # gleiche Website → derselbe Kandidat: die Person geht in ihm auf (Funde wandern mit); ergänzte Felder
                # merken, damit eine Neu-Gruppierung sie zurücksetzen kann
                from ..stbv_lauf import ZUSAMMEN_MERKER

                ergaenzt = [f for f in ("strasse", "plz", "ort", "telefon") if getattr(k, f) and not getattr(anderer, f)]
                alt = (storage.cache_get(ZUSAMMEN_MERKER, str(anderer.id)) or {}).get("felder") or []
                storage.cache_put(ZUSAMMEN_MERKER, str(anderer.id), {"felder": sorted({*alt, *ergaenzt})})
                storage.kandidat_zusammenfuehren(k.id, anderer.id)
                stat.schon_vergeben += 1
                ergebnis = f"Domain {domain} gehört schon zu Kandidat {anderer.id} – zusammengeführt"
                if fortschritt:
                    fortschritt(f"{query}: {ergebnis}")
                continue  # Kandidat k gibt es nicht mehr (kein Merker nötig)
            else:
                storage.ergaenze_kandidat(k.id, domain=domain, website=h.website)
                storage.add_fund(Fund(kandidat_id=k.id, bundesland=bundesland, quelle=adapter.name, quell_id=h.quell_id,
                                      suchbegriff=query, suchort=k.ort, name=h.name, website=h.website,
                                      snippet=h.snippet, roh_url=h.roh_url))
                storage.set_crawl_status(k.id, SchrittStatus.OFFEN)
                storage.set_extract_status(k.id, SchrittStatus.OFFEN)
                stat.gefunden += 1
                ergebnis = f"Website {domain}"
        if ergebnis is None or not treffer:
            stat.ohne_website += 1
            ergebnis = ergebnis or "keine passende Website – bleibt mit amtlicher Anschrift"
            gefiltert = [f"{d} ({g})" for d, g in abgelehnt.items() if g]
            if gefiltert:
                ergebnis += f"; übersprungen: {', '.join(gefiltert)}"
        storage.cache_put(MERKER, str(k.id), {"query": query, "ergebnis": ergebnis})
        if fortschritt:
            fortschritt(f"{query}: {ergebnis}")
    return stat
