"""Extraktion: regelbasiert (rules), LLM (ollama, austauschbar über base.Extractor), Zusammenführung (merge)."""

from .base import Extractor, ExtraktionsAntwort, LlmErgebnis, RegelErgebnis

__all__ = ["Extractor", "ExtraktionsAntwort", "LlmErgebnis", "RegelErgebnis"]
