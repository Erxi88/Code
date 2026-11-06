"""Stufe C `recherche telefonbuch -b SH`: Telefon (und ggf. Website) aus Das Örtliche, Rückfall Gelbe Seiten.

Umbau 05.11.2026 (Lauf brach bei 1/569 ab: Namenssuche bei Das Örtliche → HTTP 410, Gelbe Seiten /suche per
robots.txt gesperrt): **Ortssuche statt Namenssuche** – je Ort mit offenen Einträgen eine Abfrage „Steuerberater" im
Ort (Das Örtliche, Controller-URL, alle Seiten à 25 über „zur nächsten Seite"; Gelbe Seiten nur über die erlaubte
Stadtseite /branchen/steuerberater/<stadt>, wenn die Stadt in der Sitemap der Stadtseiten steht, erste 50). Offen
sind (a) Einträge des amtlichen Verzeichnisses ohne Website und (b) Kanzleien mit Website ohne Telefon.

Zuordnung je offenem Eintrag: (b) zuerst über die gleiche Domain; sonst Nachname (Sozietät: einer, Gesellschaft:
unterscheidendes Namenswort) im Eintrag **und** gleiche PLZ oder Straße. Passen mehrere Einträge mit verschiedenen
Telefonnummern → „mehrdeutig" (nichts übernommen), keiner → „kein Treffer"; das Protokoll (CSV) nennt je Eintrag
eindeutig / mehrdeutig / kein Treffer mit Quelle und Treffer. Übernommen werden Telefon (leeres Feld) und – nur bei (a)
– die Website aus dem Eintrag (Skript-Array der Trefferliste), wenn sie weder Blocklist, Portal, Kette noch vergeben
ist, zum Namen passt (Namenswort oder Steuer-/Kanzleiwort in der Domain) und erreichbar ist. Fund mit Quelle
`dasoertliche`/`gelbeseiten` (Telefon, Website, E-Mail im Snippet); ohne Website wird die E-Mail des Eintrags
Kanzlei-E-Mail (merge.py); Lückenliste Spalten „E-Mail" und „Kontakt-Quelle" (Telefon und E-Mail). Seiten gecacht,
Abstand wie beim amtlichen Verzeichnis, jeder Eintrag einmal (api_cache „telefonbuch")."""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .models import Fund, Kandidat, SchrittStatus
from .search.base import LimitErreicht, SearchError, SearchHit
from .search.dedupe import adresse_key, name_tokens
from .search.pipeline import Ort, normalize_domain
from .storage import Storage

MERKER = "telefonbuch"
QUELLEN = ("dasoertliche", "gelbeseiten")  # Reihenfolge: Rückfall danach
ANZEIGE = {"dasoertliche": "Das Örtliche", "gelbeseiten": "Gelbe Seiten"}
EINDEUTIG, MEHRDEUTIG, KEIN_TREFFER = "eindeutig", "mehrdeutig", "kein Treffer"
_BERUF = re.compile(r"^(?:Steuerberater(?:in)?|Steuerbevollmächtigte[rn]?|Wirtschaftsprüfer(?:in)?)\s+")
_KANZLEI_DOMAIN = re.compile(r"steuer|stb|berat|kanzlei|treuhand|revision|wp|tax|audit")


