"""Stufe C `recherche telefonbuch -b SH`: Telefon (und ggf. Website) für Einträge des amtlichen Verzeichnisses ohne
Website aus Das Örtliche, Rückfall Gelbe Seiten (Vorgabe 05.11.2026: 695 „Person ohne Gesellschaft an dieser Adresse"
ohne Website in der SH-Lückenliste).

Je Eintrag eine Namenssuche „{Name} {Ort}" (Berufsbezeichnung entfernt), erste Ergebnisseite, gecacht, robots.txt,
Abstand wie beim amtlichen Verzeichnis (name_pause_s 30 + Zufall 0–5 s). Ein Treffer zählt nur, wenn der Nachname
(Sozietät: einer der Nachnamen, Gesellschaft: ein unterscheidendes Namenswort) im Eintrag steht und PLZ oder Straße
gleich sind. Übernommen werden Telefon (leeres Feld) und eine Website, die weder Blocklist noch Portal, Kette oder
schon vergeben ist; der Fund trägt die Quelle („Das Örtliche"/„Gelbe Seiten" in der Lückenliste, Spalte
„Telefon-Quelle"). E-Mail-Adressen stehen auf beiden Ergebnisseiten nicht. Jeder Eintrag einmal (api_cache
„telefonbuch"), ein erneuter Aufruf macht nur die offenen.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from .models import Fund, Kandidat, SchrittStatus
from .search.base import LimitErreicht, SearchError, SearchHit
from .search.dedupe import adresse_key, name_tokens
from .storage import Storage

MERKER = "telefonbuch"
QUELLEN = ("dasoertliche", "gelbeseiten")  # Reihenfolge: Rückfall danach
ANZEIGE = {"dasoertliche": "Das Örtliche", "gelbeseiten": "Gelbe Seiten"}
_BERUF = re.compile(r"^(?:Steuerberater(?:in)?|Steuerbevollmächtigte[rn]?|Wirtschaftsprüfer(?:in)?)\s+")


def _falten(text: str | None) -> str:
    return (text or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")


def suchname(name: str) -> str:
    """„Steuerberaterin Imke Thordsen" → „Imke Thordsen", „Steuerberater Lund & Ott" → „Lund Ott"."""
    return " ".join(_BERUF.sub("", name).replace("&", " ").replace(",", " ").split())


def namenswoerter(name: str) -> set[str]:
    """Person: Nachname; Sozietät („Steuerberater Lund, Ott & Rau"): jeder Nachname; Gesellschaft: unterscheidende
    Namenswörter (ab 4 Zeichen)."""
    if _BERUF.match(name):
        rest = _BERUF.sub("", name)
        teile = re.split(r"\s*(?:,|&)\s*", rest) if "&" in rest else [rest.split()[-1]] if rest.split() else []
        return {re.sub(r"[^a-z0-9]", "", _falten(t.split()[-1])) for t in teile if t.split()}
    return {t for t in name_tokens(name) if len(t) >= 4}


def passt(hit: SearchHit, k: Kandidat) -> bool:
    """Nachname im Eintrag und PLZ oder Straße gleich."""
    eintrag = set(re.findall(r"[a-z0-9]+", _falten(hit.name)))
    if not (namenswoerter(k.name or "") & eintrag):
        return False
    if hit.plz and k.plz and hit.plz == k.plz:
        return True
    a, b = adresse_key(hit.strasse, hit.plz), adresse_key(k.strasse, k.plz)
    return bool(a and a == b) or bool(
        hit.strasse and k.strasse and re.sub(r"[^a-z]", "", _falten(hit.strasse)) == re.sub(r"[^a-z]", "", _falten(k.strasse)))


@dataclass
class TelefonbuchStatistik:
    geprueft: int = 0
    telefon: int = 0
    website: int = 0
    ohne: int = 0  # kein passender Eintrag
    fehler: int = 0
    abbruch: str | None = None
    je_quelle: Counter = field(default_factory=Counter)


def offene(storage: Storage, bundesland: str) -> list[Kandidat]:
    """Kandidaten des amtlichen Verzeichnisses ohne Website, noch nicht im Telefonbuch gesucht."""
    from .stbv_lauf import QUELLE

    ids = {f.kandidat_id for f in storage.funde(bundesland, QUELLE)}
    return [k for k in storage.kandidaten(bundesland) if k.id in ids and not (k.domain or k.website) and k.name
            and storage.cache_get(MERKER, str(k.id)) is None]


def run_telefonbuch(storage: Storage, adapter: list, bundesland: str, blocklist: set[str],
                    fortschritt: Callable[[str], None] | None = None) -> TelefonbuchStatistik:
    """`adapter`: Telefonbuch-Adapter in Reihenfolge (Das Örtliche, dann Gelbe Seiten als Rückfall)."""
    from .extract.kanzlei import suchorte_je_stammdomain
    from .search.nachsuche import domain_abgelehnt
    from .search.pipeline import website_domain

    stat = TelefonbuchStatistik()
    suchorte = suchorte_je_stammdomain(storage, bundesland)
    offen = offene(storage, bundesland)
    for n, k in enumerate(offen, start=1):
        query = suchname(k.name)
        treffer, quelle, fehler = None, None, []
        for a in adapter:
            try:
                hits = a.suche_name(query, k.ort, k.plz)
            except LimitErreicht as e:
                stat.abbruch = str(e)
                return stat
            except SearchError as e:
                fehler.append(str(e))
                continue
            if treffer := next((h for h in hits if passt(h, k)), None):
                quelle = a.name
                break
        if treffer is None and fehler and len(fehler) == len(adapter):
            stat.fehler += 1
            if fortschritt:
                fortschritt(f"{n}/{len(offen)} {query} {k.ort or ''}: Fehler – {'; '.join(fehler)}")
            continue  # ohne Merker: ein erneuter Aufruf versucht es wieder
        stat.geprueft += 1
        ergebnis = "kein passender Eintrag (Nachname und PLZ/Straße)"
        if treffer is not None:
            stat.je_quelle[quelle] += 1
            felder: dict = {}
            if treffer.telefon and not k.telefon:
                felder["telefon"] = treffer.telefon
                stat.telefon += 1
            domain, _ = website_domain(treffer, blocklist)
            if domain and not domain_abgelehnt(storage, domain, blocklist, suchorte) and not storage.kandidat_nach_domain(domain):
                felder.update(domain=domain, website=treffer.website)
                stat.website += 1
            if felder:
                storage.ergaenze_kandidat(k.id, **felder)
            storage.add_fund(Fund(kandidat_id=k.id, bundesland=bundesland, quelle=quelle, quell_id=treffer.quell_id,
                                  suchbegriff=query, suchort=k.ort, name=treffer.name, strasse=treffer.strasse,
                                  plz=treffer.plz, ort=treffer.ort, telefon=treffer.telefon, website=treffer.website,
                                  snippet=treffer.snippet, roh_url=treffer.roh_url))
            if "domain" in felder:
                storage.set_crawl_status(k.id, SchrittStatus.OFFEN)
            storage.set_extract_status(k.id, SchrittStatus.OFFEN)
            ergebnis = (f"{ANZEIGE.get(quelle, quelle)}: {treffer.telefon or 'ohne Telefon'}"
                        + (f", Website {felder['domain']}" if "domain" in felder else ""))
        else:
            stat.ohne += 1
        storage.cache_put(MERKER, str(k.id), {"query": query, "quelle": quelle, "ergebnis": ergebnis})
        if fortschritt:
            fortschritt(f"{n}/{len(offen)} {query} {k.ort or ''}: {ergebnis}")
    return stat


def telefon_quelle(funde: list[Fund], telefon: str | None) -> str | None:
    """Quelle des Telefons für die Lückenliste: Detailseite des Verzeichnisses („amtlich"), sonst das Telefonbuch,
    dessen Fund diese Nummer trägt."""
    if not telefon:
        return None
    ziffern = re.sub(r"\D", "", telefon)[-6:]
    for f in funde:
        if f.quelle == "amtlich" and f.snippet and ziffern and ziffern in re.sub(r"\D", "", f.snippet):
            return "amtlich"
    for f in funde:
        if f.quelle in ANZEIGE and f.telefon and re.sub(r"\D", "", f.telefon).endswith(ziffern):
            return ANZEIGE[f.quelle]
    return None
