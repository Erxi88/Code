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
    assert (b1.plz, b1.eintraege, b1.angezeigt, b1.gesamt, b1.kanzleien, b1.ohne_gesellschaft) == ("24103", 9, 9, 9, 5, 3)
    assert b1.sekunden is not None and not b1.cache and not b1.abgeschnitten
    assert b2.abgeschnitten and [b.plz for b in stat.abgeschnitten] == ["24105"]
    assert (b1.neu, b2.neu) == (5, 0)  # unter 24105 dieselben Kanzleien – schon im Bestand
    text = bericht_csv(stat.berichte, tmp_path / "b.csv").read_text(encoding="utf-8-sig")
    assert "24105;Kiel;9;9;12;ja;5;3;0;" in text

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
    """Je Person ein Kandidat; die Nachsuche sucht mit der Firmenzeile und führt Personen derselben Website zusammen."""
    from fakes import FakeQueryAdapter

    from recherche.search.nachsuche import website_nachsuche
    from recherche.search.verzeichnisse import OHNE_GESELLSCHAFT

    html = BEISPIEL_STBV.read_text(encoding="utf-8")
    run_verzeichnis(storage, _stbv_adapter(storage, {"24103": html}), plz_orte([Ort("Kiel", "24103", "Kreisfreie Stadt Kiel")]),
                    "SH", set(), "p1")
    zeilen = {z.name: z for z in luecken(storage, "SH")}
    ole, lars = zeilen["Steuerberater Ole Ketelsen"], zeilen["Steuerberater Lars Ketelsen"]
    assert ole.ohne_gesellschaft and ole.firma == "Nordlicht Treuhand- und Revisionsgesellschaft mbH"
    assert not zeilen["Förde Steuerberater PartG mbB"].ohne_gesellschaft
    assert luecken_werte_kennzeichen(ole) == OHNE_GESELLSCHAFT

    # Nachsuche: beide Ketelsen mit der Firmenzeile → dieselbe Website → ein Kandidat mit beiden Funden
    firma = "Nordlicht Treuhand- und Revisionsgesellschaft mbH Kiel"
    brave = FakeQueryAdapter("brave", {firma: ["https://www.nordlicht-treuhand.example/"]})
    meldungen = []
    stat = website_nachsuche(storage, brave, "SH", QUELLE, set(), fortschritt=meldungen.append)
    assert brave.aufrufe.count(firma) == 2 and stat.gefunden == 1 and stat.schon_vergeben == 1
    k = storage.kandidat_nach_domain("nordlicht-treuhand.example")
    assert storage.get_kandidat(ole.kandidat_id if ole.kandidat_id != k.id else lars.kandidat_id) is None
    namen = [f.name for f in storage.funde_kandidat(k.id) if f.quelle == QUELLE]
    assert sorted(namen) == ["Steuerberater Lars Ketelsen", "Steuerberater Ole Ketelsen"]
    assert any("zusammengeführt" in m for m in meldungen)


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
