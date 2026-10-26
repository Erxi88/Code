import sqlite3

import pytest

from recherche.models import (
    Kandidat,
    Kanzlei,
    LaufEintrag,
    NichtGefunden,
    SchrittStatus,
    Seite,
    Seitentyp,
    Status,
)  # fmt: skip
from recherche.storage import SCHEMA_VERSION, Storage


def test_schema_version(db_pfad):
    with Storage(db_pfad) as st:
        assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_neuere_schemaversion_wird_abgelehnt(db_pfad):
    Storage(db_pfad).close()
    con = sqlite3.connect(db_pfad)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    con.close()
    with pytest.raises(RuntimeError):
        Storage(db_pfad)


def test_kanzleien_roundtrip(storage, kanzleien):
    storage.import_kanzleien(kanzleien)
    geladen = {k.domain: k for k in storage.kanzleien("SH")}
    assert len(geladen) == 20
    for k in kanzleien:
        g = geladen[k.domain]
        assert g.id is not None
        assert g.model_dump(exclude={"id"}) == k.model_dump(exclude={"id"})


def test_kanzlei_nullfelder_bleiben_none(storage, kanzleien):
    storage.import_kanzleien(kanzleien)
    k = storage.get_kanzlei("stb-oldesloe.example")
    assert k.kanzleiname is None and k.strasse is None and k.email is None
    assert k.ansprechpartner == []


def test_upsert_aktualisiert_statt_dublette(storage, kanzleien):
    k = kanzleien[0]
    id1 = storage.upsert_kanzlei(k)
    geaendert = k.model_copy(update={"status": Status.KONTAKTIERT, "telefon": "+49 431 1"})
    id2 = storage.upsert_kanzlei(geaendert)
    assert id1 == id2
    alle = storage.kanzleien("SH")
    assert len(alle) == 1
    assert alle[0].status == Status.KONTAKTIERT
    assert alle[0].telefon == "+49 431 1"


def test_kanzleien_nach_bundesland_getrennt(storage):
    storage.upsert_kanzlei(Kanzlei(domain="a.example", lauf_bundesland="SH"))
    storage.upsert_kanzlei(Kanzlei(domain="b.example", lauf_bundesland="HH"))
    assert [k.domain for k in storage.kanzleien("SH")] == ["a.example"]
    assert [k.domain for k in storage.kanzleien("HH")] == ["b.example"]


def test_daten_ueberleben_neues_oeffnen(db_pfad, kanzleien):
    with Storage(db_pfad) as st:
        st.import_kanzleien(kanzleien)
    with Storage(db_pfad) as st:
        assert len(st.kanzleien("SH")) == 20


def test_kandidat_jede_domain_einmal(storage):
    k = Kandidat(domain="a.example", bundesland="SH", url="https://a.example/")
    assert storage.add_kandidat(k) is True
    assert storage.add_kandidat(k.model_copy(update={"url": "https://a.example/x"})) is False
    assert storage.get_kandidat("a.example").url == "https://a.example/"


def test_kandidat_status_fuer_wiederaufnahme(storage):
    for d in ("a.example", "b.example", "c.example"):
        storage.add_kandidat(Kandidat(domain=d, bundesland="SH", url=f"https://{d}/"))
    storage.set_crawl_status("a.example", SchrittStatus.FERTIG)
    storage.set_crawl_status("b.example", SchrittStatus.FEHLER, "Timeout")
    offen = storage.kandidaten("SH", crawl_status=SchrittStatus.OFFEN)
    assert [k.domain for k in offen] == ["c.example"]
    assert storage.get_kandidat("b.example").fehler == "Timeout"
    storage.set_extract_status("a.example", SchrittStatus.FERTIG)
    rest = storage.kandidaten("SH", crawl_status=SchrittStatus.FERTIG, extract_status=SchrittStatus.OFFEN)
    assert rest == []


def test_seiten_cache_nie_zweimal(storage):
    s = Seite(url="https://a.example/impressum", domain="a.example",
              seitentyp=Seitentyp.IMPRESSUM, http_status=200, text="Impressum A")
    assert not storage.hat_seite(s.url)
    assert storage.save_seite(s) is True
    assert storage.hat_seite(s.url)
    # zweites Speichern überschreibt nicht
    assert storage.save_seite(s.model_copy(update={"text": "neu"})) is False
    assert storage.get_seite(s.url).text == "Impressum A"
    assert storage.get_seite(s.url).seitentyp == Seitentyp.IMPRESSUM
    assert [x.url for x in storage.seiten("a.example")] == [s.url]


def test_nicht_gefunden_ein_eintrag_je_domain(storage):
    storage.add_nicht_gefunden(NichtGefunden(domain="a.example", bundesland="SH", grund="Timeout"))
    storage.add_nicht_gefunden(NichtGefunden(domain="a.example", bundesland="SH", grund="kein Impressum"))
    eintraege = storage.nicht_gefunden("SH")
    assert len(eintraege) == 1 and eintraege[0].grund == "kein Impressum"


def test_fixture_import_komplett(befuellt):
    assert len(befuellt.kanzleien("SH")) == 20
    assert len(befuellt.nicht_gefunden("SH")) == 3
    assert {d.kriterium for d in befuellt.dubletten("SH")} == {"telefon", "plz+name"}
    assert len(befuellt.lauf_protokoll("SH")) == 4


def test_query_erledigt_nur_ohne_fehler(befuellt):
    assert befuellt.query_erledigt("SH", "ddgs", "Steuerberater Kiel")
    assert not befuellt.query_erledigt("SH", "ddgs", "Steuerberatung 24103 Kiel")  # mit Fehler
    assert not befuellt.query_erledigt("SH", "google", "Steuerberater Kiel")
    assert not befuellt.query_erledigt("SH", "ddgs", "Steuerberater Flensburg")


def test_lauf_protokoll_reihenfolge(storage):
    for i in range(3):
        storage.log_lauf(LaufEintrag(lauf_id="x", schritt="search", bundesland="SH", suchbegriff=f"q{i}"))
    assert [e.suchbegriff for e in storage.lauf_protokoll("SH")] == ["q0", "q1", "q2"]
