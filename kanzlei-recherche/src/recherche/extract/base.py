"""Interface für austauschbare Extraktions-Anbieter (Ollama, später weitere).

Grundregel: Fehlende Felder bleiben None – nie raten.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from ..models import Seite, Seitentyp, TeamKlasse


class RegelErgebnis(BaseModel):
    """Stufe 1: regelbasiert aus dem Impressum (Vorrang vor dem LLM für Adresse und Telefon)."""

    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    telefon: str | None = None
    email: str | None = None


class LlmPerson(BaseModel):
    name: str = Field(description="Vor- und Nachname ohne Titel")
    titel: str | None = Field(None, description="akademischer Grad/Titel, z. B. 'Dr.', 'Dipl.-Kfm.'; sonst null")
    funktion: str | None = Field(
        None, description="Funktion wie auf der Seite, z. B. 'Steuerberater, Partner', 'Steuerfachangestellte'"
    )


class LlmErgebnis(BaseModel):
    """Stufe 2 per LLM. Dieses Modell ist zugleich das erzwungene JSON-Schema (Ollama `format`)."""

    kanzleiname: str | None = Field(None, description="vollständiger Name der Kanzlei laut Impressum")
    rechtsform: str | None = Field(
        None, description="z. B. Einzelkanzlei, PartG mbB, GmbH, GbR; null wenn nicht erkennbar"
    )
    strasse: str | None = Field(None, description="Straße und Hausnummer der Kanzlei (nicht der Kammer)")
    plz: str | None = Field(None, description="fünfstellige PLZ der Kanzlei")
    ort: str | None = Field(None, description="Ort der Kanzlei")
    telefon: str | None = Field(None, description="Telefonnummer der Kanzlei (kein Fax)")
    email: str | None = Field(None, description="E-Mail-Adresse der Kanzlei")
    verantwortliche_person: str | None = Field(
        None, description="Inhaber/in bzw. vertretungsberechtigte Person laut Impressum, Name ohne Titel"
    )
    personen: list[LlmPerson] = Field(
        default_factory=list,
        description="alle namentlich genannten Personen der Kanzlei (nicht Kammer, Hoster, Webdesigner)",
    )
    mitarbeiterzahl: int | None = Field(
        None, description="Anzahl Mitarbeitende, nur wenn im Text ausdrücklich als Zahl genannt"
    )
    mitarbeiterzahl_beleg: str | None = Field(None, description="wörtliche Textstelle zur Mitarbeiterzahl")
    teamgroesse_klasse: TeamKlasse | None = Field(
        None, description="nur wenn keine Zahl genannt ist, der Text aber eine Größenordnung angibt"
    )


def llm_schema() -> dict:
    """JSON-Schema für den Ollama-Parameter `format`."""
    return LlmErgebnis.model_json_schema()


class ExtraktionsAntwort(BaseModel):
    ergebnis: LlmErgebnis
    modell: str
    laufzeit_s: float = 0.0
    prompt_tokens: int | None = None
    antwort_tokens: int | None = None

    @property
    def tokens(self) -> int | None:
        if self.prompt_tokens is None and self.antwort_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.antwort_tokens or 0)


class ExtraktionFehler(RuntimeError):
    """Die Extraktion eines Kandidaten ist gescheitert (z. B. dauerhaft ungültige Antwort)."""


class AnbieterNichtErreichbar(ExtraktionFehler):
    """Der Anbieter ist nicht erreichbar – der Lauf wird beendet statt alle Kandidaten zu markieren."""


class Extractor(ABC):
    """Ein LLM-Anbieter, der aus den Seiten eines Kandidaten ein LlmErgebnis macht."""

    name: str
    modell: str

    @abstractmethod
    def extract(self, seiten: dict[Seitentyp, Seite]) -> ExtraktionsAntwort:
        """Extrahiert Name, Rechtsform, Personen, Teamgröße (und Kontakt als Rückfall)."""
