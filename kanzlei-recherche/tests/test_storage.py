import sqlite3

import pytest
from conftest import FIXTURES

from recherche.models import (
    DublettenArt,
    Fund,
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


def test_kandidat_anlegen_und_laden(storage):
    kid = storage.add_kandidat(Kandidat(bundesland="SH", domain="a.example", website="https://a.example/"))
    k = storage.get_kandidat(kid)
    assert (k.id, k.domain, k.crawl_status) == (kid, "a.example", SchrittStatus.OFFEN)
    assert storage.kandidat_nach_domain("a.example").id == kid


def test_kandidaten_ohne_website_erlaubt(storage):
    a = storage.add_kandidat(Kandidat(bundesland="SH", name="Steuerbüro Schütt", plz="24534"))
    b = storage.add_kandidat(Kandidat(bundesland="SH", name="Kanzlei Voß", plz="24306"))
    assert a != b
    assert len(storage.kandidaten("SH")) == 2
    assert storage.kandidaten("SH", nur_mit_website=True) == []


def test_domain_bleibt_eindeutig(storage):
    storage.add_kandidat(Kandidat(bundesland="SH", domain="a.example"))
    with pytest.raises(sqlite3.IntegrityError):
        storage.add_kandidat(Kandidat(bundesland="SH", domain="a.example"))


def test_ergaenzen_fuellt_nur_leere_felder(storage):
    kid = storage.add_kandidat(Kandidat(bundesland="SH", name="Hansen", telefon="+49 431 1"))
    k = storage.ergaenze_kandidat(kid, name="Anderer Name", telefon="+49 431 2", plz="24103",
                                  domain="hansen.example", suchort="egal")
    assert (k.name, k.telefon, k.plz, k.domain) == ("Hansen", "+49 431 1", "24103", "hansen.example")
    assert k.suchort is None  # nicht ergänzbar


def test_ergaenzen_ueberspringt_vergebene_domain(storage):
    storage.add_kandidat(Kandidat(bundesland="SH", domain="a.example"))
    kid = storage.add_kandidat(Kandidat(bundesland="SH", name="B"))
    assert storage.ergaenze_kandidat(kid, domain="a.example").domain is None


def test_kandidat_status_fuer_wiederaufnahme(storage):
    ids = [storage.add_kandidat(Kandidat(bundesland="SH", domain=d)) for d in ("a.example", "b.example", "c.example")]
    storage.set_crawl_status(ids[0], SchrittStatus.FERTIG)
    storage.set_crawl_status(ids[1], SchrittStatus.FEHLER, "Timeout")
    offen = storage.kandidaten("SH", crawl_status=SchrittStatus.OFFEN)
    assert [k.domain for k in offen] == ["c.example"]
    assert storage.get_kandidat(ids[1]).fehler == "Timeout"
    storage.set_extract_status(ids[0], SchrittStatus.FERTIG)
    rest = storage.kandidaten("SH", crawl_status=SchrittStatus.FERTIG, extract_status=SchrittStatus.OFFEN)
    assert rest == []


def test_funde_je_quelle_einmal(storage):
    kid = storage.add_kandidat(Kandidat(bundesland="SH", domain="a.example"))
    f = Fund(kandidat_id=kid, bundesland="SH", quelle="brave", quell_id="https://a.example")
    assert storage.add_fund(f) is True
    assert storage.add_fund(f) is False
    assert storage.add_fund(f.model_copy(update={"quelle": "places"})) is True
    assert storage.hat_fund("SH", "brave", "https://a.example")
    assert not storage.hat_fund("HH", "brave", "https://a.example")
    assert [x.quelle for x in storage.funde("SH")] == ["brave", "places"]
    assert len(storage.funde("SH", quelle="places")) == 1


def test_api_cache(storage):
    assert storage.cache_get("brave", "k") is None
    storage.cache_put("brave", "k", {"web": {"results": [1, 2]}})
    assert storage.cache_get("brave", "k") == {"web": {"results": [1, 2]}}
    assert storage.cache_get("google_cse", "k") is None


def test_kostenlog_summe(storage):
    for adapter, n in (("brave", 3), ("brave", 2), ("ddgs", 4)):
        storage.log_lauf(LaufEintrag(lauf_id="l1", schritt="search", bundesland="SH", adapter=adapter,
                                     anfragen=n, kosten=n * 0.005))
    storage.log_lauf(LaufEintrag(lauf_id="l2", schritt="search", bundesland="SH", adapter="brave", anfragen=10))
    assert storage.anfragen_summe("SH", "l1") == {"brave": (5, 0.025), "ddgs": (4, 0.02)}
    assert storage.anfragen_summe("SH")["brave"][0] == 15


def test_migration_v1_auf_v2(db_pfad):
    con = sqlite3.connect(db_pfad)
    con.executescript((FIXTURES / "migration" / "schema_v1.sql").read_text(encoding="utf-8"))
    con.execute("PRAGMA user_version = 1")
    con.execute(
        "INSERT INTO kandidaten (domain, bundesland, url, ort, suchbegriff, quelle, titel, snippet, gefunden_am)"
        " VALUES ('a.example', 'SH', 'https://a.example/', 'Kiel', 'Steuerberater Kiel', 'ddgs', 'A', 's',"
        " '2026-10-20T09:00:00')"
    )
    con.execute(
        "INSERT INTO dubletten VALUES ('b.example', 'a.example', 'SH', 'telefon', 'A', NULL, '2026-10-20T09:00:00')"
    )
    con.execute(
        "INSERT INTO lauf_protokoll (lauf_id, schritt, bundesland, suchbegriff, adapter, trefferzahl, neue_domains,"
        " laufzeit_s, zeitpunkt) VALUES ('l', 'search', 'SH', 'Steuerberater Kiel', 'ddgs', 5, 3, 1.0,"
        " '2026-10-20T09:00:00')"
    )
    con.commit()
    con.close()
    with Storage(db_pfad) as st:
        assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        k = st.kandidat_nach_domain("a.example")
        assert (k.website, k.suchort, k.name, k.quelle) == ("https://a.example/", "Kiel", "A", "ddgs")
        (f,) = st.funde("SH")
        assert (f.kandidat_id, f.quelle, f.quell_id, f.suchbegriff) == (k.id, "ddgs", "https://a.example/", "Steuerberater Kiel")
        (d,) = st.dubletten("SH")
        assert (d.behalten, d.dublette, d.art) == ("a.example", "b.example", DublettenArt.ZUSAMMENGEFUEHRT)
        (e,) = st.lauf_protokoll("SH")
        assert (e.neue_kandidaten, e.anfragen) == (3, 0)
        assert st.query_erledigt("SH", "ddgs", "Steuerberater Kiel")
    # zweites Öffnen: keine erneute Migration
    with Storage(db_pfad) as st:
        assert len(st.kandidaten("SH")) == 1


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
    assert befuellt.query_erledigt("SH", "brave", "Steuerberater Kiel")
    assert not befuellt.query_erledigt("SH", "brave", "Steuerberatung 24103 Kiel")  # mit Fehler
    assert not befuellt.query_erledigt("SH", "google_cse", "Steuerberater Kiel")
    assert not befuellt.query_erledigt("SH", "brave", "Steuerberater Flensburg")
    assert not befuellt.query_erledigt("SH", "brave", "Steuerberater Kiel", schritt="fill")


def test_lauf_protokoll_reihenfolge(storage):
    for i in range(3):
        storage.log_lauf(LaufEintrag(lauf_id="x", schritt="search", bundesland="SH", suchbegriff=f"q{i}"))
    assert [e.suchbegriff for e in storage.lauf_protokoll("SH")] == ["q0", "q1", "q2"]


def test_migration_v2_bis_aktuell(db_pfad):
    from recherche.storage import _SCHEMA_V2

    con = sqlite3.connect(db_pfad)
    con.executescript(_SCHEMA_V2)
    con.execute("PRAGMA user_version = 2")
    con.execute(
        "INSERT INTO kanzleien (domain, lauf_bundesland, kanzleiname, konfidenz, status) "
        "VALUES ('a.example', 'SH', 'Kanzlei A', 'mittel', 'kontaktiert')"
    )
    con.execute("INSERT INTO seiten VALUES ('https://a.example/', 'a.example', 'start', 200, 'Text', '2026-10-20T09:00:00')")
    con.execute("INSERT INTO nicht_gefunden VALUES ('b.example', 'SH', 'Timeout', NULL, '2026-10-20T09:00:00')")
    con.commit()
    con.close()
    with Storage(db_pfad) as st:
        assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
        k = st.get_kanzlei("a.example")
        assert (k.kanzleiname, k.status, k.website_vorhanden, k.kandidat_id) == ("Kanzlei A", Status.KONTAKTIERT, True, None)
        assert k.adresszusatz is None
        st.upsert_kanzlei(k.model_copy(update={"adresszusatz": "Haus 8 Süd"}))
        assert st.get_kanzlei("a.example").adresszusatz == "Haus 8 Süd"
        s = st.get_seite("https://a.example/")
        assert (s.text, s.html, s.final_url) == ("Text", None, None)
        (ng,) = st.nicht_gefunden("SH")
        assert (ng.grund, ng.name) == ("Timeout", None)


def test_seite_mit_html_komprimiert(storage):
    html = "<html><body>" + "Holstenstraße 42 " * 500 + "</body></html>"
    storage.save_seite(Seite(url="https://a.example/", domain="a.example", html=html, text="t", final_url="https://www.a.example/"))
    s = storage.get_seite("https://a.example/")
    assert s.html == html and s.final_url == "https://www.a.example/"
    roh = storage.conn.execute("SELECT length(html_gz) FROM seiten").fetchone()[0]
    assert roh < len(html.encode()) / 10


def test_kandidat_seiten(storage):
    kid = storage.add_kandidat(Kandidat(bundesland="SH", domain="a.example"))
    for typ, url in ((Seitentyp.START, "https://a.example/"), (Seitentyp.IMPRESSUM, "https://a.example/impressum"),
                     (Seitentyp.KONTAKT, "https://a.example/impressum")):
        storage.save_seite(Seite(url=url, domain="a.example", seitentyp=typ, text=url))
        storage.setze_kandidat_seite(kid, typ, url)
    seiten = storage.kandidat_seiten(kid)
    assert seiten[Seitentyp.KONTAKT].url == seiten[Seitentyp.IMPRESSUM].url == "https://a.example/impressum"
    assert set(seiten) == {Seitentyp.START, Seitentyp.IMPRESSUM, Seitentyp.KONTAKT}


def test_kanzlei_je_kandidat_auch_ohne_domain(storage):
    a = storage.add_kandidat(Kandidat(bundesland="SH", name="Ohne Web A"))
    b = storage.add_kandidat(Kandidat(bundesland="SH", name="Ohne Web B"))
    ida = storage.upsert_kanzlei(Kanzlei(kandidat_id=a, lauf_bundesland="SH", kanzleiname="A", website_vorhanden=False))
    storage.upsert_kanzlei(Kanzlei(kandidat_id=b, lauf_bundesland="SH", kanzleiname="B", website_vorhanden=False))
    assert storage.upsert_kanzlei(Kanzlei(kandidat_id=a, lauf_bundesland="SH", kanzleiname="A2")) == ida
    assert sorted(k.kanzleiname for k in storage.kanzleien("SH")) == ["A2", "B"]


def test_upsert_behaelt_gepflegten_status(storage):
    storage.upsert_kanzlei(Kanzlei(domain="a.example", lauf_bundesland="SH", status=Status.KONTAKTIERT))
    storage.upsert_kanzlei(Kanzlei(domain="a.example", lauf_bundesland="SH", kanzleiname="neu extrahiert"))
    k = storage.get_kanzlei("a.example")
    assert (k.kanzleiname, k.status) == ("neu extrahiert", Status.KONTAKTIERT)
    storage.upsert_kanzlei(k.model_copy(update={"status": Status.UNGUELTIG}))
    assert storage.get_kanzlei("a.example").status == Status.UNGUELTIG


def test_nicht_gefunden_mit_discovery_daten(storage):
    storage.add_nicht_gefunden(NichtGefunden(domain="a.example", bundesland="SH", grund="Kein Impressum gefunden",
                                             name="Kanzlei A", strasse="Wall 5", plz="24103", ort="Kiel", telefon="0431 1"))
    (ng,) = storage.nicht_gefunden("SH")
    assert (ng.name, ng.strasse, ng.plz, ng.telefon) == ("Kanzlei A", "Wall 5", "24103", "0431 1")
    storage.entferne_nicht_gefunden("a.example")
    assert storage.nicht_gefunden("SH") == []
