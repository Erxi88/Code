"""Interface für austauschbare Extraktions-Anbieter (Ollama, später weitere).

Grundregel: Fehlende Felder bleiben None – nie raten.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from ..models import Ansprechpartner, Seite, TeamKlasse


class RegelErgebnis(BaseModel):
    """Stufe 1: regelbasiert aus dem Impressum."""

    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    telefon: str | None = None
    email: str | None = None


class LlmErgebnis(BaseModel):
    """Stufe 2: per LLM. Dieses Modell ist zugleich das erzwungene JSON-Schema."""

    kanzleiname: str | None = Field(None, description="Name der Kanzlei laut Impressum")
    rechtsform: str | None = Field(
        None, description="z. B. Einzelkanzlei, PartG mbB, GmbH, GbR; null wenn nicht genannt"
    )
    ansprechpartner: list[Ansprechpartner] = Field(
        default_factory=list,
        description="Berufsträger/Inhaber zuerst; Titel z. B. 'Dipl.-Kfm.', Funktion z. B. 'Steuerberater, Partner'",
    )
    teamgroesse: int | None = Field(None, description="Anzahl Mitarbeitende, nur wenn ablesbar")
    teamgroesse_klasse: TeamKlasse | None = Field(
        None, description="nur wenn keine Zahl ablesbar, aber eine Größenordnung erkennbar ist"
    )
    teamgroesse_basis: str | None = Field(
        None, description="woraus die Teamgröße abgeleitet wurde, z. B. '12 Personen auf Teamseite'"
    )


def llm_schema() -> dict:
    """JSON-Schema für den Ollama-Parameter `format`."""
    return LlmErgebnis.model_json_schema()


class Extractor(ABC):
    """Ein LLM-Anbieter, der aus Rohseiten ein LlmErgebnis macht."""

    name: str

    @abstractmethod
    def extract(self, seiten: list[Seite]) -> LlmErgebnis:
        """Extrahiert Name, Rechtsform, Ansprechpartner und Teamgröße."""
