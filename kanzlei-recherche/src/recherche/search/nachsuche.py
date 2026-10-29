"""Website-Nachsuche für Verzeichnis-Einträge ohne Website (Stufe B → Stufe A, eine Brave-Seite je Eintrag).

Das amtliche Verzeichnis (und die Branchenverzeichnisse) liefern Name und Anschrift, oft aber keine
Website. Für jeden neuen Kandidaten ohne Website: Websuche „{Name} {Ort}", eine Seite. Der erste Treffer,
dessen Domain nicht auf der Blocklist steht und zum Namen passt (gemeinsames unterscheidendes Wort in
Domain oder Titel), wird Website des Kandidaten – er läuft dann durch Crawl und Extraktion. Ohne passenden
Treffer bleibt der Eintrag mit der amtlichen Anschrift („Website vorhanden" = nein); das Telefon kommt
über die Branchenverzeichnisse (Abgleich ergänzt leere Felder). Jeder Kandidat wird einmal nachgesucht
(gemerkt in api_cache), ein erneuter Aufruf macht nur die offenen.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..models import Fund, SchrittStatus
from ..storage import Storage
from .base import LimitErreicht, QueryAdapter, SearchError, SearchHit
from .dedupe import name_tokens
from .pipeline import website_domain

MERKER = "website_nachsuche"


@dataclass
class NachsucheStatistik:
    geprueft: int = 0
    gefunden: int = 0
    ohne_website: int = 0
    schon_vergeben: int = 0  # Domain gehört schon einem anderen Kandidaten (Dublette)
    fehler: int = 0
    abbruch: str | None = None


def passender_treffer(name: str, ort: str | None, hits: list[SearchHit], blocklist: set[str]) -> tuple[SearchHit, str] | None:
    """Erster Treffer mit Kanzlei-Domain, die zum Namen passt."""
    soll = name_tokens(name, ort)
    if not soll:
        return None
    for h in hits:
        domain, _ = website_domain(h, blocklist)
        if not domain:
            continue
        domain_woerter = set(re.split(r"[^a-z0-9]+", domain.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")))
        if soll & (domain_woerter | name_tokens(h.name, ort)) or any(t in domain.replace("-", "") for t in soll if len(t) >= 5):
            return h, domain
    return None


def website_nachsuche(storage: Storage, adapter: QueryAdapter, bundesland: str, quelle: str, blocklist: set[str],
                      fortschritt: Callable[[str], None] | None = None) -> NachsucheStatistik:
    stat = NachsucheStatistik()
    offen = [k for k in storage.kandidaten(bundesland) if k.quelle == quelle and not (k.domain or k.website)
             and k.name and storage.cache_get(MERKER, str(k.id)) is None]
    for k in offen:
        query = f"{k.name} {k.ort or ''}".strip()
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
        treffer = passender_treffer(k.name, k.ort, hits, blocklist)
        ergebnis = None
        if treffer:
            h, domain = treffer
            anderer = storage.kandidat_nach_domain(domain)
            if anderer and anderer.id != k.id:
                stat.schon_vergeben += 1
                ergebnis = f"Domain {domain} gehört schon zu Kandidat {anderer.id}"
            else:
                storage.ergaenze_kandidat(k.id, domain=domain, website=h.website)
                storage.add_fund(Fund(kandidat_id=k.id, bundesland=bundesland, quelle=adapter.name, quell_id=h.quell_id,
                                      suchbegriff=query, suchort=k.ort, name=h.name, website=h.website,
                                      snippet=h.snippet, roh_url=h.roh_url))
                storage.set_crawl_status(k.id, SchrittStatus.OFFEN)
                storage.set_extract_status(k.id, SchrittStatus.OFFEN)
                stat.gefunden += 1
                ergebnis = f"Website {domain}"
        if ergebnis is None:
            stat.ohne_website += 1
            ergebnis = "keine passende Website – bleibt mit amtlicher Anschrift"
        storage.cache_put(MERKER, str(k.id), {"query": query, "ergebnis": ergebnis})
        if fortschritt:
            fortschritt(f"{query}: {ergebnis}")
    return stat
