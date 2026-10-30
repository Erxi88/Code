"""Extraktionslauf: gecrawlte Seiten → Regeln + LLM → Kanzlei (wiederaufnehmbar über extract_status).

- Kandidaten mit erfolgreichem Crawl: Regeln auf Impressum (Rückfall Kontakt, Start), LLM auf
  allen Seiten, Zusammenführung mit den Discovery-Daten.
- Kandidaten ohne Website: Kanzlei nur aus Discovery-Daten (Spalte „Website vorhanden" = nein).
- Kandidaten mit Crawl-Fehler stehen im Blatt „Nicht gefunden" und werden hier übersprungen.
- Ist Ollama nicht erreichbar, endet der Lauf sauber; ein erneuter Aufruf macht weiter.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..crawl import links, meta_texte
from ..models import Kandidat, LaufEintrag, SchrittStatus, Seitentyp, Status
from ..normalize import PlzPruefer
from ..storage import Storage
from ..textnorm import text_normalisieren
from .base import AnbieterNichtErreichbar, Extractor, ExtraktionFehler
from .kanzlei import (
    ausland_tld,
    pruefen,
    stammdomain,
    status_setzen,
    suchorte_je_stammdomain,
)
from .merge import zusammenfuehren
from .prompt import seiten_rahmen
from .rules import Ausschluss, extract_impressum, festnetz_aus_html, ist_mobil

log = logging.getLogger(__name__)


@dataclass
class ExtractStatistik:
    fertig: int = 0
    ohne_website: int = 0
    fehler: int = 0
    llm_aufrufe: int = 0
    llm_laufzeit_s: float = 0.0
    tokens: int = 0
    gekuerzt: int = 0  # Kanzleien, deren Seitentext für den Prompt gekürzt wurde
    kontext_voll: int = 0  # Prompt + Antwort ≥ 95 % von num_ctx (Ollama hat vermutlich abgeschnitten)
    ohne_llm_ausserhalb: int = 0  # PLZ laut Regel außerhalb des Bundeslandes – LLM übersprungen
    abbruch: str | None = None

    def hinweis(self) -> str | None:
        teile = []
        if self.gekuerzt:
            teile.append(f"gekürzt: {self.gekuerzt} Kanzleien")
        if self.kontext_voll:
            teile.append(f"Kontext fast voll: {self.kontext_voll} Kanzleien")
        return "; ".join(teile) or None

    @property
    def mittlere_laufzeit_s(self) -> float | None:
        return self.llm_laufzeit_s / self.llm_aufrufe if self.llm_aufrufe else None


def regeln_fuer(seiten, ausschluss: Ausschluss, domain: str | None):
    """Regel-Extraktion: Impressum zuerst, Kontakt- und Startseite für fehlende Felder."""
    reihenfolge = [seiten[t] for t in (Seitentyp.IMPRESSUM, Seitentyp.KONTAKT, Seitentyp.START) if t in seiten]
    if not reihenfolge:
        return None
    texte = [(text_normalisieren(s.text) or "", [u for u, _ in links(s.html, s.final_url or s.url)] if s.html else [])
             for s in reihenfolge]  # fmt: skip
    # letzter Rückfall: Meta-Beschreibungen (E-Mail steht manchmal nur dort)
    meta = "\n".join(m for s in reihenfolge for m in meta_texte(s.html))
    if meta:
        texte.append((meta, []))
    (text, lnks), *weitere = texte
    erg = extract_impressum(text, ausschluss, lnks, domain, weitere, rahmen=seiten_rahmen(seiten))
    if erg.telefon is None or ist_mobil(erg.telefon):
        # Festnetz fehlt im bereinigten Text (z. B. Standort-Block der Fußzeile entfernt): rohes HTML
        if festnetz := festnetz_aus_html([s.html for s in reihenfolge], erg.plz):
            erg.telefon = festnetz
    return erg


def run_extract(
    storage: Storage,
    bundesland: str,
    extractor: Extractor | None,
    pruefer: PlzPruefer | None,
    ausschluss: Ausschluss,
    lauf_id: str,
    limit: int | None = None,
    fortschritt: Callable[[str], None] | None = None,
    kanzlei_rechtsformen: list[str] | None = None,
    ziel_land: str | None = None,
    blocklist: set[str] | None = None,
) -> ExtractStatistik:
    """kanzlei_rechtsformen gesetzt → Kanzlei-Erkennung aktiv (extract.kanzlei_pruefung).
    ziel_land („Schleswig-Holstein") → PLZ in anderem Land: Status „außerhalb Bundesland"."""
    stat = ExtractStatistik()
    start = time.monotonic()
    offen = [
        k for k in storage.kandidaten(bundesland, extract_status=SchrittStatus.OFFEN)
        if k.crawl_status == SchrittStatus.FERTIG or not (k.domain or k.website)
    ]  # fmt: skip
    if limit:
        offen = offen[:limit]
    suchorte = suchorte_je_stammdomain(storage, bundesland) if kanzlei_rechtsformen is not None else {}
    for k in offen:
        try:
            kanzlei = _eine(storage, k, extractor, pruefer, ausschluss, stat, ziel_land)
            if kanzlei_rechtsformen is not None and k.domain:
                pruefen(kanzlei, storage.kandidat_seiten(k.id), tuple(kanzlei_rechtsformen), storage.funde_kandidat(k.id),
                        suchorte.get(stammdomain(k.domain), 0), ziel_land, blocklist)
            else:
                status_setzen(kanzlei, ziel_land)
        except AnbieterNichtErreichbar as e:
            stat.abbruch = str(e)
            break
        except ExtraktionFehler as e:
            stat.fehler += 1
            storage.set_extract_status(k.id, SchrittStatus.FEHLER, str(e))
            if fortschritt:
                fortschritt(f"{k.domain or k.name}: Fehler – {e}")
            continue
        except Exception as e:  # unerwarteter Fehler eines Kandidaten darf den Lauf nicht anhalten
            log.exception("Extraktion %s fehlgeschlagen", k.domain or k.name)
            stat.fehler += 1
            storage.set_extract_status(k.id, SchrittStatus.FEHLER, f"unerwarteter Fehler: {type(e).__name__}: {e}"[:500])
            if fortschritt:
                fortschritt(f"{k.domain or k.name}: unerwarteter Fehler – {type(e).__name__}: {e} (Kandidat übersprungen)")
            continue
        storage.upsert_kanzlei(kanzlei)
        felder = {"telefon": kanzlei.telefon, "name": kanzlei.kanzleiname}
        if not k.plz:  # Adresse nur als Einheit ergänzen
            felder.update(strasse=kanzlei.strasse, plz=kanzlei.plz, ort=kanzlei.ort)
        storage.ergaenze_kandidat(k.id, **felder)
        storage.set_extract_status(k.id, SchrittStatus.FERTIG)
        stat.fertig += 1
        if fortschritt:
            zusatz = f", {kanzlei.status.value}" if kanzlei.status in (Status.KEINE_KANZLEI, Status.AUSSERHALB) else ""
            zusatz += ", ohne LLM" if "ohne LLM:" in (kanzlei.abweichung or "") else ""
            fortschritt(f"{k.domain or kanzlei.kanzleiname}: {kanzlei.kanzleiname or '–'} ({kanzlei.konfidenz.value}{zusatz})")
    storage.log_lauf(
        LaufEintrag(
            lauf_id=lauf_id, schritt="extract", bundesland=bundesland,
            adapter=extractor.modell if extractor else "nur Regeln",
            suchbegriff=f"{stat.fertig} Kanzleien, {stat.fehler} Fehler", trefferzahl=stat.fertig,
            anfragen=stat.llm_aufrufe, laufzeit_s=round(time.monotonic() - start, 2),
            fehler="; ".join(x for x in (stat.abbruch, stat.hinweis()) if x) or None,
        )
    )  # fmt: skip
    return stat


def _eine(storage: Storage, k: Kandidat, extractor, pruefer, ausschluss, stat: ExtractStatistik,
          ziel_land: str | None = None):
    funde = storage.funde_kandidat(k.id)
    if not (k.domain or k.website):
        stat.ohne_website += 1
        return zusammenfuehren(k, funde, {}, None, None, pruefer.pruefe if pruefer else None, ausschluss)
    seiten = storage.kandidat_seiten(k.id)
    regel = regeln_fuer(seiten, ausschluss, k.domain)
    llm = None
    if extractor is not None and ziel_land and (tld := ausland_tld(k.domain)):
        # .at/.ch/.tirol …: außerhalb ohne LLM (Österreich, Schweiz) – Status setzt status_setzen/pruefen
        stat.ohne_llm_ausserhalb += 1
        kanzlei = zusammenfuehren(k, funde, seiten, regel, None, pruefer.pruefe if pruefer else None, ausschluss)
        kanzlei.abweichung = "; ".join(x for x in (kanzlei.abweichung, f"ohne LLM: ausländische Domain ({tld})") if x)
        return kanzlei
    if extractor is not None and seiten and ziel_land and pruefer:
        # Vorprüfung ohne LLM: liegt die PLZ (Discovery bzw. Impressum-Regel) außerhalb des Bundeslandes,
        # spart das den LLM-Aufruf (Kleingemeinden: Brave füllt mit bundesweiten Kanzleien auf)
        try:
            vorab = zusammenfuehren(k, funde, seiten, regel, None, pruefer.pruefe, ausschluss)
            regel_plz = vorab.plz
            grund = _ausserhalb_vorab(vorab, seiten, ausschluss, pruefer, ziel_land)
            log.info("%s: Vorprüfung – Regel-PLZ %s (%s), PLZ im Text %s → %s", k.domain, regel_plz or "keine",
                     vorab.bundesland or "?", plz_in_seiten(seiten, ausschluss)[:5], grund or "LLM")
        except Exception:  # Vorprüfung ist nur eine Abkürzung – bei Fehlern normal mit LLM weiter
            log.exception("%s: Vorprüfung ohne LLM fehlgeschlagen – weiter mit LLM", k.domain)
            vorab, grund = None, None
        if vorab is not None and grund:
            stat.ohne_llm_ausserhalb += 1
            vorab.abweichung = "; ".join(x for x in (vorab.abweichung, f"ohne LLM: {grund}") if x)
            return vorab
    if extractor is not None and seiten:
        antwort = extractor.extract(seiten)
        llm = antwort.ergebnis
        stat.llm_aufrufe += 1
        stat.llm_laufzeit_s += antwort.laufzeit_s
        stat.tokens += antwort.tokens or 0
        stat.gekuerzt += bool(antwort.kuerzung)
        stat.kontext_voll += antwort.kontext_voll
        if antwort.kuerzung or antwort.kontext_voll:
            log.info("%s: Prompt gekürzt (%s)%s", k.domain, antwort.kuerzung, ", Kontext fast voll" if antwort.kontext_voll else "")
    kanzlei = zusammenfuehren(k, funde, seiten, regel, llm, pruefer.pruefe if pruefer else None, ausschluss)
    if hinweis := _robots_hinweis(storage, seiten):
        kanzlei.abweichung = "; ".join(x for x in (kanzlei.abweichung, hinweis) if x)
    return kanzlei


# PLZ + Ort, auch über einen Zeilenumbruch („10115\nBerlin") und ohne Trennzeichen dahinter
_PLZ_LOSE = re.compile(r"(?<![\d.,/-])(?:D\s*-\s*)?(\d{5})[ \t]*\n?[ \t]*([A-ZÄÖÜ][a-zäöüß]+)")
_KEINE_PLZ_DAVOR = re.compile(r"(?:HR\s?[AB]|PR|VR|GnR|Nr\.?|Nummer|Konto|IBAN|BLZ|USt|St\.?-?Nr)\W*$", re.I)


def plz_in_seiten(seiten, ausschluss: Ausschluss) -> list[str]:
    return list(_plz_orte_in_seiten(seiten, ausschluss))


def _plz_orte_in_seiten(seiten, ausschluss: Ausschluss) -> dict[str, str]:
    """Alle PLZ mit Ort aus Impressum, Kontakt- und Startseite – ohne Fremd-Abschnitte (Kammer, Versicherung,
    Hoster) und ohne Register-/Kontonummern. Für die Vorprüfung, unabhängig von der strengen Adressregel."""
    gefunden: dict[str, str] = {}  # PLZ → Ort
    for t in (Seitentyp.IMPRESSUM, Seitentyp.KONTAKT, Seitentyp.START):
        if t not in seiten:
            continue
        zeilen = (text_normalisieren(seiten[t].text) or "").splitlines()
        fremd = ausschluss.fremd_zeilen(zeilen)
        text = "\n".join("" if i in fremd else z for i, z in enumerate(zeilen))
        for m in _PLZ_LOSE.finditer(text):
            if not _KEINE_PLZ_DAVOR.search(text[max(0, m.start() - 20): m.start()]):
                gefunden.setdefault(m.group(1), m.group(2))
    return gefunden


def _ausserhalb_vorab(vorab, seiten, ausschluss: Ausschluss, pruefer: PlzPruefer, ziel_land: str) -> str | None:
    """Grund, wenn die Kanzlei sicher außerhalb liegt: Regel-Adresse außerhalb, oder ohne Regel-Adresse alle
    PLZ der Seiten außerhalb (mindestens eine bekannt). Eine PLZ im Land → None (LLM entscheidet)."""
    if vorab.plz:
        if vorab.bundesland and vorab.bundesland != ziel_land:
            return f"PLZ {vorab.plz} liegt in {vorab.bundesland}"
        return None
    orte = _plz_orte_in_seiten(seiten, ausschluss)
    laender = {plz: pruefer.pruefe(plz, None).bundesland for plz in list(orte)[:8]}
    bekannt = {p: land for p, land in laender.items() if land}
    if bekannt and ziel_land not in bekannt.values():
        p, land = next(iter(bekannt.items()))
        # PLZ, Ort und Bundesland übernehmen, damit Status und Blatt „Außerhalb Bundesland" stimmen
        vorab.plz, vorab.ort, vorab.bundesland = p, orte[p], land
        return f"PLZ {p} (aus dem Seitentext) liegt in {land}"
    return None


def _robots_hinweis(storage: Storage, seiten) -> str | None:
    """Gelesen, obwohl die robots.txt nicht abrufbar war (5xx, Timeout) – aus dem robots-Cache."""
    if (start := seiten.get(Seitentyp.START)) is None:
        return None
    teile = urlsplit(start.url)
    c = storage.cache_get("robots", f"{teile.scheme}://{teile.netloc}")
    if not c or (c.get("status") is not None and c["status"] < 500):
        return None
    return f"robots.txt nicht abrufbar ({c.get('fehler') or 'HTTP ' + str(c['status'])}), Seite gelesen"
