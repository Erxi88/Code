"""Stufe B: Google Places API (New), Text Search + Raster – Platzhalter mit fertigem Raster.

Vorgesehen (Places-Sitzung):
- POST places:searchText mit textQuery „Steuerberater", includedType „accounting",
  locationRestriction = Rechteck einer Rasterzelle, pageSize 20, Paging über pageToken
- Key aus .env (GOOGLE_PLACES_API_KEY), FieldMask nur mit den benötigten Feldern
- Ort → Rechteck (Begrenzung aus PLZ-Geodaten, kommt mit dem Befehl `orte`)
- Zellen, die das Trefferlimit erreichen, werden geviertelt, bis min_zelle_km erreicht ist
- Nutzungsbedingungen zum Speichern von Places-Inhalten vor dem Bau klären

Das Raster selbst (lückenlose Zerlegung, adaptive Verfeinerung) ist schon hier gebaut
und getestet.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from ..config import PlacesConfig, env
from .base import Cache, OrtsPlatzhalter, SearchHit

ENV_KEY = "GOOGLE_PLACES_API_KEY"
KM_JE_GRAD_BREITE = 111.32


@dataclass(frozen=True)
class Rechteck:
    sued: float
    west: float
    nord: float
    ost: float

    @property
    def mitte_breite(self) -> float:
        return (self.sued + self.nord) / 2

    def hoehe_km(self) -> float:
        return (self.nord - self.sued) * KM_JE_GRAD_BREITE

    def breite_km(self) -> float:
        return (self.ost - self.west) * KM_JE_GRAD_BREITE * math.cos(math.radians(self.mitte_breite))

    def viertel(self) -> list[Rechteck]:
        mb, ml = (self.sued + self.nord) / 2, (self.west + self.ost) / 2
        return [
            Rechteck(self.sued, self.west, mb, ml),
            Rechteck(self.sued, ml, mb, self.ost),
            Rechteck(mb, self.west, self.nord, ml),
            Rechteck(mb, ml, self.nord, self.ost),
        ]

    def als_location_restriction(self) -> dict:
        return {
            "rectangle": {
                "low": {"latitude": self.sued, "longitude": self.west},
                "high": {"latitude": self.nord, "longitude": self.ost},
            }
        }


def rechteck_um(lat: float, lon: float, radius_km: float) -> Rechteck:
    d_lat = radius_km / KM_JE_GRAD_BREITE
    d_lon = radius_km / (KM_JE_GRAD_BREITE * math.cos(math.radians(lat)))
    return Rechteck(lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon)


def zerlege(r: Rechteck, zelle_km: float) -> list[Rechteck]:
    """Lückenlose Zerlegung in Zellen von höchstens zelle_km Kantenlänge (Kanten fallen zusammen)."""
    zeilen = max(1, math.ceil(r.hoehe_km() / zelle_km))
    spalten = max(1, math.ceil(r.breite_km() / zelle_km))
    d_lat = (r.nord - r.sued) / zeilen
    d_lon = (r.ost - r.west) / spalten
    zellen = []
    for i in range(zeilen):
        sued = r.sued + i * d_lat
        nord = r.nord if i == zeilen - 1 else sued + d_lat
        for j in range(spalten):
            west = r.west + j * d_lon
            ost = r.ost if j == spalten - 1 else west + d_lon
            zellen.append(Rechteck(sued, west, nord, ost))
    return zellen


def raster_suche(
    gebiet: Rechteck,
    abfrage: Callable[[Rechteck], list[SearchHit]],
    zelle_km: float,
    max_treffer_je_zelle: int,
    min_zelle_km: float,
) -> list[SearchHit]:
    """Fragt jede Zelle ab; volle Zellen werden geviertelt, bis min_zelle_km erreicht ist.

    Ergebnis ohne Doppelte (nach quell_id), Reihenfolge des ersten Auftretens.
    """
    gesehen: dict[str, SearchHit] = {}

    def besuche(zelle: Rechteck) -> None:
        treffer = abfrage(zelle)
        for h in treffer:
            gesehen.setdefault(h.quell_id, h)
        kante = max(zelle.hoehe_km(), zelle.breite_km())
        if len(treffer) >= max_treffer_je_zelle and kante / 2 >= min_zelle_km:
            for teil in zelle.viertel():
                besuche(teil)

    for zelle in zerlege(gebiet, zelle_km):
        besuche(zelle)
    return list(gesehen.values())


class PlacesAdapter(OrtsPlatzhalter):
    name = "places"
    cfg: PlacesConfig

    def __init__(self, cfg: PlacesConfig | None = None, cache: Cache | None = None, **_):
        super().__init__(cfg or PlacesConfig(), cache)
        self.api_key = env(ENV_KEY)

    def fehlende_keys(self) -> list[str]:
        return [] if self.api_key else [ENV_KEY]
