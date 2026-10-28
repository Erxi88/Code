"""blocklist-vorschlag: Portale in „Nicht gefunden" (robots.txt/403) nach Suchorten."""

from typer.testing import CliRunner

from recherche import cli
from recherche.blocklist_vorschlag import als_csv, stammdomain, vorschlaege
from recherche.models import Fund, Kandidat, NichtGefunden


def _anlegen(storage, domain, grund, suchorte, name=None, snippet=None):
    kid = storage.add_kandidat(Kandidat(bundesland="SH", domain=domain, website=f"https://{domain}/", name=name))
    for i, ort in enumerate(suchorte):
        storage.add_fund(Fund(kandidat_id=kid, bundesland="SH", quelle="brave", quell_id=f"{domain}-{i}",
                              suchort=ort, name=name, snippet=snippet, website=f"https://{domain}/"))
    storage.add_nicht_gefunden(NichtGefunden(domain=domain, bundesland="SH", grund=grund, kandidat_id=kid))
    return kid


def test_sortiert_nach_suchorten_und_fasst_subdomains_zusammen(storage):
    orte = [f"Ort {i}" for i in range(20)]
    _anlegen(storage, "kiel.steuerportal.example", "Startseite: robots.txt verbietet den Zugriff", orte[:12],
             name="Steuerberater in Kiel – Steuerportal", snippet="Finden Sie Steuerberater in Kiel")
    _anlegen(storage, "luebeck.steuerportal.example", "Startseite: HTTP 403", orte[10:20])
    _anlegen(storage, "kanzlei-nord.example", "Startseite: robots.txt verbietet den Zugriff", ["Husum"])
    _anlegen(storage, "kanzlei-sued.example", "Kein Impressum gefunden (probiert: /impressum)", orte)  # anderer Grund
    _anlegen(storage, "gelbeseiten.de", "Startseite: HTTP 403", orte)  # schon auf der Blocklist
    liste = vorschlaege(storage, "SH", {"gelbeseiten.de"})
    assert [v.domain for v in liste] == ["steuerportal.example", "kanzlei-nord.example"]
    portal = liste[0]
    assert len(portal.suchorte) == 20 and portal.treffer == 22
    assert portal.domains == {"kiel.steuerportal.example", "luebeck.steuerportal.example"}
    assert portal.grund_kurz == "HTTP 403, robots.txt" and portal.name.startswith("Steuerberater in Kiel")
    assert stammdomain("www.kanzlei.example") == "kanzlei.example"


def test_csv(storage, tmp_path):
    _anlegen(storage, "steuerportal.example", "Startseite: robots.txt nicht erreichbar (Timeout) – nach RFC 9309 gesperrt",
             ["Kiel", "Husum"], snippet="Portal")
    pfad = als_csv(vorschlaege(storage, "SH"), tmp_path / "v.csv")
    zeilen = pfad.read_text(encoding="utf-8-sig").splitlines()
    assert zeilen[0].startswith("domain;suchorte;treffer;portal;grund") and zeilen[1].startswith("steuerportal.example;2;2;;robots.txt")


def test_cli(monkeypatch, tmp_path):
    from recherche.storage import Storage

    db = tmp_path / "t.sqlite"
    with Storage(db) as st:
        _anlegen(st, "steuerportal.example", "Startseite: HTTP 403", ["Kiel", "Husum", "Heide"], name="Steuerportal")
    r = CliRunner().invoke(cli.app, ["blocklist-vorschlag", "--db", str(db), "-b", "SH", "--out", str(tmp_path / "v.csv")])
    assert r.exit_code == 0, r.output
    assert "steuerportal.example" in r.output and "CSV geschrieben" in r.output
    r = CliRunner().invoke(cli.app, ["blocklist-vorschlag", "--db", str(db), "-b", "SH", "--mindestens", "5"])
    assert "Keine Domains" in r.output
