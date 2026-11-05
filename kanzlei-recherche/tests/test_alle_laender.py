"""stb-verzeichnis --alle-laender: Warteschlange über die Länder (parallel, fortsetzbar), PLZ aus OpenPLZ ohne
orte_<BL>.csv (Vorgabe 05.11.2026)."""

import threading

from recherche import cli
from recherche.config import Settings, VerzeichnisConfig
from recherche.search.base import OrtsAdapter, SearchHit
from recherche.search.pipeline import Ort
from recherche.storage import Storage

QUELLE = "stb_verzeichnis"
ORTE = {"SH": [Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")], "HH": [Ort("Hamburg", "20095", "Hamburg")],
        "HB": [Ort("Bremen", "28195", "Bremen")]}


class FakeVerzeichnis(OrtsAdapter):
    name = QUELLE
    abgefragt: list[str] = []
    sperre = threading.Lock()

    def __init__(self):
        super().__init__(VerzeichnisConfig(pause_s=0, pause_jitter_s=0))

    def search_ort(self, ort):
        if ort.plz == "28195":
            raise RuntimeError("Verbindung weg")  # ein Land scheitert – die anderen laufen weiter
        with self.sperre:
            self.abgefragt.append(ort.plz)
        return [SearchHit(quelle=QUELLE, quell_id=f"stbv-{ort.plz}", name=f"Steuerkanzlei {ort.name}", strasse="Markt 1",
                          plz=ort.plz, ort=ort.name, snippet="Amtliches Steuerberaterverzeichnis: 1 Berufsträger (x)")]


def test_warteschlange_parallel_und_fortsetzbar(monkeypatch, db_pfad):
    FakeVerzeichnis.abgefragt = []
    monkeypatch.setattr(cli, "build_adapter", lambda name, s, cache=None: FakeVerzeichnis())
    monkeypatch.setattr(cli.Kontext, "orte", lambda self, auswahl, mit_zusatz=True: ORTE[self.bundesland])
    meldungen = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: meldungen.append(" ".join(map(str, a))))
    cli._alle_laender(None, db_pfad, 3, ohne_nachsuche=True, ohne_details=True, nur_verzeichnis=True, mit_export=False,
                      laender=["SH", "HH", "HB"])
    assert sorted(FakeVerzeichnis.abgefragt) == ["20095", "24103"]
    with Storage(db_pfad) as st:
        assert {k.name for k in st.kandidaten("SH")} == {"Steuerkanzlei Kiel"}
        assert {k.name for k in st.kandidaten("HH")} == {"Steuerkanzlei Hamburg"}
        assert st.cache_get(cli.LAND_MERKER, "SH") == {"netz": True} and st.cache_get(cli.LAND_MERKER, "HB") is None
    assert any("HB:" in m and "erneuter Aufruf setzt fort" in m for m in meldungen)

    # Neustart: SH und HH sind erledigt – nur HB wird erneut versucht
    FakeVerzeichnis.abgefragt = []
    cli._alle_laender(None, db_pfad, 3, ohne_nachsuche=True, ohne_details=True, nur_verzeichnis=True, mit_export=False,
                      laender=["SH", "HH", "HB"])
    assert FakeVerzeichnis.abgefragt == []


def test_alle_laender_ohne_eintrag_in_settings():
    s = Settings()
    assert s.bundesland("BY").name == "Bayern" and s.bundesland("BY").orte_datei == "config/orte_BY.csv"


def test_plz_aus_openplz_ohne_ortsliste(db_pfad):
    """Für Länder ohne orte_<BL>.csv: Gemeinden mit allen PLZ und Kreis aus OpenPLZ (hier aus dem Cache)."""
    loc = [{"postalCode": "28195", "name": "Bremen", "municipality": {"key": "04011000", "name": "Bremen, Stadt"},
            "district": {"name": "Bremen", "type": "Kreisfreie Stadt"}},
           {"postalCode": "28199", "name": "Bremen", "municipality": {"key": "04011000", "name": "Bremen, Stadt"},
            "district": {"name": "Bremen", "type": "Kreisfreie Stadt"}},
           {"postalCode": "27568", "name": "Bremerhaven", "municipality": {"key": "04012000", "name": "Bremerhaven"},
            "district": {"name": "Bremerhaven", "type": "Kreisfreie Stadt"}}]
    with Storage(db_pfad) as st:
        st.cache_put("openplz_orte", "FederalStates/04/Localities?page=1&pageSize=50", {"daten": loc, "gesamt_seiten": 1})
    orte = cli.orte_aus_openplz(Settings(), "HB", db_pfad)
    assert [(o.name, o.plz, o.plz_weitere) for o in orte] == [("Bremen", "28195", ("28199",)),
                                                              ("Bremerhaven", "27568", ())]
    assert all(o.kreis for o in orte)
