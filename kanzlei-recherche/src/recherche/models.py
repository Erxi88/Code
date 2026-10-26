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


# Länderschlüssel (erste zwei Stellen des AGS) je Kürzel
LAND_SCHLUESSEL: dict[str, str] = {
    "SH": "01", "HH": "02", "NI": "03", "HB": "04", "NW": "05", "HE": "06", "RP": "07", "BW": "08",
    "BY": "09", "SL": "10", "BE": "11", "BB": "12", "MV": "13", "SN": "14", "ST": "15", "TH": "16",
}  # fmt: skip


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
    TEAM2 = "team2"  # zweite Team-Kandidatin, z. B. /berufstraeger neben /ueber-uns
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
    """Eine (vermutete) Kanzlei aus der Discovery – aus allen Quellen zusammengeführt.

    Treffer aus Branchenverzeichnissen oder Places haben oft keine Website; deshalb ist
    die Domain optional (aber eindeutig, wenn vorhanden).
    """

    id: int | None = None
    bundesland: str  # Kürzel des Recherche-Laufs, z. B. "SH"
    domain: str | None = None
    website: str | None = None
    name: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    telefon: str | None = None
    suchort: str | None = None  # Ort, für den gesucht wurde
    quelle: str | None = None  # Quelle des ersten Fundes
    gefunden_am: datetime = Field(default_factory=datetime.now)
    crawl_status: SchrittStatus = SchrittStatus.OFFEN
    extract_status: SchrittStatus = SchrittStatus.OFFEN
    fehler: str | None = None


class Fund(BaseModel):
    """Ein einzelner Treffer einer Quelle, zugeordnet zu einem Kandidaten."""

    id: int | None = None
    kandidat_id: int | None = None
    bundesland: str
    quelle: str
    quell_id: str
    lauf_id: str | None = None
    suchbegriff: str | None = None
    suchort: str | None = None
    name: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    telefon: str | None = None
    website: str | None = None
    snippet: str | None = None
    roh_url: str | None = None
    gefunden_am: datetime = Field(default_factory=datetime.now)


class Seite(BaseModel):
    """Gecachte Seite: bereinigter Text plus rohes HTML (in SQLite gzip-komprimiert)."""

    url: str  # angefragte URL (Cache-Schlüssel)
    domain: str
    seitentyp: Seitentyp = Seitentyp.SONSTIGE
    http_status: int | None = None
    text: str | None = None
    html: str | None = None
    final_url: str | None = None  # nach Weiterleitungen
    abgerufen_am: datetime = Field(default_factory=datetime.now)


class Kanzlei(BaseModel):
    id: int | None = None
    kandidat_id: int | None = None
    domain: str | None = None  # None bei Kanzleien ohne Website (nur Discovery-Daten)
    lauf_bundesland: str  # Kürzel des Recherche-Laufs (Filter für Export)
    kanzleiname: str | None = None
    rechtsform: str | None = None
    strasse: str | None = None
    adresszusatz: str | None = None  # „Haus 8 Süd", „2. OG", „c/o …"
    plz: str | None = None
    ort: str | None = None
    bundesland: str | None = None  # tatsächliches Bundesland laut PLZ
    plz_verifiziert: bool | None = None  # PLZ/Ort gegen OpenPLZ geprüft (None: nicht prüfbar)
    telefon: str | None = None
    email: str | None = None
    website: str | None = None
    website_vorhanden: bool = True
    ansprechpartner: list[Ansprechpartner] = Field(default_factory=list)  # Berufsträger und Leitung
    berufstraeger_anzahl: int | None = None
    teamgroesse: int | None = None
    teamgroesse_klasse: TeamKlasse | None = None
    teamgroesse_basis: str | None = None
    konfidenz: Konfidenz = Konfidenz.NIEDRIG
    abweichung: str | None = None  # Unterschiede zwischen Discovery-Quelle und Website
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
    """Domain ohne lesbares Impressum oder mit Fehler – mit den Discovery-Daten zum Nachpflegen."""

    domain: str
    bundesland: str
    grund: str
    url: str | None = None
    zeitpunkt: datetime = Field(default_factory=datetime.now)
    kandidat_id: int | None = None
    name: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    telefon: str | None = None
    website: str | None = None


class DublettenArt(StrEnum):
    ZUSAMMENGEFUEHRT = "zusammengeführt"  # sicher (Domain, Telefon, Adresse + Name)
    UNSICHER = "unsicher"  # zusammengeführt über PLZ + Name – bitte prüfen
    VERDACHT = "verdacht"  # gleiche Adresse, anderer Name – NICHT zusammengeführt


class Dublette(BaseModel):
    behalten: str  # Datensatz, der bleibt (Domain oder Name/Adresse)
    dublette: str  # zusammengeführter bzw. verdächtiger Datensatz
    bundesland: str
    kriterium: str  # domain | telefon | adresse | plz+name
    art: DublettenArt = DublettenArt.ZUSAMMENGEFUEHRT
    kanzleiname: str | None = None
    details: str | None = None
    zeitpunkt: datetime = Field(default_factory=datetime.now)


class LaufEintrag(BaseModel):
    lauf_id: str
    schritt: str  # search | fill | crawl | extract | export
    bundesland: str
    suchbegriff: str | None = None
    adapter: str | None = None
    ort: str | None = None
    trefferzahl: int = 0
    neue_kandidaten: int = 0
    anfragen: int = 0  # tatsächlich gesendete (bezahlte) Anfragen, ohne Cache-Treffer
    kosten: float = 0.0  # anfragen * kosten_je_1000 / 1000
    laufzeit_s: float = 0.0
    fehler: str | None = None
    zeitpunkt: datetime = Field(default_factory=datetime.now)
