import pytest

from recherche.search.base import SearchHit
from recherche.search.places import Rechteck, raster_suche, rechteck_um, zerlege

KIEL = rechteck_um(54.3233, 10.1228, 6.0)  # 12 x 12 km um Kiel


def test_rechteck_um():
    assert KIEL.hoehe_km() == pytest.approx(12.0, rel=1e-3)
    assert KIEL.breite_km() == pytest.approx(12.0, rel=1e-3)


def test_zerlegung_lueckenlos():
    zellen = zerlege(KIEL, 5.0)
    assert len(zellen) == 9  # 3 x 3 Zellen à 4 km
    assert all(z.hoehe_km() <= 5.0 + 1e-9 and z.breite_km() <= 5.0 + 1e-6 for z in zellen)
    # Kanten fallen zusammen, Gesamtfläche = Gebiet
    assert min(z.sued for z in zellen) == KIEL.sued and max(z.nord for z in zellen) == KIEL.nord
    assert min(z.west for z in zellen) == KIEL.west and max(z.ost for z in zellen) == KIEL.ost
    flaeche = sum((z.nord - z.sued) * (z.ost - z.west) for z in zellen)
    assert flaeche == pytest.approx((KIEL.nord - KIEL.sued) * (KIEL.ost - KIEL.west))
    nachbarn = sorted({round(z.west, 12) for z in zellen})
    assert len(nachbarn) == 3


def test_viertel():
    teile = KIEL.viertel()
    assert len(teile) == 4
    assert sum(t.hoehe_km() * t.breite_km() for t in teile) == pytest.approx(KIEL.hoehe_km() * KIEL.breite_km(), rel=1e-3)


def test_location_restriction():
    r = Rechteck(54.0, 10.0, 54.1, 10.2).als_location_restriction()
    assert r == {"rectangle": {"low": {"latitude": 54.0, "longitude": 10.0},
                               "high": {"latitude": 54.1, "longitude": 10.2}}}


def test_volle_zellen_werden_verfeinert():
    """Fake-Abfrage: Zellen mit Mittelpunkt nördlich von 54.33 sind 'voll' (60 Treffer)."""
    aufrufe: list[Rechteck] = []

    def abfrage(z: Rechteck) -> list[SearchHit]:
        aufrufe.append(z)
        n = 60 if z.mitte_breite > 54.33 and z.hoehe_km() > 1.0 else 3
        key = f"{z.sued:.5f},{z.west:.5f}"
        return [SearchHit(quelle="places", quell_id=f"{key}-{i}") for i in range(n)]

    hits = raster_suche(KIEL, abfrage, zelle_km=5.0, max_treffer_je_zelle=60, min_zelle_km=0.5)
    assert len(aufrufe) > 9  # Startraster + Verfeinerung
    assert min(z.hoehe_km() for z in aufrufe) >= 0.5
    assert len(hits) == len({h.quell_id for h in hits})


def test_doppelte_treffer_aus_ueberlappung_einmal():
    def abfrage(z: Rechteck) -> list[SearchHit]:
        return [SearchHit(quelle="places", quell_id="gleich")]

    assert len(raster_suche(KIEL, abfrage, 5.0, 60, 0.5)) == 1
