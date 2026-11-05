"""`recherche stb-verzeichnis`: amtliches Verzeichnis PLZ für PLZ, Abgleich, neue Einträge direkt, Lückenliste."""

from pathlib import Path

from openpyxl import load_workbook

from recherche.config import VerzeichnisConfig
from recherche.export import BLATT_LUECKEN, export_excel
from recherche.models import Kandidat, Kanzlei, Konfidenz
from recherche.search.base import LimitErreicht, OrtsAdapter, SearchHit
from recherche.search.pipeline import Ort
from recherche.stbv_lauf import (
    QUELLE,
    kreise,
    luecken,
    luecken_csv,
    offene_plz,
    plz_orte,
    run_verzeichnis,
)

ORTE = [Ort("Kiel", "24103", "Kreisfreie Stadt Kiel", plz_weitere=("24105",)),
        Ort("Kiel", "24103", None, gemeinde="Kiel"),  # Zusatz-Suchort: zählt nicht doppelt
        Ort("Kronshagen", "24119", "Kreis Rendsburg-Eckernförde")]


class FakeVerzeichnis(OrtsAdapter):
    name = QUELLE

    def __init__(self, treffer: dict[str, list[SearchHit]], abbruch_bei: str | None = None):
        super().__init__(VerzeichnisConfig())
        self.treffer, self.abbruch_bei, self.abgefragt = treffer, abbruch_bei, []

    def search_ort(self, ort):
        if ort.plz == self.abbruch_bei:
            raise LimitErreicht("gesperrt")
        self.abgefragt.append(ort.plz)
        self.anfragen += 1
        return self.treffer.get(ort.plz, [])


def _hit(i, name, strasse, plz, ort, telefon=None):
    return SearchHit(quelle=QUELLE, quell_id=f"stbv-{i}", name=name, strasse=strasse, plz=plz, ort=ort, telefon=telefon,
                     snippet=f"Berufsträger: {name}")


def test_plz_orte_je_plz_einmal():
    assert [(o.plz, o.name, o.kreis) for o in plz_orte(ORTE)] == [
        ("24103", "Kiel", "Kreisfreie Stadt Kiel"), ("24105", "Kiel", "Kreisfreie Stadt Kiel"),
        ("24119", "Kronshagen", "Kreis Rendsburg-Eckernförde")]


def test_abgleich_neue_direkt_wiederaufnahme_und_lueckenliste(storage, tmp_path):
    # Bestand: Kanzlei Lund (Telefon gleich) – das amtliche Verzeichnis ergänzt, legt nicht neu an
    storage.add_kandidat(Kandidat(bundesland="SH", domain="lund.example", website="https://lund.example/",
                                  name="Steuerkanzlei Lund", plz="24103", ort="Kiel", telefon="+49 431 900000"))
    treffer = {"24103": [_hit(1, "Steuerkanzlei Lund", "Markt 1", "24103", "Kiel", "+49 431 900000"),
                         _hit(2, "Steuerberaterin Ina Ott", "Holstenstraße 5", "24103", "Kiel")],
               "24119": [_hit(3, "Nordlicht Steuerberatungsgesellschaft mbH", "Eckernförder Str. 1", "24119", "Kronshagen")]}
    orte = plz_orte(ORTE)
    adapter = FakeVerzeichnis(treffer, abbruch_bei="24119")
    stat = run_verzeichnis(storage, adapter, orte, "SH", set(), "lauf-1")
    assert (stat.neu, stat.bekannt, stat.abbruch) == (1, 1, "gesperrt")
    assert [o.plz for o in offene_plz(storage, "SH", orte)] == ["24119"]  # 24103, 24105 erledigt

    # Wiederaufnahme: nur die offene PLZ
    adapter = FakeVerzeichnis(treffer)
    stat = run_verzeichnis(storage, adapter, orte, "SH", set(), "lauf-2")
    assert adapter.abgefragt == ["24119"] and (stat.neu, stat.uebersprungen) == (1, 2)

    zeilen = luecken(storage, "SH", kreise(ORTE))
    assert [(z.name, z.plz, z.kreis, z.stand) for z in zeilen] == [
        ("Steuerberaterin Ina Ott", "24103", "Kreisfreie Stadt Kiel", "ohne Website (amtliche Anschrift)"),
        ("Nordlicht Steuerberatungsgesellschaft mbH", "24119", "Kreis Rendsburg-Eckernförde",
         "ohne Website (amtliche Anschrift)")]
    assert zeilen[0].berufstraeger == "Berufsträger: Steuerberaterin Ina Ott"
    text = luecken_csv(zeilen, tmp_path / "luecken.csv").read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("Kandidat;Name (amtlich);Berufsträger (amtlich)") and "Ina Ott" in text

    # nach crawl/extract: Stand und Kanzleiname aus der Zeile
    kid = zeilen[1].kandidat_id
    storage.upsert_kanzlei(Kanzlei(kandidat_id=kid, lauf_bundesland="SH", kanzleiname="Nordlicht StBG mbH",
                                   plz="24119", ort="Kronshagen", bundesland="Schleswig-Holstein",
                                   plz_verifiziert=True, konfidenz=Konfidenz.HOCH))
    z = next(z for z in luecken(storage, "SH") if z.kandidat_id == kid)
    assert z.stand == "extrahiert (neu)" and z.kanzleiname == "Nordlicht StBG mbH" and z.konfidenz == "hoch"

    # Blatt „Lücken (amtlich)" im Export des Landes (auch im Lieferformat)
    pfad = export_excel(storage, "SH", tmp_path / "sh.xlsx", "Schleswig-Holstein", kontrollblaetter=False,
                        kreis_je_plz=kreise(ORTE))
    ws = load_workbook(pfad)[BLATT_LUECKEN]
    assert ws.max_row == 3 and ws.cell(row=2, column=2).value == "Steuerberaterin Ina Ott"


