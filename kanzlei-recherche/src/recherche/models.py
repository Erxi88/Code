"""Datenmodelle (Pydantic v2) für Kandidaten, Rohseiten, Kanzleien und Protokolle."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

BUNDESLAENDER: dict[str, str] = {
    "SH": "Schleswig-Holstein",
    "HH": "Hamburg",
    "NI": "Niedersachsen",
    "HB": "Bremen",
    "NW": "Nordrhein-Westfalen",
    "HE": "Hessen",
    "RP": "Rheinland-Pfalz",
    "BW": "Baden-Württemberg",
    "BY": "Bayern",
    "SL": "Saarland",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "MV": "Mecklenburg-Vorpommern",
    "SN": "Sachsen",
    "ST": "Sachsen-Anhalt",
    "TH": "Thüringen",
}


class Status(StrEnum):
    NEU = "neu"
    GEPRUEFT = "geprüft"
    KONTAKTIERT = "kontaktiert"
    KEIN_INTERESSE = "kein Interesse"
    UNGUELTIG = "ungültig"


class Konfidenz(StrEnum):
    HOCH = "hoch"
    MITTEL = "mittel"
    NIEDRIG = "niedrig"


class TeamKlasse(StrEnum):
    K1_5 = "1-5"
    K6_15 = "6-15"
    K16_50 = "16-50"
    K50_PLUS = ">50"

    @classmethod
    def aus_zahl(cls, n: int) -> TeamKlasse:
        if n <= 5:
            return cls.K1_5
        if n <= 15:
            return cls.K6_15
        if n <= 50:
            return cls.K16_50
        return cls.K50_PLUS


class SchrittStatus(StrEnum):
    OFFEN = "offen"
    FERTIG = "fertig"
    FEHLER = "fehler"


class Seitentyp(StrEnum):
    START = "start"
    IMPRESSUM = "impressum"
    KONTAKT = "kontakt"
    TEAM = "team"
    SONSTIGE = "sonstige"


class Ansprechpartner(BaseModel):
    name: str
    titel: str | None = None
    funktion: str | None = None

    def anzeige(self) -> str:
        teile = [self.titel, self.name] if self.titel else [self.name]
        text = " ".join(teile)
        return f"{text} ({self.funktion})" if self.funktion else text


class Kandidat(BaseModel):
    """Eine Domain aus der Suche – jede Domain gibt es genau einmal."""

    domain: str
    bundesland: str  # Kürzel des Recherche-Laufs, z. B. "SH"
    url: str
    ort: str | None = None
    suchbegriff: str | None = None
    quelle: str | None = None  # Such-Adapter
    titel: str | None = None
    snippet: str | None = None
    gefunden_am: datetime = Field(default_factory=datetime.now)
    crawl_status: SchrittStatus = SchrittStatus.OFFEN
    extract_status: SchrittStatus = SchrittStatus.OFFEN
    fehler: str | None = None


class Seite(BaseModel):
    """Gecachte Rohseite (als Text)."""

    url: str
    domain: str
    seitentyp: Seitentyp = Seitentyp.SONSTIGE
    http_status: int | None = None
    text: str | None = None
    abgerufen_am: datetime = Field(default_factory=datetime.now)


class Kanzlei(BaseModel):
    id: int | None = None
    domain: str
    lauf_bundesland: str  # Kürzel des Recherche-Laufs (Filter für Export)
    kanzleiname: str | None = None
    rechtsform: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    bundesland: str | None = None  # tatsächliches Bundesland laut PLZ
    telefon: str | None = None
    email: str | None = None
    website: str | None = None
    ansprechpartner: list[Ansprechpartner] = Field(default_factory=list)
    teamgroesse: int | None = None
    teamgroesse_klasse: TeamKlasse | None = None
    teamgroesse_basis: str | None = None
    konfidenz: Konfidenz = Konfidenz.NIEDRIG
    quell_urls: list[str] = Field(default_factory=list)
    abrufdatum: date | None = None
    status: Status = Status.NEU

    @model_validator(mode="after")
    def _klasse_aus_zahl(self) -> Kanzlei:
        if self.teamgroesse is not None and self.teamgroesse_klasse is None:
            self.teamgroesse_klasse = TeamKlasse.aus_zahl(self.teamgroesse)
        return self

    @property
    def hauptansprechpartner(self) -> Ansprechpartner | None:
        return self.ansprechpartner[0] if self.ansprechpartner else None

    @property
    def teamgroesse_anzeige(self) -> int | str | None:
        """Zahl, wenn ablesbar; sonst Klasse."""
        if self.teamgroesse is not None:
            return self.teamgroesse
        return self.teamgroesse_klasse.value if self.teamgroesse_klasse else None


class NichtGefunden(BaseModel):
    domain: str
    bundesland: str
    grund: str
    url: str | None = None
    zeitpunkt: datetime = Field(default_factory=datetime.now)


class Dublette(BaseModel):
    behalten_domain: str  # Datensatz, der in „Kanzleien" bleibt
    dublette_domain: str  # zusammengeführter Datensatz
    bundesland: str
    kriterium: str  # domain | telefon | plz+name
    kanzleiname: str | None = None
    details: str | None = None
    zeitpunkt: datetime = Field(default_factory=datetime.now)


class LaufEintrag(BaseModel):
    lauf_id: str
    schritt: str  # search | crawl | extract | export
    bundesland: str
    suchbegriff: str | None = None
    adapter: str | None = None
    ort: str | None = None
    trefferzahl: int = 0
    neue_domains: int = 0
    laufzeit_s: float = 0.0
    fehler: str | None = None
    zeitpunkt: datetime = Field(default_factory=datetime.now)
