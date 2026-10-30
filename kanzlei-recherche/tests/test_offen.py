"""`recherche offen`: Kandidaten ohne Kanzlei-Zeile nach Ursache, Weg und Länder-Hinweis – fiktive Daten."""

from recherche.models import Fund, Kandidat, Kanzlei, SchrittStatus
from recherche.offen import NEIN, PLAYWRIGHT, WIEDERHOLEN, auswerten, ursache
from recherche.search.pipeline import Fremdorte, Ort

LAENDER = {"24103": "Schleswig-Holstein", "25813": "Schleswig-Holstein", "01067": "Sachsen", "53111": "Nordrhein-Westfalen"}


def _k(fehler=None, crawl=SchrittStatus.FEHLER, **kw):
    return Kandidat(bundesland="SH", domain=kw.pop("domain", "x.example"), website="https://x.example/",
                    crawl_status=crawl, fehler=fehler, **kw)


def test_ursachen_und_wege():
    faelle = {
        "Startseite: Timeout nach 15 s": ("Startseite Timeout", WIEDERHOLEN),
        "Startseite: nicht erreichbar (ConnectError)": ("Startseite nicht erreichbar (ConnectError)", WIEDERHOLEN),
        "Startseite: HTTP 503": ("Startseite HTTP 5xx/429", WIEDERHOLEN),
        "Startseite: HTTP 403": ("Startseite HTTP 403 (Bot-Schutz)", PLAYWRIGHT),
        "Startseite: HTTP 404": ("Startseite HTTP 404/410", NEIN),
        "Startseite: Domain nicht erreichbar (DNS)": ("Domain existiert nicht (DNS)", NEIN),
        "Startseite: robots.txt verbietet den Zugriff": ("robots.txt verbietet", NEIN),
        "Weiterleitung auf Portal (11880.com)": ("Weiterleitung auf Portal", NEIN),
        "Seite ohne Text (vermutlich JavaScript; 12 Zeichen, Playwright: crawl.playwright ist aus)":
            ("Seite ohne Text, nicht gerendert", PLAYWRIGHT),
        "Seite ohne Text (vermutlich JavaScript; 12 Zeichen, Playwright: gerendert, nicht mehr Text)":
            ("Seite ohne Text, gerendert (gerendert, nicht mehr Text)", NEIN),
        "Kein Impressum gefunden (probiert: /impressum, /impressum/, /impressum.html)":
            ("kein Impressum (ohne zusätzliche Pfade)", WIEDERHOLEN),
        "Kein Impressum gefunden (probiert: /impressum, /imprint, /impressum.php)": ("kein Impressum (alle Pfade probiert)", NEIN),
        "außerhalb Schleswig-Holstein laut Suchtreffer (01067 Dresden – Sachsen)":
            ("außerhalb laut Suchtreffer (nicht gecrawlt)", NEIN),
        "Fehler: ValueError: kaputt": ("unerwarteter Fehler (ValueError)", WIEDERHOLEN),
    }
    for grund, erwartet in faelle.items():
        assert ursache(_k(grund), playwright_an=True) == erwartet, grund
    assert ursache(_k("Startseite: HTTP 403"), playwright_an=False)[1] == "nein (crawl.playwright aus)"
    assert ursache(_k(crawl=SchrittStatus.OFFEN), True) == ("nie gecrawlt", "crawl")
    assert ursache(Kandidat(bundesland="SH", name="Steuerbüro A"), True) == ("ohne Website, nicht extrahiert", "extract")


