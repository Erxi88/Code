"""Extraktion: regelbasiert (rules) und LLM (austauschbar über base.Extractor)."""

from .base import Extractor, LlmErgebnis, RegelErgebnis

__all__ = ["Extractor", "LlmErgebnis", "RegelErgebnis"]
