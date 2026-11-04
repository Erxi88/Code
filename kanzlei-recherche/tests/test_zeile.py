"""`recherche zeile --domain`: alles zu einem Kandidaten, auch für Zeilen, die in keinem Blatt des Exports stehen."""

from typer.testing import CliRunner

from recherche import cli
from recherche.extract.kanzlei import GRUND_PRAEFIX
from recherche.models import Fund, Kandidat, Kanzlei, Konfidenz, Status
from recherche.zeile import bericht, domain_normal, zeilen


def _anlegen(st, domain, **kanzlei):
    kid = st.add_kandidat(Kandidat(bundesland="SH", domain=domain, website=f"https://{domain}/", quelle="brave",
                                   suchort="Kiel"))
    st.add_fund(Fund(kandidat_id=kid, bundesland="SH", quelle="brave", quell_id=f"{domain}-1", suchort="Kiel",
                     name="Steuerberater Muster", snippet="Ihr Steuerberater in Kiel"))
    st.upsert_kanzlei(Kanzlei(kandidat_id=kid, domain=domain, lauf_bundesland="SH", quell_urls=[f"https://{domain}/"],
                              **kanzlei))
    return kid


def test_domain_normal():
    assert domain_normal("https://www.Kanzlei.example/impressum") == "kanzlei.example"
    assert domain_normal("www.kanzlei.example") == domain_normal("kanzlei.example") == "kanzlei.example"


def test_zeile_keine_kanzlei_mit_plz_in_anderem_land(storage):
    """SH-Lauf, PLZ in Hamburg, Status keine Kanzlei: steht in keinem Blatt des SH-Exports – mit Begründung."""
    _anlegen(storage, "hagen-steuer.example", kanzleiname=None, plz="20095", ort="Hamburg", bundesland="Hamburg",
             plz_verifiziert=True, status=Status.KEINE_KANZLEI, konfidenz=Konfidenz.MITTEL,
             abweichung=f"{GRUND_PRAEFIX}Branchenfremd (kein Kanzleiname); Name „Hagen Steuer“ ist nur "
                        "Domain/Seitentitel – verworfen")
    b = bericht(storage, "https://www.hagen-steuer.example/")
    text = "\n".join(zeilen(b))
    assert "Lauf SH" in text and "Bundesland laut PLZ Hamburg" in text
    assert "Grund „keine Kanzlei“: Branchenfremd (kein Kanzleiname)" in text
    assert "  - Name „Hagen Steuer“ ist nur Domain/Seitentitel – verworfen" in text
    assert "Fund brave (Kiel): Steuerberater Muster" in text and "https://hagen-steuer.example/" in text
    assert b.blatt is None and "PLZ in Hamburg" in b.blatt_grund and "Lieferliste Schleswig-Holstein" in b.blatt_grund


def test_zeile_blatt_im_land(storage):
    _anlegen(storage, "lund.example", kanzleiname="Steuerkanzlei Lund", plz="24103", ort="Kiel",
             bundesland="Schleswig-Holstein", plz_verifiziert=True, konfidenz=Konfidenz.HOCH)
    _anlegen(storage, "weg.example", kanzleiname="Weg GmbH", plz="24103", ort="Kiel", bundesland="Schleswig-Holstein",
             plz_verifiziert=True, status=Status.KEINE_KANZLEI, abweichung=f"{GRUND_PRAEFIX}Branchenfremd (…)")
    assert bericht(storage, "lund.example").blatt == "Kanzleien"
    b = bericht(storage, "weg.example")
    assert b.blatt is None and "--mit-kontrollblaettern" in b.blatt_grund
    assert bericht(storage, "weg.example", mit_kontrollblaettern=True).blatt == "Keine Kanzlei"


def test_cli_zeile(storage, db_pfad):
    _anlegen(storage, "lund.example", kanzleiname="Steuerkanzlei Lund", plz="24103", ort="Kiel",
             bundesland="Schleswig-Holstein", plz_verifiziert=True, konfidenz=Konfidenz.HOCH)
    _anlegen(storage, "niedrig.example", kanzleiname="Steuerkanzlei Ott", plz="24103", ort="Kiel",
             bundesland="Schleswig-Holstein", plz_verifiziert=True, konfidenz=Konfidenz.NIEDRIG)
    assert bericht(storage, "niedrig.example").blatt == "Zu prüfen"
    storage.close()
    r = CliRunner().invoke(cli.app, ["zeile", "--domain", "lund.example", "--db", str(db_pfad)])
    assert r.exit_code == 0, r.output
    assert "Kanzleiname: Steuerkanzlei Lund" in r.output and "Export-Blatt: Kanzleien" in r.output
    r = CliRunner().invoke(cli.app, ["zeile", "--domain", "unbekannt.example", "--db", str(db_pfad)])
    assert r.exit_code == 1 and "Kein Kandidat" in r.output
