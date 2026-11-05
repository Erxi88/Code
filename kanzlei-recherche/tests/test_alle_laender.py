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


def test_land_mit_frischer_fremder_sperre_wird_uebersprungen(monkeypatch, db_pfad):
    """Ein anderer Lauf bearbeitet HH gerade (Lebenszeichen jung) – die Warteschlange überspringt HH; eine verwaiste
    Sperre (älter als 10 min) gilt nicht."""
    import time

    from recherche.stbv_lauf import SPERRE, SPERRE_VERWAIST_S, fremde_sperre

    FakeVerzeichnis.abgefragt = []
    monkeypatch.setattr(cli, "build_adapter", lambda name, s, cache=None: FakeVerzeichnis())
    monkeypatch.setattr(cli.Kontext, "orte", lambda self, auswahl, mit_zusatz=True: ORTE[self.bundesland])
    meldungen = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: meldungen.append(" ".join(map(str, a))))
    with Storage(db_pfad) as st:
        st.cache_put(SPERRE, "HH", {"kennung": "anderer-rechner:1:1", "seit": time.time(), "zuletzt": time.time()})
        st.cache_put(SPERRE, "SH", {"kennung": "abgebrochen:2:2", "seit": 0, "zuletzt": time.time() - SPERRE_VERWAIST_S - 5})
    cli._alle_laender(None, db_pfad, 2, ohne_nachsuche=True, ohne_details=True, nur_verzeichnis=True, mit_export=False,
                      laender=["SH", "HH"])
    assert FakeVerzeichnis.abgefragt == ["24103"]  # SH (verwaiste Sperre) ja, HH nein
    assert any("HH: läuft in einem anderen Lauf" in m for m in meldungen)
    with Storage(db_pfad) as st:
        assert st.cache_get(SPERRE, "SH") is None  # nach dem Lauf freigegeben
        assert fremde_sperre(st, "HH", "ich:0:0") is not None


def test_detailseiten_per_robots_gesperrt_werden_spaeter_nachgeholt(monkeypatch, db_pfad):
    """Vorgabe 05.11.2026: Detailschritt auslassen, solange robots.txt ihn sperrt – aber nicht als erledigt merken.
    Der nächste Aufruf prüft erneut (Suchseiten aus dem Protokoll, keine neue Abfrage); erlaubt robots.txt die Seiten
    dann, werden sie gelesen und Nachsuche/crawl/extract/Export des Landes neu angestoßen."""
    from recherche import stbv_lauf

    erlaubt = [False]

    class MitDetails(FakeVerzeichnis):
        def search_ort(self, ort):
            with self.sperre:
                self.abgefragt.append(ort.plz)
            return [SearchHit(quelle=QUELLE, quell_id="stbv-1", name="Steuerkanzlei Kiel", strasse="Markt 1",
                              plz=ort.plz, ort=ort.name, roh_url="https://stbv.example/details/1",
                              snippet="Amtliches Steuerberaterverzeichnis: 1 Berufsträger (x)")]

        def detail_erlaubt(self, url):
            return erlaubt[0]

    MitDetails.abgefragt = []
    monkeypatch.setattr(cli, "build_adapter", lambda name, s, cache=None: MitDetails())
    monkeypatch.setattr(cli.Kontext, "orte", lambda self, auswahl, mit_zusatz=True: ORTE[self.bundesland])
    meldungen = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: meldungen.append(" ".join(map(str, a))))
    lauf = lambda: cli._alle_laender(None, db_pfad, 1, ohne_nachsuche=True, ohne_details=False,  # noqa: E731
                                     nur_verzeichnis=True, mit_export=False, laender=["SH"])
    lauf()
    assert any(stbv_lauf.ROBOTS_GESPERRT in m for m in meldungen) and MitDetails.abgefragt == ["24103"]
    with Storage(db_pfad) as st:
        assert st.cache_get(cli.DETAIL_MERKER, "SH") == {"gesperrt": True}
        st.cache_put(cli.LAND_MERKER, "SH", {"netz": True, "nach": True, "export": True})

    meldungen.clear()
    lauf()  # weiter gesperrt: erneut geprüft, keine neue Suchanfrage
    assert MitDetails.abgefragt == ["24103"] and any(stbv_lauf.ROBOTS_GESPERRT in m for m in meldungen)

    erlaubt[0] = True
    gelesen = []
    monkeypatch.setattr(stbv_lauf, "run_details",
                        lambda *a, **k: gelesen.append(1) or stbv_lauf.DetailStatistik(gelesen=1))
    echt = stbv_lauf.offene_details
    monkeypatch.setattr(stbv_lauf, "offene_details", lambda *a, **k: [] if gelesen else echt(*a, **k))
    lauf()
    assert gelesen == [1]
    with Storage(db_pfad) as st:
        assert st.cache_get(cli.DETAIL_MERKER, "SH") == {"gesperrt": False}
        assert st.cache_get(cli.LAND_MERKER, "SH") == {"netz": True}  # nach/export werden neu angestoßen
    gelesen.clear()
    lauf()  # jetzt erledigt – kein weiterer Netz-Teil
    assert gelesen == []


def test_aeltere_gruppierung_erst_neu_gruppieren_dann_nachsuche(monkeypatch, db_pfad):
    """Vorgabe 05.11.2026: nach neuen Gruppierungsregeln erst aus dem Cache neu gruppieren, Nachsuche danach. Ein Land
    mit amtlichen Funden einer älteren Gruppierung läuft in der Warteschlange wieder durch den Netz-Teil (der neu
    gruppiert), Nachsuche/crawl/extract/Export werden neu angestoßen; der Einzelschritt „nach" verweigert vorher."""
    FakeVerzeichnis.abgefragt = []
    monkeypatch.setattr(cli, "build_adapter", lambda name, s, cache=None: FakeVerzeichnis())
    monkeypatch.setattr(cli.Kontext, "orte", lambda self, auswahl, mit_zusatz=True: ORTE[self.bundesland])
    meldungen = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: meldungen.append(" ".join(map(str, a))))
    lauf = lambda: cli._alle_laender(None, db_pfad, 1, ohne_nachsuche=True, ohne_details=True,  # noqa: E731
                                     nur_verzeichnis=True, mit_export=False, laender=["SH"])
    lauf()
    with Storage(db_pfad) as st:
        st.cache_put("stbv_gruppierung", "SH", {"version": "alt"})
        st.cache_put(cli.LAND_MERKER, "SH", {"netz": True, "nach": True, "export": True})

    meldungen.clear()
    cli.stb_verzeichnis(bundesland="SH", db=db_pfad, config=None, ja=True, ohne_nachsuche=False, nur_verzeichnis=True,
                        out=None, kreis=None, ohne_details=True, neu_gruppieren=False, alle_laender=False, parallel=1,
                        schritte="nach")
    assert any("älterer Gruppierung" in m for m in meldungen) and not any("Website-Nachsuche" in m for m in meldungen)

    lauf()
    assert any("Neu gruppieren SH" in m for m in meldungen)
    with Storage(db_pfad) as st:
        assert st.cache_get(cli.LAND_MERKER, "SH") == {"netz": True}
        assert {k.name for k in st.kandidaten("SH")} == {"Steuerkanzlei Kiel"}
    assert FakeVerzeichnis.abgefragt == ["24103", "24103"]  # Fake ohne Cache; echt käme die Seite aus dem Cache