def test_auswertung_mit_laender_hinweis(storage):
    def neu(k, *funde):
        kid = storage.add_kandidat(k)
        for i, (name, snippet, plz) in enumerate(funde):
            storage.add_fund(Fund(kandidat_id=kid, bundesland="SH", quelle="brave", quell_id=f"{kid}-{i}", lauf_id="l",
                                  name=name, snippet=snippet, plz=plz))
        return kid

    gelesen = neu(_k(crawl=SchrittStatus.FERTIG, domain="gelesen.example"))
    storage.upsert_kanzlei(Kanzlei(kandidat_id=gelesen, domain="gelesen.example", lauf_bundesland="SH", kanzleiname="G"))
    neu(_k("Startseite: Timeout nach 15 s", domain="a.example", suchort="Tetenbüll"),
        ("Steuerberater Dresden", "Kanzlei in 01067 Dresden", None))
    neu(_k("Startseite: HTTP 403", domain="b.example", suchort="Husum"), ("Steuerbüro B", "Markt 1, 25813 Husum", None))
    neu(_k("Startseite: HTTP 403", domain="c.example", suchort="Tetenbüll"), ("Steuerberatung Bonn", "Ihr Berater", None))
    neu(_k("Kein Impressum gefunden (probiert: /impressum)", domain="d.example", suchort="Husum"))
    orte = [Ort("Tetenbüll", "25882", einwohner=600), Ort("Husum", "25813", einwohner=23000)]
    erg = auswerten(storage, "SH", orte, LAENDER.get, "Schleswig-Holstein",
                    Fremdorte({"Bonn": "NW", "Kiel": "SH"}, "SH", unter_einwohner=10**9))
    e = {x.domain: x for x in erg.eintraege}
    assert set(e) == {"a.example", "b.example", "c.example", "d.example"}  # gelesen.example hat eine Zeile
    assert (e["a.example"].land_hinweis, e["a.example"].plz_hinweis) == ("PLZ außerhalb", "01067 Dresden – Sachsen")
    assert e["b.example"].land_hinweis == "PLZ im Land" and e["b.example"].fremdort is None
    assert (e["c.example"].land_hinweis, e["c.example"].fremdort) == ("keine PLZ", "Bonn")
    assert erg.vermutlich_ausserhalb() == 2
    zeilen = {u: c for u, _, c in erg.je_ursache()}
    assert zeilen["Startseite HTTP 403 (Bot-Schutz)"]["gesamt"] == 2
    assert zeilen["Startseite HTTP 403 (Bot-Schutz)"]["fremdort"] == 1
    assert zeilen["Startseite Timeout"]["unter 5.000 EW"] == 1
    assert erg.je_holbar() == {PLAYWRIGHT: 2, WIEDERHOLEN: 2}


def test_coverage_trennt_vorfilter_von_offen(storage):
    from recherche.coverage import berechne

    gelesen = storage.add_kandidat(_k(crawl=SchrittStatus.FERTIG, domain="g.example", plz="24103"))
    storage.upsert_kanzlei(Kanzlei(kandidat_id=gelesen, domain="g.example", lauf_bundesland="SH", kanzleiname="G",
                                   plz="24103", bundesland="Schleswig-Holstein"))
    storage.add_kandidat(_k("außerhalb Schleswig-Holstein laut Suchtreffer (01067 Dresden – Sachsen)", domain="v.example"))
    storage.add_kandidat(_k("Startseite: Timeout nach 15 s", domain="t.example"))
    erg = berechne(storage, "SH", [Ort("Kiel", "24103", "Kreisfreie Stadt Kiel", einwohner=247000)],
                   ziel_land="Schleswig-Holstein")
    assert (erg.offen, erg.ausserhalb_vorab) == (1, 1)


def test_coverage_zaehlt_weitere_domains_einmal(storage):
    from recherche.coverage import berechne
    from recherche.models import Ansprechpartner

    for d in ("knueppel.example", "steuerberater-wedel.example", "steuerberater-uetersen.example"):
        kid = storage.add_kandidat(_k(crawl=SchrittStatus.FERTIG, domain=d))
        storage.upsert_kanzlei(Kanzlei(kandidat_id=kid, domain=d, lauf_bundesland="SH", kanzleiname="Knüppel & Partner",
                                       telefon="+49 4103 1234", plz="22880", strasse="Bahnhofstr. 1",
                                       bundesland="Schleswig-Holstein",
                                       ansprechpartner=[Ansprechpartner(name="Jan Knüppel", funktion="Steuerberater")]))
    erg = berechne(storage, "SH", [Ort("Wedel", "22880", "Kreis Pinneberg", einwohner=34000)], ziel_land="Schleswig-Holstein")
    assert (erg.gefunden, erg.weitere_domains) == (1, 2)
