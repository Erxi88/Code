"""Amtliches Verzeichnis: Detailseite (Vorgabe 05.11.2026) – Parser, Abruf mit eigenem Abstand, Vorrang „amtlich",
weitere Beratungsstellen als Standorte, Vertretungsberechtigte als Ansprechpartner."""

import json
import random
from pathlib import Path

import httpx
import pytest

from recherche.config import VerzeichnisConfig
from recherche.extract.base import LlmErgebnis, LlmPerson
from recherche.extract.merge import zusammenfuehren
from recherche.models import SchrittStatus
from recherche.normalize import PlzPruefung
from recherche.search.pipeline import Ort
from recherche.search.verzeichnisse import (
    StbvDetail,
    StbVerzeichnisAdapter,
    stbv_detail,
)
from recherche.stbv_lauf import (
    AMTLICH,
    offene_details,
    plz_orte,
    run_details,
    run_verzeichnis,
)

FIX = Path(__file__).parent / "fixtures" / "verzeichnisse"
STBV_URL = "https://steuerberaterverzeichnis.berufs-org.de/?lang=de"
SH = "Schleswig-Holstein"


def test_detailseite_beispiel():
    d = stbv_detail((FIX / "beispiele" / "stbv_detail_G1.html").read_text(encoding="utf-8"), "https://x/details/G1/")
    assert (d.name, d.rechtsform, d.strasse, d.plz, d.ort) == (
        "Hansen & Petersen Steuerberatungsgesellschaft mbH", "GmbH", "Holstenstraße 42", "24103", "Kiel")
    assert (d.telefon, d.email, d.internet, d.safe_id) == (
        "0431 900000", "info@hansen-petersen.example", "http://www.hansen-petersen.example", "DE.BStBK.beispiel.1")
    assert d.kammer == "Steuerberaterkammer Schleswig-Holstein"
    assert d.beratungsstellen == [("Markt 5", "24340", "Eckernförde")]
    assert d.geschaeftsfuehrung == ["Dipl.-Kaufmann Jan Hansen", "Bachelor of Arts Maren Petersen"]
    assert d.gesellschafter == ["Dipl.-Kaufmann Jan Hansen"]
    assert StbvDetail.aus_dict(json.loads(json.dumps(d.als_dict()))) == d
    assert stbv_detail("<html><body>leer</body></html>", "u") is None


@pytest.mark.skipif(not (FIX / "echt" / "stbv_detail.html").exists(), reason="echte Detailseite nur lokal")
def test_detailseite_echt():
    d = stbv_detail((FIX / "echt" / "stbv_detail.html").read_text(encoding="utf-8"), "u")
    assert d.name and d.rechtsform and d.plz and d.telefon and d.email and d.internet and d.safe_id
    assert d.geschaeftsfuehrung and d.kammer.startswith("Steuerberaterkammer")


def _adapter(storage, abrufe):
    such = (FIX / "beispiele" / "stbv_24103.html").read_text(encoding="utf-8")
    detail = (FIX / "beispiele" / "stbv_detail_G1.html").read_text(encoding="utf-8")

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        abrufe.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(200, text=such)
        return httpx.Response(200, text=detail) if req.url.path == "/details/G1/" else httpx.Response(404)

    cfg = VerzeichnisConfig(url_vorlage=STBV_URL, formular={"nachnameOrFirmenname": "", "plzFilter": "{plz}"},
                            pause_s=30, pause_jitter_s=5, detail_pause_s=5, detail_pause_jitter_s=5, je_plz=True,
                            max_seiten=1)
    pausen = []
    a = StbVerzeichnisAdapter(cfg, cache=storage, client=httpx.Client(transport=httpx.MockTransport(handler)),
                              sleep=pausen.append, rng=random.Random(1))
    return a, pausen


