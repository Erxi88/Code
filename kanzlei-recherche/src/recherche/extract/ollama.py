"""Stufe 2: LLM-Extraktion über Ollama (lokal oder GPU-Server).

Immer: JSON-Schema über den Parameter `format` erzwingen, Thinking abschalten
(`think: false`), Kontextfenster `num_ctx` setzen (der Ollama-Standard ist so klein,
dass längere Seiten stillschweigend abgeschnitten würden).
"""

from __future__ import annotations

import logging
import time

import httpx
from pydantic import ValidationError

from ..config import LlmConfig
from ..crawl import haupttext
from ..models import Seite, Seitentyp
from .base import (
    AnbieterNichtErreichbar,
    Extractor,
    ExtraktionFehler,
    ExtraktionsAntwort,
    LlmErgebnis,
    llm_schema,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Du extrahierst Daten einer deutschen Steuerberaterkanzlei aus dem Text ihrer Website
(Impressum, Kontakt, Team, Startseite). Antworte ausschließlich im vorgegebenen JSON-Schema.

Regeln:
- Übernimm nur, was ausdrücklich im Text steht. Fehlt eine Angabe, setze null bzw. eine leere Liste –
  niemals raten, ergänzen oder aus dem Domainnamen ableiten.
- Adresse und Telefon der Kanzlei, NICHT die der Steuerberaterkammer, Aufsichtsbehörde, Versicherung,
  des Hosters oder Webdesigners. Kein Fax.
- personen: alle namentlich genannten Menschen der Kanzlei (auch Mitarbeitende und Auszubildende),
  jeweils Name ohne Titel, Titel separat, Funktion so wie auf der Seite. Keine Personen der Kammer,
  Versicherung, des Webdesigns oder Datenschutzbeauftragte externer Firmen.
- mitarbeiterzahl nur, wenn eine Zahl ausdrücklich genannt wird (z. B. „Team aus 12 Mitarbeitern");
  Personen nicht selbst zählen.
- teamgroesse_klasse nur, wenn keine Zahl genannt ist, aber eine Größenordnung im Text steht."""

# Anteil der Seiten am Textbudget (llm.max_text_chars)
ANTEILE = {Seitentyp.IMPRESSUM: 0.30, Seitentyp.TEAM: 0.45, Seitentyp.KONTAKT: 0.12, Seitentyp.START: 0.13}
REIHENFOLGE = (Seitentyp.IMPRESSUM, Seitentyp.KONTAKT, Seitentyp.TEAM, Seitentyp.START)


class OllamaExtractor(Extractor):
    name = "ollama"

    def __init__(self, cfg: LlmConfig, modell: str | None = None, client: httpx.Client | None = None):
        self.cfg = cfg
        self.modell = modell or cfg.modell
        self._client = client
        self.aufrufe = 0

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.cfg.timeout_s)
        return self._client

    def seiten_text(self, seiten: dict[Seitentyp, Seite]) -> str:
        teile = []
        gesehen: set[str] = set()
        for typ in REIHENFOLGE:
            s = seiten.get(typ)
            if s is None or s.url in gesehen:
                continue
            gesehen.add(s.url)
            text = s.text or ""
            if typ == Seitentyp.TEAM and s.html:
                # Haupttext ohne Navigation – außer trafilatura verwirft offensichtlich zu viel (Kachel-Layouts)
                kurz = haupttext(s.html)
                if kurz and len(kurz) >= 0.15 * len(text):
                    text = kurz
            text = text[: int(self.cfg.max_text_chars * ANTEILE[typ])]
            if text.strip():
                teile.append(f"### {typ.value.upper()} ({s.url})\n{text}")
        return "\n\n".join(teile)[: self.cfg.max_text_chars]

    def build_payload(self, seiten: dict[Seitentyp, Seite], modell: str | None = None) -> dict:
        return {
            "model": modell or self.modell,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self.seiten_text(seiten)},
            ],
            "format": llm_schema(),
            "think": False,
            "stream": False,
            "options": {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx},
        }

    def extract(self, seiten: dict[Seitentyp, Seite]) -> ExtraktionsAntwort:
        letzter_fehler = ""
        for versuch in range(self.cfg.retries + 1):
            daten, dauer = self._anfrage(self.build_payload(seiten))
            inhalt = (daten.get("message") or {}).get("content") or ""
            try:
                ergebnis = LlmErgebnis.model_validate_json(inhalt)
            except ValidationError as e:
                letzter_fehler = f"ungültige Antwort ({e.error_count()} Fehler): {inhalt[:120]!r}"
                log.warning("%s: %s (Versuch %d)", self.modell, letzter_fehler, versuch + 1)
                continue
            ns = daten.get("total_duration")
            return ExtraktionsAntwort(
                ergebnis=ergebnis,
                modell=daten.get("model") or self.modell,
                laufzeit_s=round(ns / 1e9, 3) if ns else round(dauer, 3),
                prompt_tokens=daten.get("prompt_eval_count"),
                antwort_tokens=daten.get("eval_count"),
            )
        raise ExtraktionFehler(f"{self.modell}: {letzter_fehler}")

    def _anfrage(self, payload: dict) -> tuple[dict, float]:
        url = f"{self.cfg.url.rstrip('/')}/api/chat"
        start = time.monotonic()
        try:
            self.aufrufe += 1
            resp = self.client.post(url, json=payload)
        except httpx.TimeoutException as e:
            raise ExtraktionFehler(f"{self.modell}: Zeitüberschreitung nach {self.cfg.timeout_s:.0f} s") from e
        except httpx.HTTPError as e:
            raise AnbieterNichtErreichbar(f"Ollama nicht erreichbar unter {self.cfg.url} ({type(e).__name__})") from e
        if resp.status_code == 404 and "not found" in resp.text.lower():
            if self.cfg.fallback_modell and payload["model"] != self.cfg.fallback_modell:
                log.warning("Modell %s nicht installiert – nehme %s", payload["model"], self.cfg.fallback_modell)
                self.modell = self.cfg.fallback_modell
                return self._anfrage({**payload, "model": self.modell})
            raise AnbieterNichtErreichbar(f"Modell {payload['model']} ist in Ollama nicht installiert (ollama pull)")
        if resp.status_code >= 400:
            raise ExtraktionFehler(f"{self.modell}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            return resp.json(), time.monotonic() - start
        except ValueError as e:
            raise ExtraktionFehler(f"{self.modell}: Antwort ist kein JSON") from e
