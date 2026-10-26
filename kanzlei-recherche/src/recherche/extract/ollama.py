"""Stufe 2: LLM-Extraktion über Ollama (lokal oder GPU-Server).

Immer: JSON-Schema über den Parameter `format` erzwingen und Thinking abschalten
(`think: false`). Der HTTP-Aufruf folgt in der Extraktions-Sitzung; der Payload ist
schon hier festgelegt und getestet.
"""

from __future__ import annotations

from ..config import LlmConfig
from ..models import Seite
from .base import Extractor, LlmErgebnis, llm_schema

SYSTEM_PROMPT = (
    "Du extrahierst Daten einer deutschen Steuerberaterkanzlei aus Webseiten-Text "
    "(Impressum, Kontakt, Team). Antworte ausschließlich im vorgegebenen JSON-Schema. "
    "Übernimm nur, was ausdrücklich im Text steht. Fehlt eine Angabe, setze null bzw. eine "
    "leere Liste – niemals raten oder ergänzen."
)


class OllamaExtractor(Extractor):
    name = "ollama"

    def __init__(self, cfg: LlmConfig, modell: str | None = None):
        self.cfg = cfg
        self.modell = modell or cfg.modell

    def seiten_text(self, seiten: list[Seite]) -> str:
        teile = [f"### {s.seitentyp.value.upper()} ({s.url})\n{s.text or ''}" for s in seiten if s.text]
        return "\n\n".join(teile)[: self.cfg.max_text_chars]

    def build_payload(self, seiten: list[Seite]) -> dict:
        return {
            "model": self.modell,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self.seiten_text(seiten)},
            ],
            "format": llm_schema(),
            "think": False,
            "stream": False,
            "options": {"temperature": self.cfg.temperature},
        }

    def extract(self, seiten: list[Seite]) -> LlmErgebnis:
        raise NotImplementedError("Ollama-Aufruf folgt in der Extraktions-Sitzung")