def test_detail_lauf_amtlich_beratungsstelle_und_fortsetzbar(storage):
    abrufe = []
    a, pausen = _adapter(storage, abrufe)
    run_verzeichnis(storage, a, plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")]), "SH", set(), "l1")
    offen = offene_details(storage, "SH")
    assert len(offen) == 4  # je Kandidat (Gesellschaft, Firmenzeile, Person) eine Detailseite
    stat = run_details(storage, a, "SH", set(), "l1")
    assert (stat.gelesen, stat.mit_internet, stat.mit_telefon, stat.beratungsstellen, stat.leer) == (4, 1, 1, 1, 3)
    assert all(5 <= p <= 10 for p in pausen[1:])  # Detailseiten: eigener, kürzerer Abstand (5–10 s)
    hp = storage.kandidat_nach_domain("hansen-petersen.example")
    assert hp and hp.telefon == "+49 431 900000" and hp.crawl_status == SchrittStatus.OFFEN
    amtlich = next(f for f in storage.funde_kandidat(hp.id) if f.quelle == AMTLICH)
    assert json.loads(amtlich.snippet)["email"] == "info@hansen-petersen.example"
    bs = next(k for k in storage.kandidaten("SH") if k.ort == "Eckernförde")
    fund = next(f for f in storage.funde_kandidat(bs.id) if f.quelle == AMTLICH)
    assert fund.telefon == "+49 431 900000" and json.loads(fund.snippet)["beratungsstelle_von"].startswith("Hansen")
    # fortsetzbar: alles gelesen, nichts offen, kein neuer Abruf
    vorher = len(abrufe)
    assert offene_details(storage, "SH") == [] and run_details(storage, a, "SH", set(), "l2").gelesen == 0
    assert len(abrufe) == vorher


def test_amtlich_vor_website(storage):
    """Name, Rechtsform, Adresse, Telefon, E-Mail und Ansprechpartner aus dem Verzeichnis; die Website ergänzt nur
    Teamgröße, Durchwahl und E-Mail der Personen; Berufsträger aus den zugeordneten Personen."""
    abrufe = []
    a, _ = _adapter(storage, abrufe)
    run_verzeichnis(storage, a, plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")]), "SH", set(), "l1")
    run_details(storage, a, "SH", set(), "l1")
    hp = storage.kandidat_nach_domain("hansen-petersen.example")
    funde = storage.funde_kandidat(hp.id)
    llm = LlmErgebnis(kanzleiname="H&P Steuerberatung", email="kontakt@web.example", telefon="0431 111111",
                      personen=[LlmPerson(name="Ole Web", funktion="Steuerberater"),
                                LlmPerson(name="Jan Hansen", funktion="Geschäftsführer")], mitarbeiterzahl=12)
    k = zusammenfuehren(hp, funde, {}, None, llm, lambda p, o: PlzPruefung(True, SH, ort_kanonisch=o))
    assert k.kanzleiname == "Hansen & Petersen Steuerberatungsgesellschaft mbH" and k.rechtsform == "GmbH"
    assert (k.strasse, k.plz, k.telefon, k.email) == ("Holstenstraße 42", "24103", "+49 431 900000",
                                                      "info@hansen-petersen.example")
    assert [p.name for p in k.ansprechpartner] == ["Jan Hansen", "Maren Petersen"]
    assert (k.ansprechpartner[0].titel, k.ansprechpartner[1].titel) == ("Dipl.-Kaufmann", "Bachelor of Arts")
    assert k.berufstraeger_anzahl == 2 and k.teamgroesse == 12  # Teamgröße von der Website


def test_beratungsstelle_wird_standortzeile(storage):
    abrufe = []
    a, _ = _adapter(storage, abrufe)
    run_verzeichnis(storage, a, plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")]), "SH", set(), "l1")
    run_details(storage, a, "SH", set(), "l1")
    bs = next(k for k in storage.kandidaten("SH") if k.ort == "Eckernförde")
    k = zusammenfuehren(bs, storage.funde_kandidat(bs.id), {}, None, None,
                        lambda p, o: PlzPruefung(True, SH, ort_kanonisch=o))
    assert (k.kanzleiname, k.strasse, k.plz, k.telefon) == (
        "Hansen & Petersen Steuerberatungsgesellschaft mbH", "Markt 5", "24340", "+49 431 900000")
    assert k.hauptansprechpartner.name == "Jan Hansen"
    amtlich = next(f for f in storage.funde_kandidat(bs.id) if f.quelle == AMTLICH)
    assert json.loads(amtlich.snippet)["beratungsstelle_von"] == "Hansen & Petersen Steuerberatungsgesellschaft mbH"



def test_beratungsstelle_mit_zentrale_verknuepft(storage):
    """Export: die Beratungsstelle steht als eigene Zeile mit Verweis auf die Zentrale (gleiches Telefon und
    Hauptansprechpartner – Gruppe in „Zentrale/Standort")."""
    from recherche.extract.standorte import (
        _schluessel,
        standort_spalte,
        zentrale_spalte,
    )

    abrufe = []
    a, _ = _adapter(storage, abrufe)
    run_verzeichnis(storage, a, plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")]), "SH", set(), "l1")
    run_details(storage, a, "SH", set(), "l1")
    zeilen = [zusammenfuehren(k, storage.funde_kandidat(k.id), {}, None, None,
                              lambda p, o: PlzPruefung(True, SH, ort_kanonisch=o)) for k in storage.kandidaten("SH")]
    zentrale = next(z for z in zeilen if z.domain == "hansen-petersen.example")
    bs = next(z for z in zeilen if z.ort == "Eckernförde")
    spalte = standort_spalte(zeilen)
    assert spalte[_schluessel(zentrale)].startswith("Zentrale") and "Standort von" in spalte[_schluessel(bs)]
    assert zentrale_spalte(zeilen)[_schluessel(bs)] == "hansen-petersen.example"


def test_detailseiten_per_robots_gesperrt_eine_zeile(storage):
    """HH-Lauf 05.11.2026: /details/ per robots.txt gesperrt – eine Zeile, keine Fehlerzeile je Kandidat, kein Abruf."""
    from recherche.stbv_lauf import ROBOTS_GESPERRT

    such = (FIX / "beispiele" / "stbv_24103.html").read_text(encoding="utf-8")
    abrufe = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /details/\n")
        abrufe.append(req.url.path)
        return httpx.Response(200, text=such)

    cfg = VerzeichnisConfig(url_vorlage=STBV_URL, formular={"nachnameOrFirmenname": "", "plzFilter": "{plz}"},
                            pause_s=0, pause_jitter_s=0, je_plz=True, max_seiten=1)
    a = StbVerzeichnisAdapter(cfg, cache=storage, client=httpx.Client(transport=httpx.MockTransport(handler)),
                              sleep=lambda s: None)
    run_verzeichnis(storage, a, plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")]), "SH", set(), "l1")
    meldungen = []
    stat = run_details(storage, a, "SH", set(), "l1", fortschritt=meldungen.append)
    assert stat.robots_gesperrt and stat.gelesen == 0 and stat.fehler == 0
    assert meldungen == [ROBOTS_GESPERRT] and not any("/details/" in x for x in abrufe)
    assert len(offene_details(storage, "SH")) == 4  # bleibt offen – läuft, sobald robots.txt es erlaubt