def _falten(text: str | None) -> str:
    return (text or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")


def namenswoerter(name: str) -> set[str]:
    """Person: Nachname; Sozietät („Steuerberater Lund, Ott & Rau"): jeder Nachname; Gesellschaft: unterscheidende
    Namenswörter (ab 4 Zeichen)."""
    if _BERUF.match(name):
        rest = _BERUF.sub("", name)
        teile = re.split(r"\s*(?:,|&)\s*", rest) if "&" in rest else [rest.split()[-1]] if rest.split() else []
        return {re.sub(r"[^a-z0-9]", "", _falten(t.split()[-1])) for t in teile if t.split()}
    return {re.sub(r"[^a-z0-9]", "", t) for t in name_tokens(name) if len(t) >= 4}


def _strasse(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", re.sub(r"(strasse|str\.?)(?=\W|$)", "str", _falten(s)))


def passt(hit: SearchHit, name: str, strasse: str | None, plz: str | None) -> bool:
    """Nachname im Eintrag und PLZ oder Straße gleich."""
    eintrag = set(re.findall(r"[a-z0-9]+", _falten(hit.name)))
    if not (namenswoerter(name or "") & eintrag):
        return False
    if hit.plz and plz and hit.plz == plz:
        return True
    a, b = adresse_key(hit.strasse, hit.plz), adresse_key(strasse, plz)
    return bool(a and a == b) or bool(hit.strasse and strasse and _strasse(hit.strasse) == _strasse(strasse))


def _telefon(h: SearchHit) -> str:
    return re.sub(r"\D", "", h.telefon or "")


@dataclass
class Offen:
    kandidat: Kandidat
    art: str  # "verzeichnis" (amtlicher Eintrag ohne Website) | "telefon" (Kanzlei mit Website ohne Telefon)
    name: str
    strasse: str | None
    plz: str | None
    ort: str


@dataclass
class Zuordnung:
    offen: Offen
    ergebnis: str  # eindeutig / mehrdeutig / kein Treffer
    quelle: str | None = None
    treffer: SearchHit | None = None
    kandidaten: int = 0  # passende Einträge
    uebernommen: str = ""  # „Telefon, Website" …
    grund: str = ""


@dataclass
class TelefonbuchStatistik:
    orte: int = 0
    abfragen: int = 0  # Ergebnisseiten (ohne Cache)
    eindeutig: int = 0
    mehrdeutig: int = 0
    kein_treffer: int = 0
    telefon: int = 0
    website: int = 0
    fehler: list[str] = field(default_factory=list)
    abbruch: str | None = None
    je_quelle: Counter = field(default_factory=Counter)
    abfrageart: Counter = field(default_factory=Counter)  # „Das Örtliche: Ort" / „Das Örtliche: je PLZ" / Stadtseite
    protokoll: list[Zuordnung] = field(default_factory=list)


def offene(storage: Storage, bundesland: str) -> list[Offen]:
    """(a) amtliche Einträge ohne Website, (b) Kanzleien mit Website ohne Telefon – noch nicht im Telefonbuch gesucht."""
    from .stbv_lauf import QUELLE

    amtlich = {f.kandidat_id for f in storage.funde(bundesland, QUELLE)}
    ergebnis = []
    for k in storage.kandidaten(bundesland):
        if storage.cache_get(MERKER, str(k.id)) is not None:
            continue
        z = storage.kanzlei_fuer_kandidat(k.id) if k.id is not None else None
        ort = (z.ort if z and z.ort else None) or k.ort
        if not ort:
            continue
        if k.id in amtlich and not (k.domain or k.website) and k.name:
            ergebnis.append(Offen(k, "verzeichnis", k.name, k.strasse, k.plz, ort))
        elif k.domain and z is not None and not z.telefon and not k.telefon:
            ergebnis.append(Offen(k, "telefon", z.kanzleiname or k.name or k.domain, z.strasse or k.strasse,
                                  z.plz or k.plz, ort))
    return ergebnis


def zuordnen(o: Offen, hits: list[SearchHit]) -> tuple[str, SearchHit | None, int]:
    """(Ergebnis, Treffer, Zahl passender Einträge) – Domain vor Name + PLZ/Straße; gleiche Nummer zählt einmal."""
    if o.art == "telefon" and o.kandidat.domain:
        gleich = [h for h in hits if h.website and normalize_domain(h.website) == o.kandidat.domain and h.telefon]
        if gleich:
            return EINDEUTIG, gleich[0], len(gleich)
    passend = [h for h in hits if passt(h, o.name, o.strasse, o.plz)]
    if not passend:
        return KEIN_TREFFER, None, 0
    nummern = {_telefon(h) for h in passend if _telefon(h)}
    if len(nummern) > 1:
        return MEHRDEUTIG, None, len(passend)
    # eine Nummer: den Eintrag mit Website bevorzugen (Rau Sönke / Sönke Rau – zwei Einträge, eine Kanzlei)
    return EINDEUTIG, next((h for h in passend if h.website), passend[0]), len(passend)


def website_passt(domain: str, o: Offen) -> bool:
    """Namenswort oder Steuer-/Kanzleiwort in der Domain („muenchen-klinik.de" für Dr. Wendeborn passt nicht)."""
    kompakt = re.sub(r"[^a-z0-9]", "", _falten(domain.rsplit(".", 1)[0]))
    return bool(_KANZLEI_DOMAIN.search(kompakt)) or any(w in kompakt for w in namenswoerter(o.name) if len(w) >= 4)


def run_telefonbuch(storage: Storage, adapter: list, bundesland: str, blocklist: set[str],
                    fortschritt: Callable[[str], None] | None = None,
                    erreichbar: Callable[[str], bool] | None = None) -> TelefonbuchStatistik:
    """`adapter`: Telefonbuch-Adapter in Reihenfolge (Das Örtliche, dann Gelbe Seiten als Rückfall). `erreichbar(url)`
    prüft eine übernommene Website (Standard: ohne Prüfung)."""
    from .extract.kanzlei import suchorte_je_stammdomain
    from .search.nachsuche import domain_abgelehnt
    from .search.pipeline import website_domain

    stat = TelefonbuchStatistik()
    suchorte = suchorte_je_stammdomain(storage, bundesland)
    je_ort: dict[str, list[Offen]] = defaultdict(list)
    for o in offene(storage, bundesland):
        je_ort[o.ort].append(o)
    stat.orte = len(je_ort)
    for n, (ort, liste) in enumerate(sorted(je_ort.items()), start=1):
        offen = list(liste)
        for a in adapter:
            if not offen:
                break
            if hasattr(a, "stadt_bekannt") and not a.stadt_bekannt(ort):
                continue  # Gelbe Seiten: keine Stadtseite laut Sitemap → weglassen
            vorher = a.anfragen
            try:
                if hasattr(a, "suche_telefonbuch"):  # Das Örtliche: Großstädte und Ortsauswahl je PLZ
                    hits, art = a.suche_telefonbuch(ort, sorted({o.plz for o in offen if o.plz}))
                else:
                    hits, art = a.search_ort(Ort(ort, offen[0].plz or "")), "Stadtseite"
                stat.abfrageart[f"{ANZEIGE.get(a.name, a.name)}: {art.split(' (')[0]}"] += 1
                if fortschritt and art.startswith("je PLZ"):
                    fortschritt(f"Ort {n}/{stat.orte} {ort}: {ANZEIGE.get(a.name, a.name)} {art}")
            except LimitErreicht as e:
                stat.abbruch = str(e)
                return stat
            except SearchError as e:
                stat.fehler.append(f"{ort} ({ANZEIGE.get(a.name, a.name)}): {e}")
                if fortschritt:
                    fortschritt(f"Ort {n}/{stat.orte} {ort}: {ANZEIGE.get(a.name, a.name)} Fehler – {e}")
                continue
            stat.abfragen += a.anfragen - vorher
            rest = []
            for o in offen:
                ergebnis, treffer, zahl = zuordnen(o, hits)
                if ergebnis != EINDEUTIG and a is not adapter[-1]:
                    rest.append(o)  # nächste Quelle versuchen; protokolliert wird das letzte Ergebnis
                    o._letztes = (ergebnis, a.name, zahl)  # type: ignore[attr-defined]
                    continue
                _uebernehmen(storage, stat, o, ergebnis, treffer, zahl, a.name, bundesland, blocklist, suchorte,
                             website_domain, domain_abgelehnt, erreichbar)
            offen = rest
        for o in offen:  # nach allen Quellen ohne eindeutigen Treffer
            ergebnis, quelle, zahl = getattr(o, "_letztes", (KEIN_TREFFER, None, 0))
            _uebernehmen(storage, stat, o, ergebnis, None, zahl, quelle, bundesland, blocklist, suchorte,
                         website_domain, domain_abgelehnt, erreichbar)
        if fortschritt:
            z = Counter(p.ergebnis for p in stat.protokoll if p.offen.ort == ort)
            fortschritt(f"Ort {n}/{stat.orte} {ort}: {len(liste)} offen – {z[EINDEUTIG]} eindeutig, "
                        f"{z[MEHRDEUTIG]} mehrdeutig, {z[KEIN_TREFFER]} kein Treffer")
    return stat


def _uebernehmen(storage, stat, o: Offen, ergebnis, treffer, zahl, quelle, bundesland, blocklist, suchorte,
                 website_domain, domain_abgelehnt, erreichbar) -> None:
    z = Zuordnung(o, ergebnis, quelle, treffer, zahl)
    k = o.kandidat
    if ergebnis == EINDEUTIG and treffer is not None:
        stat.eindeutig += 1
        stat.je_quelle[quelle] += 1
        felder, teile = {}, []
        if treffer.telefon and not k.telefon:
            felder["telefon"] = treffer.telefon
            teile.append("Telefon")
            stat.telefon += 1
        domain, _ = website_domain(treffer, blocklist)
        if o.art == "verzeichnis" and domain:
            if grund := domain_abgelehnt(storage, domain, blocklist, suchorte):
                z.grund = f"Website {domain} nicht übernommen: {grund}"
            elif storage.kandidat_nach_domain(domain):
                z.grund = f"Website {domain} gehört schon einem Kandidaten"
            elif not website_passt(domain, o):
                z.grund = f"Website {domain} ohne Namensbezug – nicht übernommen"
            elif erreichbar is not None and not erreichbar(treffer.website):
                z.grund = f"Website {domain} nicht erreichbar – nicht übernommen"
            else:
                felder.update(domain=domain, website=treffer.website)
                teile.append("Website")
                stat.website += 1
        if felder:
            storage.ergaenze_kandidat(k.id, **felder)
        snippet = "; ".join(x for x in (f"Telefonbuch {ANZEIGE.get(quelle, quelle)}",
                                        f"E-Mail: {treffer.email}" if treffer.email else None,
                                        f"Website laut Eintrag: {treffer.website}" if treffer.website else None) if x)
        storage.add_fund(Fund(kandidat_id=k.id, bundesland=bundesland, quelle=quelle, quell_id=treffer.quell_id,
                              suchbegriff=f"Steuerberater {o.ort}", suchort=o.ort, name=treffer.name,
                              strasse=treffer.strasse, plz=treffer.plz, ort=treffer.ort, telefon=treffer.telefon,
                              website=treffer.website if "Website" in teile else None, snippet=snippet,
                              roh_url=treffer.roh_url))
        if "Website" in teile:
            storage.set_crawl_status(k.id, SchrittStatus.OFFEN)
        storage.set_extract_status(k.id, SchrittStatus.OFFEN)
        z.uebernommen = ", ".join(teile)
    elif ergebnis == MEHRDEUTIG:
        stat.mehrdeutig += 1
        z.grund = f"{zahl} passende Einträge mit verschiedenen Nummern – nichts übernommen"
    else:
        stat.kein_treffer += 1
    stat.protokoll.append(z)
    storage.cache_put(MERKER, str(k.id), {"ergebnis": ergebnis, "quelle": quelle, "uebernommen": z.uebernommen,
                                          "grund": z.grund})


PROTOKOLL_SPALTEN = ["Kandidat", "Art", "Name", "Straße", "PLZ", "Ort", "Zuordnung", "Quelle", "passende Einträge",
                     "Treffer", "Treffer-Adresse", "Telefon", "Website laut Eintrag", "übernommen", "Hinweis"]


def protokoll_csv(stat: TelefonbuchStatistik, pfad: str | Path) -> Path:
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(PROTOKOLL_SPALTEN)
        for z in stat.protokoll:
            o, t = z.offen, z.treffer
            w.writerow([o.kandidat.id, "Verzeichnis ohne Website" if o.art == "verzeichnis" else "Website ohne Telefon",
                        o.name, o.strasse, o.plz, o.ort, z.ergebnis, ANZEIGE.get(z.quelle or "", z.quelle), z.kandidaten,
                        t.name if t else None, f"{t.strasse or ''}, {t.plz or ''} {t.ort or ''}" if t else None,
                        t.telefon if t else None, t.website if t else None, z.uebernommen, z.grund])
    return pfad


_EMAIL_IM_SNIPPET = re.compile(r"E-Mail: ([^\s;]+@[^\s;]+)")


def telefonbuch_email(funde: list[Fund], gesperrt: tuple[str, ...] = ()) -> str | None:
    """E-Mail des Telefonbuch-Eintrags (im Snippet des Funds), ohne gesperrte Präfixe (datenschutz@ …)."""
    for f in funde:
        if (f.quelle in ANZEIGE or f.quelle == "kette") and (m := _EMAIL_IM_SNIPPET.search(f.snippet or "")):
            mail = m.group(1).lower()
            if not mail.startswith(gesperrt):
                return mail
    return None


def _quelle_von(funde: list[Fund], passt_amtlich, passt_telefonbuch) -> str | None:
    if any(f.quelle == "amtlich" and f.snippet and passt_amtlich(f.snippet) for f in funde):
        return "amtlich"
    return next((ANZEIGE[f.quelle] for f in funde if f.quelle in ANZEIGE and passt_telefonbuch(f)), None)


def kontakt_quelle(funde: list[Fund], telefon: str | None, email: str | None) -> str | None:
    """Quelle von Telefon und E-Mail für die Lückenliste (Spalte „Kontakt-Quelle", 06.11.2026): Detailseite des
    Verzeichnisses („amtlich"), sonst das Telefonbuch, dessen Fund diesen Wert trägt; beide verschieden → „Telefon: …,
    E-Mail: …"."""
    tel = mail = None
    if telefon and (ziffern := re.sub(r"\D", "", telefon)[-6:]):
        tel = _quelle_von(funde, lambda s: ziffern in re.sub(r"\D", "", s),
                          lambda f: bool(f.telefon) and re.sub(r"\D", "", f.telefon).endswith(ziffern))
    if email:
        e = email.lower()
        mail = _quelle_von(funde, lambda s: e in s.lower(), lambda f: e in (f.snippet or "").lower())
    if tel and mail and tel != mail:
        return f"Telefon: {tel}, E-Mail: {mail}"
    return tel or mail
