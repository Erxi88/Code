"""Stichprobe Seed 2 (03.11.2026): Seitentitel/Domain statt Firmierung."""

from recherche.extract.base import LlmErgebnis
from recherche.extract.merge import generischer_titel, name_wie_domain, zusammenfuehren
from recherche.models import Kandidat, Seite, Seitentyp
from recherche.normalize import PlzPruefung

SH = "Schleswig-Holstein"


def _merge(domain, impressum, llm_name):
    seiten = {Seitentyp.IMPRESSUM: Seite(url=f"https://{domain}/impressum", domain=domain, seitentyp=Seitentyp.IMPRESSUM,
                                         text="\n".join(impressum))}
    return zusammenfuehren(Kandidat(id=1, bundesland="SH", domain=domain), [], seiten, None,
                           LlmErgebnis(kanzleiname=llm_name), lambda p, o: PlzPruefung(True, SH, ort_kanonisch=o))


def test_generische_titel_und_domain_namen():
    assert generischer_titel("Mein Steuerberater in Kiel") and generischer_titel("Kanzlei für Steuer- und Wirtschaftsberatung")
    assert not generischer_titel("Steuerkanzlei Lund") and not generischer_titel("Hansen Steuerberatung")
    assert name_wie_domain("Lund Steuer", "lund-steuer.example") and not name_wie_domain("Lund & Partner", "lund-partner.de")


def test_seitentitel_als_name_wird_durch_person_mit_berufsbezeichnung_ersetzt():
    """schroeder-steuer.de „Schroeder Steuer", mein-steuerberater-in-kiel.de, steuerberatungluebeck.de."""
    impressum = ["Impressum", "Angaben gemäß § 5 DDG", "Ina Lund, Steuerberaterin", "Markt 1", "24103 Kiel"]
    for domain, titel in [("lund-steuer.example", "Lund Steuer"), ("mein-steuerberater-in-kiel.example",
                                                                   "Mein Steuerberater in Kiel"),
                          ("steuerberatung-luebeck.example", "Kanzlei für Steuer- und Wirtschaftsberatung")]:
        k = _merge(domain, impressum, titel)
        assert k.kanzleiname == "Steuerberaterin Ina Lund", (domain, k.kanzleiname)
        assert "Seitentitel" in k.abweichung


def test_echte_firmierung_aus_domain_woertern_bleibt():
    """„Hansen Steuerberatung" bei hansen-steuerberatung.de steht im Impressum – das ist die Firmierung."""
    impressum = ["Impressum", "Hansen Steuerberatung", "Inhaber: Ole Hansen, Steuerberater", "Markt 1", "24103 Kiel"]
    assert _merge("hansen-steuerberatung.example", impressum, "Hansen Steuerberatung").kanzleiname == "Hansen Steuerberatung"


def test_domain_name_zaehlt_nicht_ueber_url_und_email():
    """grunewald-stb.de (Rückschritt gegenüber 72b4204): „www.stb-grunewald.de" und „info@stb-grunewald.de" im Impressum
    sind kein Beleg für den LLM-Namen „Stb-Grunewald"."""
    impressum = ["Impressum", "Malte Grünberg, Steuerberater", "Holstenstraße 5", "24103 Kiel",
                 "E-Mail: info@stb-gruenberg.example", "Internet: www.stb-gruenberg.example",
                 "https://stb-gruenberg.example/impressum"]
    k = _merge("stb-gruenberg.example", impressum, "Stb-Gruenberg")
    assert k.kanzleiname == "Steuerberater Malte Grünberg" and "Seitentitel" in k.abweichung

