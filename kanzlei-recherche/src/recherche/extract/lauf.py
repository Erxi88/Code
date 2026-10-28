"""Extraktionslauf: gecrawlte Seiten → Regeln + LLM → Kanzlei (wiederaufnehmbar über extract_status).

- Kandidaten mit erfolgreichem Crawl: Regeln auf Impressum (Rückfall Kontakt, Start), LLM auf
  allen Seiten, Zusammenführung mit den Discovery-Daten.
- Kandidaten ohne Website: Kanzlei nur aus Discovery-Daten (Spalte „Website vorhanden" = nein).
- Kandidaten mit Crawl-Fehler stehen im Blatt „Nicht gefunden" und werden hier übersprungen.
- Ist Ollama nicht erreichbar, endet der Lauf sauber; ein erneuter Aufruf macht weiter.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..crawl import links, meta_texte
from ..models import Kandidat, LaufEintrag, SchrittStatus, Seitentyp, Status
from ..normalize import PlzPruefer
from ..storage import Storage
from ..textnorm import text_normalisieren
from .base import AnbieterNichtErreichbar, Extractor, ExtraktionFehler
from .kanzlei import pruefen, stammdomain, suchorte_je_stammdomain
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
) -> ExtractStatistik:
    """kanzlei_rechtsformen gesetzt → Kanzlei-Erkennung aktiv (extract.kanzlei_pruefung)."""
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
            kanzlei = _eine(storage, k, extractor, pruefer, ausschluss, stat)
            if kanzlei_rechtsformen is not None and k.domain:
                pruefen(kanzlei, storage.kandidat_seiten(k.id), tuple(kanzlei_rechtsformen), storage.funde_kandidat(k.id),
                        suchorte.get(stammdomain(k.domain), 0))
        except AnbieterNichtErreichbar as e:
            stat.abbruch = str(e)
            break
        except ExtraktionFehler as e:
            stat.fehler += 1
            storage.set_extract_status(k.id, SchrittStatus.FEHLER, str(e))
            if fortschritt:
                fortschritt(f"{k.domain or k.name}: Fehler – {e}")
            continue
        storage.upsert_kanzlei(kanzlei)
        felder = {"telefon": kanzlei.telefon, "name": kanzlei.kanzleiname}
        if not k.plz:  # Adresse nur als Einheit ergänzen
            felder.update(strasse=kanzlei.strasse, plz=kanzlei.plz, ort=kanzlei.ort)
        storage.ergaenze_kandidat(k.id, **felder)
        storage.set_extract_status(k.id, SchrittStatus.FERTIG)
        stat.fertig += 1
        if fortschritt:
            zusatz = ", keine Kanzlei" if kanzlei.status == Status.KEINE_KANZLEI else ""
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


def _eine(storage: Storage, k: Kandidat, extractor, pruefer, ausschluss, stat: ExtractStatistik):
    funde = storage.funde_kandidat(k.id)
    if not (k.domain or k.website):
        stat.ohne_website += 1
        return zusammenfuehren(k, funde, {}, None, None, pruefer.pruefe if pruefer else None, ausschluss)
    seiten = storage.kandidat_seiten(k.id)
    regel = regeln_fuer(seiten, ausschluss, k.domain)
    llm = None
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
    return zusammenfuehren(k, funde, seiten, regel, llm, pruefer.pruefe if pruefer else None, ausschluss)
