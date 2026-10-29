"""Places-Adapter (Text Search New) mit gespeicherten Beispielantworten – kein Netz."""

import json

import httpx
import pytest

from recherche.config import PlacesConfig
from recherche.search.base import BudgetErschoepft, ZugangVerweigert
from recherche.search.pipeline import Ort, run_search
from recherche.search.places import (
    FELDER_GEBIET,
    FELDER_RASTER,
    PlacesAdapter,
    place_als_hit,
)

KIEL_VIEWPORT = {"places": [{"id": "ChIJkiel", "viewport": {"low": {"latitude": 54.30, "longitude": 10.10},
                                                             "high": {"latitude": 54.34, "longitude": 10.16}}}]}


def _place(i, telefon="0431 123450", website=None, plz="24103", strasse=("Holstenstraße", "1")):
    return {
        "id": f"place-{i}", "displayName": {"text": f"Steuerkanzlei {i}", "languageCode": "de"},
        "formattedAddress": f"{strasse[0]} {strasse[1]}, {plz} Kiel, Deutschland",
        "addressComponents": [
            {"longText": strasse[1], "types": ["street_number"]}, {"longText": strasse[0], "types": ["route"]},
            {"longText": plz, "types": ["postal_code"]}, {"longText": "Kiel", "types": ["locality", "political"]},
        ],
        "nationalPhoneNumber": telefon, "websiteUri": website,
        "rating": 4.8, "regularOpeningHours": {"weekdayDescriptions": ["…"]},  # darf nicht gespeichert werden
    }  # fmt: skip


def _adapter(antworten, monkeypatch, cache=None, **cfg):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    anfragen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        anfragen.append((body, req.headers["X-Goog-FieldMask"], req.headers["X-Goog-Api-Key"]))
        if req.headers["X-Goog-FieldMask"] == FELDER_GEBIET:
            return httpx.Response(200, json=KIEL_VIEWPORT)
        return antworten(body)

    a = PlacesAdapter(PlacesConfig(zelle_km=10.0, **cfg), cache=cache,
                      client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    return a, anfragen


def test_place_als_hit_nur_erlaubte_felder():
    h = place_als_hit(_place(1, website="https://k1.example/"))
    assert (h.quelle, h.quell_id, h.name, h.strasse, h.plz, h.ort, h.telefon, h.website) == (
        "places", "place-1", "Steuerkanzlei 1", "Holstenstraße 1", "24103", "Kiel", "0431 123450", "https://k1.example/")
    assert "rating" not in h.model_dump_json() and "4.8" not in h.model_dump_json()


def test_raster_mit_seiten_und_feldmaske(monkeypatch):
    seiten = {None: ([_place(i) for i in range(20)], "t2"), "t2": ([_place(i) for i in range(20, 25)], None)}

    def antworten(body):
        places, token = seiten[body.get("pageToken")]
        return httpx.Response(200, json={"places": places, **({"nextPageToken": token} if token else {})})

    a, anfragen = _adapter(antworten, monkeypatch)
    hits = a.search_ort(Ort("Kiel", "24103", "Kreisfreie Stadt Kiel"))
    assert len(hits) == 25 and a.anfragen == 3  # 1 × Gebiet + 2 Seiten
    gebiet, raster = anfragen[0], anfragen[1]
    assert gebiet[0]["textQuery"] == "24103 Kiel, Deutschland" and gebiet[2] == "test-key"
    assert raster[1] == FELDER_RASTER and "rating" not in raster[1] and "Hours" not in raster[1] and "photos" not in raster[1]
    assert raster[0]["includedType"] == "accounting" and "rectangle" in raster[0]["locationRestriction"]
    assert a.kosten() == pytest.approx(3 * 35 / 1000)


def test_volle_zelle_wird_geviertelt(monkeypatch):
    def antworten(body):
        rect = body["locationRestriction"]["rectangle"]
        groesse = rect["high"]["latitude"] - rect["low"]["latitude"]
        n = 20 if groesse > 0.03 else 2  # große Zelle: volle Seiten, Viertel: wenige
        start = int(rect["low"]["longitude"] * 1e4) + int(rect["low"]["latitude"] * 1e4) * 7
        token = "weiter" if n == 20 and body.get("pageToken") != "letzte" else None
        if body.get("pageToken") == "weiter":
            token = "letzte"
        return httpx.Response(200, json={"places": [_place(start + i + 100 * (body.get("pageToken") is not None))
                                                     for i in range(n)], **({"nextPageToken": token} if token else {})})

    a, _ = _adapter(antworten, monkeypatch, max_treffer_je_zelle=60, min_zelle_km=1.0)
    a.search_ort(Ort("Kiel", "24103"))
    assert a.zellen == 5  # Startzelle voll (60) → 4 Viertel


def test_zugang_verweigert_und_budget(monkeypatch):
    a, _ = _adapter(lambda body: httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}}), monkeypatch)
    with pytest.raises(ZugangVerweigert):
        a.search_ort(Ort("Kiel", "24103"))
    b, _ = _adapter(lambda body: httpx.Response(200, json={"places": []}), monkeypatch)
    b.max_anfragen = 1
    with pytest.raises(BudgetErschoepft):
        b.search_ort(Ort("Kiel", "24103"))


def test_ohne_key_nicht_einsatzbereit(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    assert "GOOGLE_PLACES_API_KEY" in PlacesAdapter(PlacesConfig()).nicht_konfiguriert_grund()


def test_fill_ausbeute_neu_und_bekannt(storage, monkeypatch):
    """„X Treffer, davon Y neu, Z bereits bekannt": Abgleich über Telefon und Adresse."""
    from recherche.models import Kandidat

    storage.add_kandidat(Kandidat(bundesland="SH", domain="bekannt.example", telefon="+49 431 999999"))
    storage.add_kandidat(Kandidat(bundesland="SH", domain="adresse.example", strasse="Wall 5", plz="24103",
                                  name="Steuerkanzlei 2"))
    places = [_place(1, telefon="0431 999999"), _place(2, telefon="0431 222", strasse=("Wall", "5")),
              _place(3, telefon="0431 333", strasse=("Ring", "7"))]
    a, _ = _adapter(lambda body: httpx.Response(200, json={"places": places}), monkeypatch, cache=storage)
    stat = run_search(storage, a, [Ort("Kiel", "24103")], [], set(), "SH", "l1", 60)
    assert (stat.treffer, stat.neue_kandidaten, stat.bekannt) == (3, 1, 2)
    (fund,) = [f for f in storage.funde("SH") if f.quell_id == "place-3"]
    assert (fund.quelle, fund.telefon, fund.strasse) == ("places", "0431 333", "Ring 7") and fund.gefunden_am
    # Wiederaufnahme: aus dem Cache, keine neuen Anfragen
    a2, anfragen2 = _adapter(lambda body: httpx.Response(500), monkeypatch, cache=storage)
    a2.search_ort(Ort("Kiel", "24103"))
    assert anfragen2 == [] and a2.anfragen == 0