def test_kein_luecken_blatt_ohne_amtliche_eintraege(storage, tmp_path):
    pfad = export_excel(storage, "SH", tmp_path / "sh.xlsx", "Schleswig-Holstein", kontrollblaetter=False)
    assert BLATT_LUECKEN not in load_workbook(pfad).sheetnames


def test_cli_stb_verzeichnis(monkeypatch, db_pfad, tmp_path):
    from typer.testing import CliRunner

    from recherche import cli

    fake = FakeVerzeichnis({"24119": [_hit(9, "Steuerberater Ole Rau", "Dorfstraße 1", "24119", "Kronshagen")]})
    monkeypatch.setattr(cli, "build_adapter", lambda name, s, cache=None: fake)
    monkeypatch.setattr(cli.Kontext, "orte", lambda self, auswahl, mit_zusatz=True: ORTE)
    r = CliRunner().invoke(cli.app, ["stb-verzeichnis", "-b", "SH", "--db", str(db_pfad), "--ja", "--ohne-nachsuche",
                                     "--nur-verzeichnis", "--out", str(tmp_path / "l.csv")])
    assert r.exit_code == 0, r.output
    text = " ".join(r.output.split())
    assert "3 PLZ, davon 3 offen" in text and "1 neu (direkt im Bestand)" in text
    assert "Lückenliste: 1 Kanzleien nur im amtlichen Verzeichnis" in text
    assert "Ole Rau" in (tmp_path / "l.csv").read_text(encoding="utf-8-sig")
    # erneuter Aufruf: nichts offen, nichts abgefragt
    fake.abgefragt.clear()
    r = CliRunner().invoke(cli.app, ["stb-verzeichnis", "-b", "SH", "--db", str(db_pfad), "--ja", "--ohne-nachsuche",
                                     "--nur-verzeichnis", "--out", str(tmp_path / "l.csv")])
    assert r.exit_code == 0 and "davon 0 offen" in " ".join(r.output.split()) and fake.abgefragt == []


# ---------------------------------------------------------------------- Pilot (Vorgabe 05.11.2026)

BEISPIEL_STBV = Path(__file__).parent / "fixtures" / "verzeichnisse" / "beispiele" / "stbv_24103.html"
STBV_URL = "https://steuerberaterverzeichnis.berufs-org.de/?lang=de"


def _stbv_adapter(storage, html_je_plz):
    import httpx

    from recherche.search.verzeichnisse import StbVerzeichnisAdapter

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        plz = next(p for p in html_je_plz if f"plzFilter={p}".encode() in req.content)
        return httpx.Response(200, text=html_je_plz[plz])

    cfg = VerzeichnisConfig(url_vorlage=STBV_URL, formular={"nachnameOrFirmenname": "", "plzFilter": "{plz}"},
                            pause_s=30, je_plz=True, max_seiten=1)
    return StbVerzeichnisAdapter(cfg, cache=storage, client=httpx.Client(transport=httpx.MockTransport(handler)),
                                 sleep=lambda s: None)


