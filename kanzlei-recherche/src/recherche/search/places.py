"""Stufe B: Google Places API (New), Text Search über ein adaptives Rechteck-Raster.

- Gebiet eines Ortes: eine Text Search nach „{PLZ} {Ort}, Deutschland" liefert dessen viewport.
- Je Rasterzelle POST places:searchText mit textQuery „Steuerberater", includedType „accounting",
  locationRestriction = Rechteck der Zelle, pageSize 20, bis 3 Seiten über nextPageToken.
- Zellen, die das Trefferlimit (60) erreichen, werden geviertelt, bis min_zelle_km erreicht ist.
- Key aus .env (GOOGLE_PLACES_API_KEY). FieldMask **nur** place_id, Name, Adresse, Telefon, Website –
  keine Bewertungen, Öffnungszeiten oder Fotos. Telefon und Website gehören zur Enterprise-SKU;
  der Preis je 1.000 Anfragen steht in `discovery.quellen.places.kosten_je_1000`.
- Jede gesendete Anfrage zählt fürs Budget (`max_anfragen_je_lauf`) und den Kostenlog; Antworten
  werden in api_cache gespeichert (Wiederaufnahme ohne Doppelkosten).
- Gespeichert wird je Treffer (Tabelle funde, Quelle „places", Abrufdatum = gefunden_am):
  place_id (quell_id), Name, Straße, PLZ, Ort, Telefon, Website.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from ..config import PlacesConfig, env
from .base import (
    Cache,
    KontingentErschoepft,
    OrtsAdapter,
    SearchError,
    SearchHit,
    ZugangVerweigert,
)

if TYPE_CHECKING:
    from .pipeline import Ort

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


FELDER_RASTER = ("places.id,places.displayName,places.formattedAddress,places.addressComponents,"
                 "places.nationalPhoneNumber,places.websiteUri,nextPageToken")
FELDER_GEBIET = "places.id,places.viewport"


def _komponente(place: dict, typ: str) -> str | None:
    for c in place.get("addressComponents") or []:
        if typ in (c.get("types") or []):
            return c.get("longText") or c.get("shortText")
    return None


def place_als_hit(place: dict) -> SearchHit | None:
    """Places-Ergebnis → SearchHit (nur die erlaubten Felder)."""
    pid = place.get("id")
    if not pid:
        return None
    strasse = " ".join(x for x in (_komponente(place, "route"), _komponente(place, "street_number")) if x) or None
    return SearchHit(
        quelle="places", quell_id=pid, name=(place.get("displayName") or {}).get("text"),
        strasse=strasse, plz=_komponente(place, "postal_code"),
        ort=_komponente(place, "locality") or _komponente(place, "postal_town"),
        telefon=place.get("nationalPhoneNumber"), website=place.get("websiteUri"),
        snippet=place.get("formattedAddress"),
        roh_url=f"https://www.google.com/maps/place/?q=place_id:{pid}",
    )  # fmt: skip


class PlacesAdapter(OrtsAdapter):
    """Gebaut und getestet, aber **stillgelegt**: Name und Adresse gehören bei Text Search (New) zur
    kostenpflichtigen Pro-Stufe, kostenlos ist nur „IDs Only" – kein kostenloser Weg als Kompass.
    Zum Wiederbeleben `implementiert = True` setzen (Kosten im Kostenlog)."""

    name = "places"
    cfg: PlacesConfig
    implementiert = False

    def __init__(self, cfg: PlacesConfig | None = None, cache: Cache | None = None,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep, **_):
        super().__init__(cfg or PlacesConfig(), cache)
        self.api_key = env(ENV_KEY)
        self._client = client
        self._sleep = sleep
        self.zellen = 0  # abgefragte Rasterzellen (Statistik)

    def fehlende_keys(self) -> list[str]:
        return [] if self.api_key else [ENV_KEY]

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.cfg.timeout_s)
        return self._client

    # -------------------------------------------------------------- Anfragen

    def _post(self, body: dict, feldmaske: str) -> dict:
        """Eine Anfrage (gecacht, gedrosselt, mit Retry). Zählt fürs Budget und den Kostenlog."""
        schluessel = json.dumps({"body": body, "felder": feldmaske}, sort_keys=True, ensure_ascii=False)
        if self.cache is not None and (gecacht := self.cache.cache_get(self.name, schluessel)) is not None:
            return gecacht
        headers = {"X-Goog-Api-Key": self.api_key or "", "X-Goog-FieldMask": feldmaske,
                   "Content-Type": "application/json"}
        letzter = ""
        for versuch in range(1, self.cfg.retries + 2):
            if self.anfragen:
                self._sleep(self.cfg.pause_s)
            self._anfrage_zaehlen()
            try:
                resp = self.client.post(self.cfg.url, json=body, headers=headers)
            except httpx.TransportError as e:
                letzter = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    antwort = resp.json()
                    if self.cache is not None:
                        self.cache.cache_put(self.name, schluessel, antwort)
                    return antwort
                text = resp.text[:200]
                if resp.status_code in (401, 403):
                    raise ZugangVerweigert(f"places: Zugang verweigert (HTTP {resp.status_code}): {text}")
                if resp.status_code == 429 and "quota" in text.lower() and "per day" in text.lower():
                    raise KontingentErschoepft(f"places: Tageskontingent erschöpft: {text}")
                letzter = f"HTTP {resp.status_code}: {text}"
                if resp.status_code < 500 and resp.status_code != 429:
                    raise SearchError(f"places: {letzter}")
            if versuch <= self.cfg.retries:
                self._sleep(self.cfg.backoff_s * 2 ** (versuch - 1))
        raise SearchError(f"places: nach {self.cfg.retries + 1} Versuchen: {letzter}")

    def gebiet(self, ort: Ort) -> Rechteck | None:
        """Rechteck des Ortes aus dem viewport einer Text Search („24103 Kiel, Deutschland")."""
        antwort = self._post({"textQuery": f"{ort.plz} {ort.name}, Deutschland", "languageCode": "de",
                              "regionCode": "DE", "pageSize": 1}, FELDER_GEBIET)
        for place in antwort.get("places") or []:
            v = place.get("viewport") or {}
            lo, hi = v.get("low") or {}, v.get("high") or {}
            if {"latitude", "longitude"} <= lo.keys() and {"latitude", "longitude"} <= hi.keys():
                return Rechteck(lo["latitude"], lo["longitude"], hi["latitude"], hi["longitude"])
        return None

    def zelle(self, r: Rechteck) -> list[SearchHit]:
        """Bis zu 3 Seiten à 20 Treffer in einer Rasterzelle."""
        self.zellen += 1
        hits: list[SearchHit] = []
        token = None
        for _ in range(max(1, self.cfg.max_treffer_je_zelle // 20)):
            body = {"textQuery": self.cfg.text_query, "includedType": self.cfg.included_type,
                    "locationRestriction": r.als_location_restriction(), "pageSize": 20,
                    "languageCode": "de", "regionCode": "DE"}
            if token:
                body["pageToken"] = token
            antwort = self._post(body, FELDER_RASTER)
            hits += [h for p in antwort.get("places") or [] if (h := place_als_hit(p))]
            if not (token := antwort.get("nextPageToken")):
                break
        return hits

    def search_ort(self, ort: Ort) -> list[SearchHit]:
        r = self.gebiet(ort)
        if r is None:
            raise SearchError(f"places: Gebiet für {ort.plz} {ort.name} nicht gefunden")
        return raster_suche(r, self.zelle, self.cfg.zelle_km, self.cfg.max_treffer_je_zelle, self.cfg.min_zelle_km)