def test_pilot_bericht_je_plz_abschneiden_cache_und_antwortzeit(storage, tmp_path):
    from recherche.stbv_lauf import bericht_csv

    html = BEISPIEL_STBV.read_text(encoding="utf-8")
    gekuerzt = html.replace("9\n          von\n          9", "9\n          von\n          12")
    orte = plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel", plz_weitere=("24105",))])
    stat = run_verzeichnis(storage, _stbv_adapter(storage, {"24103": html, "24105": gekuerzt}), orte, "SH", set(), "p1")
    b1, b2 = stat.berichte
    assert (b1.plz, b1.eintraege, b1.angezeigt, b1.gesamt, b1.kanzleien, b1.ohne_gesellschaft) == ("24103", 9, 9, 9, 4, 1)
    assert b1.sekunden is not None and not b1.cache and not b1.abgeschnitten
    assert b2.abgeschnitten and [b.plz for b in stat.abgeschnitten] == ["24105"]
    assert (b1.neu, b2.neu) == (4, 0)  # unter 24105 dieselben Kanzleien – schon im Bestand
    text = bericht_csv(stat.berichte, tmp_path / "b.csv").read_text(encoding="utf-8-sig")
    assert "24105;Kiel;9;9;12;ja;4;1;0;" in text

    # zweiter Adapter, gleiche Datenbank: Antwort aus dem Cache (neuer Lauf nach Löschen des Protokolls)
    storage.conn.execute("DELETE FROM lauf_protokoll")
    stat = run_verzeichnis(storage, _stbv_adapter(storage, {"24103": html, "24105": html}), orte, "SH", set(), "p2")
    assert all(b.cache and b.sekunden == 0 for b in stat.berichte)


def test_orte_im_kreis():
    from recherche.stbv_lauf import orte_im_kreis

    assert [o.name for o in orte_im_kreis(ORTE, "Kreisfreie Stadt Kiel")[0]] == ["Kiel"]
    assert [o.name for o in orte_im_kreis(ORTE, "rendsburg")[0]] == ["Kronshagen"]
    orte, fehler = orte_im_kreis(ORTE, "Kreis Plön")
    assert orte == [] and "nicht in der Ortsliste" in fehler and "Kreisfreie Stadt Kiel" in fehler
    assert "nicht eindeutig" in orte_im_kreis(ORTE, "Kreis")[1]


def test_person_ohne_gesellschaft_kennzeichen_und_zusammenfuehrung_ueber_website(storage):
    """Person ohne Firmenzeile: eigener Kandidat mit Kennzeichen; findet die Nachsuche die Website eines Kandidaten des
    Bestands, geht die Person in ihm auf. Gleiche Firmenzeile + Adresse ist eine Kanzlei (Vorgabe 05.11.2026)."""
    from fakes import FakeQueryAdapter

    from recherche.search.nachsuche import website_nachsuche
    from recherche.search.verzeichnisse import FIRMENZEILE_OHNE_KARTE, OHNE_GESELLSCHAFT

    bestand = storage.add_kandidat(Kandidat(bundesland="SH", domain="thordsen-steuer.example",
                                            website="https://thordsen-steuer.example/", name="Steuerbüro Thordsen"))
    storage.upsert_kanzlei(Kanzlei(kandidat_id=bestand, lauf_bundesland="SH", kanzleiname="Steuerbüro Thordsen",
                                   domain="thordsen-steuer.example"))  # Regel 2: nur in eine Kanzlei-Zeile
    html = BEISPIEL_STBV.read_text(encoding="utf-8")
    run_verzeichnis(storage, _stbv_adapter(storage, {"24103": html}), plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")]),
                    "SH", set(), "p1")
    zeilen = {z.name: z for z in luecken(storage, "SH")}
    imke = zeilen["Steuerberaterin Imke Thordsen"]
    assert imke.ohne_gesellschaft and luecken_werte_kennzeichen(imke) == OHNE_GESELLSCHAFT
    nordlicht = zeilen["Nordlicht Treuhand- und Revisionsgesellschaft mbH"]
    assert not nordlicht.ohne_gesellschaft and "Ole Ketelsen" in nordlicht.berufstraeger
    assert FIRMENZEILE_OHNE_KARTE in next(f.snippet for f in storage.funde_kandidat(nordlicht.kandidat_id))

    brave = FakeQueryAdapter("brave", {"Steuerberaterin Imke Thordsen Kiel": ["https://thordsen-steuer.example/"]})
    meldungen = []
    stat = website_nachsuche(storage, brave, "SH", QUELLE, set(), fortschritt=meldungen.append)
    assert stat.schon_vergeben == 1 and storage.get_kandidat(imke.kandidat_id) is None
    assert [f.name for f in storage.funde_kandidat(bestand) if f.quelle == QUELLE] == ["Steuerberaterin Imke Thordsen"]
    assert any("zusammengeführt" in m for m in meldungen)

    # Neu-Gruppierung setzt die von der Zusammenführung ergänzten Felder zurück (Adresse der Person)
    from recherche.stbv_lauf import neu_gruppieren_vorbereiten

    assert storage.get_kandidat(bestand).strasse == "Ring 1"
    neu_gruppieren_vorbereiten(storage, "SH")
    k = storage.get_kandidat(bestand)
    assert (k.strasse, k.plz, k.domain) == (None, None, "thordsen-steuer.example")


def luecken_werte_kennzeichen(z):
    from recherche.stbv_lauf import LUECKEN_SPALTEN, luecken_werte

    return luecken_werte(z)[LUECKEN_SPALTEN.index("Kennzeichen")]


def test_cli_pilot_kreis(monkeypatch, db_pfad, tmp_path):
    from typer.testing import CliRunner

    from recherche import cli

    fake = FakeVerzeichnis({"24119": [_hit(9, "Steuerberater Ole Rau", "Dorfstraße 1", "24119", "Kronshagen")],
                            "24103": [_hit(8, "Steuerberaterin Eva Ott", "Markt 1", "24103", "Kiel")]})
    monkeypatch.setattr(cli, "build_adapter", lambda name, s, cache=None: fake)
    monkeypatch.setattr(cli.Kontext, "orte", lambda self, auswahl, mit_zusatz=True: ORTE)
    monkeypatch.setattr(cli.Settings, "ausgabe_pfad", lambda self, bl, datum=None: tmp_path / "sh.xlsx")
    r = CliRunner().invoke(cli.app, ["stb-verzeichnis", "-b", "SH", "--db", str(db_pfad), "--ja", "--ohne-nachsuche",
                                     "--nur-verzeichnis", "--kreis", "Kiel"])
    assert r.exit_code == 0, r.output
    text = " ".join(r.output.split())
    assert "Pilot: nur Kreisfreie Stadt Kiel" in text and "2 PLZ, davon 2 offen" in text
    assert fake.abgefragt == ["24103", "24105"] and "Keine Abfrage abgeschnitten" in text
    assert (tmp_path / "stb_verzeichnis_plz_SH_Kreisfreie_Stadt_Kiel.csv").exists()
    assert "Eva Ott" in (tmp_path / "luecken_amtlich_SH_Kreisfreie_Stadt_Kiel.csv").read_text(encoding="utf-8-sig")
    assert "Ole Rau" not in (tmp_path / "luecken_amtlich_SH_Kreisfreie_Stadt_Kiel.csv").read_text(encoding="utf-8-sig")


def test_analyse_aus_dem_cache_schreibgeschuetzt(storage, db_pfad):
    """Analyse liest den Cache nur lesend: je PLZ Einträge, Gesellschaften, Personen, Kanzleien, Orte."""
    from recherche.stbv_analyse import analysiere, gecachte_seiten

    html = BEISPIEL_STBV.read_text(encoding="utf-8")
    run_verzeichnis(storage, _stbv_adapter(storage, {"24103": html}),
                    plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")]), "SH", set(), "p1")
    storage.close()
    seiten = gecachte_seiten(db_pfad)
    assert list(seiten) == ["24103"]
    a = analysiere(seiten["24103"][1], "24103")
    assert (a.eintraege, a.gesellschaften, a.personen, a.kanzleien) == (9, 2, 7, 4)
    assert (a.aus_gesellschaft, a.aus_firmenzeile, a.person_einzeln, a.fremde_plz) == (2, 1, 1, 0)
    assert a.orte == {"24103 Kiel": 9}


def test_neu_gruppieren_aus_dem_cache_ohne_abfragen(storage):
    """Bestand neu gruppieren: amtliche Kandidaten weg, Fremdkandidaten nur bereinigt; der zweite Lauf liest den Cache."""
    from recherche.stbv_lauf import neu_gruppieren_vorbereiten

    bestand = storage.add_kandidat(Kandidat(bundesland="SH", domain="hansen.example", name="Hansen & Petersen",
                                            strasse="Holstenstraße 42", plz="24103", ort="Kiel"))
    html = BEISPIEL_STBV.read_text(encoding="utf-8")
    orte = plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")])
    run_verzeichnis(storage, _stbv_adapter(storage, {"24103": html}), orte, "SH", set(), "p1")
    vorher = {k.name for k in storage.kandidaten("SH")}
    assert any(f.quelle == QUELLE for f in storage.funde_kandidat(bestand))
    entfernt, bereinigt = neu_gruppieren_vorbereiten(storage, "SH")
    assert (entfernt, bereinigt) == (3, 1)
    assert [k.id for k in storage.kandidaten("SH")] == [bestand]
    assert not any(f.quelle == QUELLE for f in storage.funde_kandidat(bestand))
    adapter = _stbv_adapter(storage, {})  # kein HTML: jede echte Abfrage schlüge fehl
    stat = run_verzeichnis(storage, adapter, orte, "SH", set(), "p2")
    assert adapter.anfragen == 0 and stat.berichte[0].cache and stat.neu == 3
    assert {k.name for k in storage.kandidaten("SH")} == vorher


def test_analyse_diagnose_der_einzelpersonen():
    """Einzelperson ohne Firmenzeile an einer Kanzlei-Adresse (wohl dort angestellt) und Syndikus-Verdacht
    (Konzern-Steuerabteilung) werden gezählt – Grundlage für die HH-Einschätzung."""
    from recherche.stbv_analyse import analysiere

    def karte(name, firma, strasse, beruf="Steuerberater"):
        zusatz = f"{firma}<br>" if firma else ""
        return (f'<a class="link-to-detail" href="https://x/details/{name}/"><p class="beruf">{beruf}</p>'
                f'<p class="name">{name}</p><p class="address-display">{zusatz}{strasse}<br>20095 Hamburg</p></a>')

    html = "<html><body>Treffer: 4 von 4" + "".join([
        karte("Ole Rau", "Rau Steuerberatung", "Markt 1"), karte("Eva Ott", None, "Markt 1"),
        karte("Kai Lund", None, "Wall 9"), karte("Ina Behn", "Muster AG Steuerabteilung", "Hafen 2")]) + "</body></html>"
    a = analysiere(html, "20095")
    # Eva Ott sitzt an der Adresse genau einer Kanzlei → zugeordnet (Regel 4, angestellt vermutet); einzeln bleibt
    # Kai Lund (allein an seiner Adresse)
    assert (a.person_einzeln, a.einzeln_allein, a.einzeln_mehrkanzlei, a.angestellt) == (1, 1, 0, 1)
    assert a.syndikus_verdacht == ["Muster AG Steuerabteilung"]


def test_regel_4_vermerk_kennzeichen_zu_pruefen_und_berufstraeger(storage, tmp_path):
    """SH 05.11.2026, Regel 4: Einzelperson ohne Firmenzeile an einer Adresse mit genau einer Kanzlei → dort
    Berufsträger (auch ohne Nachnamen im Namen), Vermerk „angestellt (vermutet)" im Kontrollblatt; bei mehreren
    Kanzleien ohne Namenstreffer eigene Zeile „Einzelperson an Mehrkanzlei-Adresse – zuordnen" → Blatt „Zu prüfen".
    Dazu: die Berufsträger-Liste endet vor dem Kennzeichen (vorher hing „Kanzlei aus Firmenzeile …" daran)."""
    from recherche.extract.merge import amtliche_berufstraeger
    from recherche.models import Fund, Status
    from recherche.qualitaet import zu_pruefen_grund
    from recherche.search.verzeichnisse import (
        ANGESTELLT,
        NICHT_ZUGEORDNET,
        StbvKarte,
        stbv_gruppieren,
    )
    from recherche.stbv_lauf import LUECKEN_SPALTEN, luecken_werte

    def person(name, firma, strasse, titel=None):
        return StbvKarte(name, "Steuerberater", titel, firma, strasse, "24103", "Kiel", f"https://x/{name}")

    def ges(name, strasse):
        return StbvKarte(name, None, None, None, strasse, "24103", "Kiel", f"https://x/{name}")

    hits = {h.name: h for h in stbv_gruppieren([
        person("Ole Rau", "Rau Steuerberatung", "Markt 1"), person("Eva Ott", None, "Markt 1", "Dipl.-Kffr. (FH)"),
        ges("Nord Treuhand GmbH StBG", "Wall 9"), ges("Süd Revision GmbH WPG", "Wall 9"),
        person("Kai Lund", None, "Wall 9")])}
    rau = hits["Rau Steuerberatung"]
    assert amtliche_berufstraeger([Fund(bundesland="SH", quelle=QUELLE, quell_id="1", snippet=rau.snippet)]) == [
        "Steuerberater Ole Rau", "Steuerberater Dipl.-Kffr. (FH) Eva Ott"]
    lund = hits["Steuerberater Kai Lund"]
    assert NICHT_ZUGEORDNET in lund.snippet

    for i, h in enumerate((rau, lund)):
        kid = storage.add_kandidat(Kandidat(bundesland="SH", name=h.name, strasse=h.strasse, plz=h.plz, ort=h.ort,
                                            quelle=QUELLE))
        storage.add_fund(Fund(kandidat_id=kid, bundesland="SH", quelle=QUELLE, quell_id=f"q{i}", snippet=h.snippet))
    zeilen = {z.name: luecken_werte(z) for z in luecken(storage, "SH")}
    spalte = LUECKEN_SPALTEN.index(f"Berufsträger {ANGESTELLT}")
    assert zeilen["Rau Steuerberatung"][spalte] == f"Steuerberater Dipl.-Kffr. (FH) Eva Ott – {ANGESTELLT}"
    assert zeilen["Steuerberater Kai Lund"][LUECKEN_SPALTEN.index("Kennzeichen")] == NICHT_ZUGEORDNET
    assert zu_pruefen_grund(Kanzlei(lauf_bundesland="SH", status=Status.NEU, abweichung=NICHT_ZUGEORDNET)) == \
        NICHT_ZUGEORDNET


def test_syndikus_in_der_lueckenliste_ohne_kandidat(storage, tmp_path):
    """Vorgabe 05.11.2026: Syndikus/Unternehmen nicht als Kandidat, aber in der Lückenliste – aus den gecachten
    Ergebnisseiten der im Land abgefragten PLZ (eine Karte nur einmal, Nachbar-PLZ anderer Länder nicht)."""
    from recherche.models import LaufEintrag
    from recherche.stbv_lauf import SCHRITT, SYNDIKUS_KENNZEICHEN, luecken_werte

    def karte(nr, name, firma, strasse, plz, ort):
        return (f'<a class="link-to-detail" href="https://stbv.example/details/{nr}/"><div><p class="beruf text-wrap">'
                f'Steuerberater</p><p class="akademische-grade text-wrap">Dr.</p><p class="name text-wrap">{name}</p>'
                f'<p class="address-display text-wrap">{firma}<br>{strasse}<br>{plz} {ort}</p></div></a>')

    seite = ("<html><body>Treffer: 2 von 2" + karte("S1", "Kai Lund", "Muster Werke AG", "Fabrikweg 2", "24103", "Kiel")
             + karte("S2", "Eva Rau", "Hanse Reederei GmbH", "Kai 1", "20095", "Hamburg") + "</body></html>")
    for plz in ("24103", "24105"):  # beide Abfragen liefern dieselbe Karte
        storage.cache_put(QUELLE, f"https://stbv.example/ POST plzFilter={plz}", {"html": seite})
        storage.log_lauf(LaufEintrag(lauf_id="l", schritt=SCHRITT, bundesland="SH", suchbegriff=f"plz:{plz}",
                                     adapter=QUELLE, ort="Kiel"))
    zeilen = luecken(storage, "SH", kreise(ORTE))
    assert [(z.kandidat_id, z.name, z.firma, z.kreis, z.stand) for z in zeilen] == [
        (None, "Kai Lund", "Muster Werke AG", "Kreisfreie Stadt Kiel", "Syndikus – nicht angelegt")]
    assert luecken_werte(zeilen[0])[2:4] == ["Dr. Kai Lund, Steuerberater", SYNDIKUS_KENNZEICHEN]
    assert storage.kandidaten("SH") == []
    assert luecken(storage, "HH") == []  # HH hat diese PLZ nicht abgefragt


def test_nachsuche_filtert_portale_und_fuehrt_nur_mit_bezug_zusammen(storage):
    """SH-Nachsuche 05.11.2026 (596 Zusammenführungen, viele falsch), fiktiv nachgebaut:
    1. Blocklist/Portal/„keine Kanzlei"-Domains werden übersprungen, der nächste Treffer zählt.
    2. Zusammenführen nur mit einer Kanzlei-Zeile und Namens- oder Adressbezug, sonst „keine passende Website".
    3. Domains von Ketten/Großgesellschaften (ketten.yaml) nie übernehmen."""
    from fakes import FakeQueryAdapter

    from recherche import ketten
    from recherche.models import Status
    from recherche.search.nachsuche import website_nachsuche

    ketten.setze_config(ketten.KettenConfig.laden_aus(
        {"grossgesellschaften": [{"name": "ETL", "muster": ["\\bETL\\b"], "domains": ["etl.example"]}]}))
    try:
        def amtlich(name, strasse="Markt 1", plz="25980", ort="Sylt"):
            return storage.add_kandidat(Kandidat(bundesland="SH", name=name, strasse=strasse, plz=plz, ort=ort,
                                                 quelle=QUELLE))

        portal = storage.add_kandidat(Kandidat(bundesland="SH", domain="ortsportal.example",
                                               website="https://ortsportal.example/", name="Ortsportal"))
        storage.upsert_kanzlei(Kanzlei(kandidat_id=portal, lauf_bundesland="SH", kanzleiname="Ortsportal",
                                       domain="ortsportal.example", status=Status.KEINE_KANZLEI))
        fremd = storage.add_kandidat(Kandidat(bundesland="SH", domain="hansen-stb.example",
                                              website="https://hansen-stb.example/", name="Steuerbüro Hansen"))
        storage.upsert_kanzlei(Kanzlei(kandidat_id=fremd, lauf_bundesland="SH", kanzleiname="Steuerbüro Hansen",
                                       domain="hansen-stb.example", strasse="Wall 9", plz="24103"))
        beine = amtlich("Steuerberater Siegfried Beine")
        forseti = amtlich("Forseti Steuerberatungsgesellschaft mbH", ort="Helgoland", plz="27498")
        fwr = amtlich("FWR Steuerberatungsgesellschaft mbH")
        lorenzen = amtlich("Lorenzen & Partner Steuerberater")
        from fakes import hit

        def t(url, titel):  # Titel nennt den Namen – wie bei Portal- und Registerseiten
            return hit("brave", url, name=titel)

        brave = FakeQueryAdapter("brave", {
            # Portal (keine Kanzlei) übersprungen, eigener Treffer danach zählt
            "Steuerberater Siegfried Beine Sylt": [t("https://ortsportal.example/beine", "Siegfried Beine – Ortsportal"),
                                                   t("https://steuerberater-beine.example/", "Start")],
            "Forseti Steuerberatungsgesellschaft mbH Helgoland": [
                t("https://online-handelsregister.de/forseti", "Forseti Steuerberatungsgesellschaft mbH")],
            "FWR Steuerberatungsgesellschaft mbH Sylt": [t("https://kanzlei.etl.example/fwr", "FWR Steuerberatung")],
            # Domain gehört einer Kanzlei ohne Namens-/Adressbezug → verworfen, nicht zusammengeführt
            "Lorenzen & Partner Steuerberater Sylt": [t("https://hansen-stb.example/lorenzen", "Lorenzen & Partner")],
        })
        meldungen = []
        stat = website_nachsuche(storage, brave, "SH", QUELLE, {"online-handelsregister.de"}, fortschritt=meldungen.append)
        assert storage.get_kandidat(beine).domain == "steuerberater-beine.example"
        assert storage.get_kandidat(forseti).domain is None and storage.get_kandidat(fwr).domain is None
        assert storage.get_kandidat(lorenzen) is not None and storage.get_kandidat(lorenzen).domain is None
        assert (stat.gefunden, stat.schon_vergeben, stat.verworfen) == (1, 0, 1)
        assert any("hansen-stb.example verworfen" in m and "ohne Namens- oder Adressbezug" in m for m in meldungen)
        assert any("Standorte nur über ketten.yaml" in m for m in meldungen)
        from recherche.stbv_lauf import LUECKEN_SPALTEN, luecken_werte

        herkunft = {z.name: luecken_werte(z)[LUECKEN_SPALTEN.index("Website-Herkunft")] for z in luecken(storage, "SH")}
        assert "Website steuerberater-beine.example" in herkunft["Steuerberater Siegfried Beine"]
        assert "hansen-stb.example verworfen" in herkunft["Lorenzen & Partner Steuerberater"]
    finally:
        ketten.setze_config(ketten.KettenConfig())


def test_land_ueber_plz_und_ort_nicht_ueber_die_abgefragte_plz(storage):
    """SH 05.11.2026: Die Abfrage 22145 (Braak, Stapelfeld) liefert auch „22145 Hamburg" – das gehört zu HH (dessen
    Lauf fragt 22145 selbst ab und legt es an); 21493 Schwarzenbek bleibt SH."""
    from recherche.stbv_lauf import LandNachOrt

    land = LandNachOrt({"SH": [Ort("Braak", "22145", "Kreis Stormarn"), Ort("Stapelfeld", "22145", "Kreis Stormarn"),
                               Ort("Schwarzenbek", "21493", "Kreis Herzogtum Lauenburg")],
                        "HH": [Ort("Hamburg", "20095", "Hamburg", plz_weitere=("22145",)),
                               Ort("Hamburg (22145)", "22145", "Hamburg", gemeinde="Hamburg")]})
    assert [land("22145", "Hamburg"), land("22145", "Stapelfeld"), land("21493", "Schwarzenbek"),
            land("22145", "Hamburg-Rahlstedt"), land("22145", "Irgendwo"), land("99999", "Hamburg")] == \
        ["HH", "SH", "SH", "HH", None, None]

    treffer = {"22145": [_hit(1, "Steuerkanzlei Rahlstedt", "Bahnhofstr. 1", "22145", "Hamburg"),
                         _hit(2, "Steuerbüro Braak", "Dorfstraße 3", "22145", "Braak")]}
    stat = run_verzeichnis(storage, FakeVerzeichnis(treffer), [Ort("Braak", "22145", "Kreis Stormarn")], "SH", set(),
                           "l", land_von=land)
    assert {k.name for k in storage.kandidaten("SH")} == {"Steuerbüro Braak"} and storage.kandidaten("HH") == []
    assert stat.anderes_land == {"HH": 1} and stat.berichte[0].anderes_land == 1 and stat.treffer == 1


def test_analyse_je_land_mit_gesamtzahl(monkeypatch, db_pfad):
    """stb-verzeichnis-analyse -b SH: nur PLZ des Landes, Kanzleien anderer Länder (PLZ+Ort) nicht gezählt, Summe ohne
    Doppel – die Landeszahl ohne neuen Lauf."""
    from typer.testing import CliRunner

    from recherche import cli, stbv_lauf
    from recherche.storage import Storage

    def karte(nr, name, strasse, ort):
        return (f'<a class="link-to-detail" href="https://x/details/{nr}/"><p class="beruf">Steuerberater</p>'
                f'<p class="name">{name}</p><p class="address-display">{strasse}<br>22145 {ort}</p></a>')

    seite = ("<html><body>Treffer: 4 von 4" + karte(1, "Ole Lund", "Dorfstraße 1", "Braak")
             + karte(2, "Eva Ott", "Dorfstraße 1", "Braak") + karte(3, "Kai Rau", "Bahnhofstr. 9", "Hamburg")
             + karte(4, "Ina Behn", "Am Teich 2", "Stapelfeld") + "</body></html>")
    with Storage(db_pfad) as st:
        st.cache_put(QUELLE, "https://stbv.example/ POST plzFilter=22145", {"html": seite})
    monkeypatch.setattr(cli.Kontext, "orte", lambda self, a, mit_zusatz=True: [Ort("Braak", "22145", "Kreis Stormarn")])
    monkeypatch.setattr(stbv_lauf.LandNachOrt, "aus_settings", classmethod(lambda cls, s: cls(
        {"SH": [Ort("Braak", "22145"), Ort("Stapelfeld", "22145")], "HH": [Ort("Hamburg", "22145")]})))
    r = CliRunner().invoke(cli.app, ["stb-verzeichnis-analyse", "-b", "SH", "--db", str(db_pfad)], env={"COLUMNS": "300"})
    text = " ".join(r.output.split())
    assert r.exit_code == 0, r.output
    assert "Kanzleien gesamt (ohne Doppel aus mehreren Abfragen): 2" in text  # Lund & Ott, Behn
    assert "1 laut PLZ und Ort in einem anderen Land nicht gezählt" in text
    assert "Sozietät/Bürogemeinschaft 1 (2 Personen) · einzeln 1 (davon 1 allein an ihrer Adresse, 0 an einer " \
           "Mehrkanzlei-Adresse" in text
