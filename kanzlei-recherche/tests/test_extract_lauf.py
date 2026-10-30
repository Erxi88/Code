
import httpx
import pytest
from fakes import FIXTURES

from recherche.extract.base import (
    AnbieterNichtErreichbar,
    Extractor,
    ExtraktionFehler,
)
from recherche.extract.lauf import run_extract
from recherche.extract.rules import Ausschluss
from recherche.fixtures import Fixture
from recherche.models import Fund, Kandidat, Konfidenz, SchrittStatus, Seitentyp, Status
from recherche.normalize import PlzPruefer

BSP = FIXTURES / "kanzleiseiten" / "beispiele"


class AufzeichnungsExtractor(Extractor):
    """Gibt die aufgezeichnete Antwort der passenden Beispiel-Fixture zurück."""

    name = "test"

    def __init__(self, fehler: Exception | None = None):
        self.modell = "qwen3.5:9b"
        self.fehler = fehler
        self.aufrufe = 0

    def extract(self, seiten):
        self.aufrufe += 1
        if self.fehler:
            raise self.fehler
        domain = next(iter(seiten.values())).domain
        return Fixture.laden(BSP / domain).aufzeichnung(self.modell)


def _pruefer():
    sh = {"federalState": {"name": "Schleswig-Holstein"}}
    orte = {"24103": "Kiel", "24937": "Flensburg", "24534": "Neumünster"}
    return PlzPruefer(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=[{"name": orte.get(r.url.params["postalCode"], "X"), **sh}]))))


def _kandidat_mit_seiten(storage, domain):
    fx = Fixture.laden(BSP / domain)
    kid = storage.add_kandidat(Kandidat(bundesland="SH", domain=domain, website=f"https://{domain}/",
                                        name="Startseite", crawl_status=SchrittStatus.FERTIG))
    for typ, s in fx.seiten.items():
        storage.save_seite(s)
        storage.setze_kandidat_seite(kid, typ, s.url)
    return kid


@pytest.fixture
def ausschluss(settings):
    return Ausschluss.laden(settings.pfad(settings.pfade.impressum_ausschluss))


def test_extraktion_ende_zu_ende(storage, ausschluss):
    kid = _kandidat_mit_seiten(storage, "hansen-steuerberatung.example")
    ohne = storage.add_kandidat(Kandidat(bundesland="SH", name="Kanzlei Ohneweb", plz="24103"))
    storage.add_fund(Fund(kandidat_id=ohne, bundesland="SH", quelle="places", quell_id="p", name="Kanzlei Ohneweb",
                          strasse="Wall 5", plz="24103", ort="Kiel", telefon="0431 222"))
    fehl = storage.add_kandidat(Kandidat(bundesland="SH", domain="kaputt.example", crawl_status=SchrittStatus.FEHLER))
    ex = AufzeichnungsExtractor()
    stat = run_extract(storage, "SH", ex, _pruefer(), ausschluss, "l1")
    assert (stat.fertig, stat.ohne_website, stat.fehler, stat.llm_aufrufe) == (2, 1, 0, 1)
    assert stat.mittlere_laufzeit_s == pytest.approx(41.3) and stat.tokens == 3184 + 402
    k = storage.kanzlei_fuer_kandidat(kid)
    assert k.kanzleiname == "Hansen & Petersen Steuerberater PartG mbB"
    assert (k.strasse, k.plz, k.telefon, k.email) == ("Holstenstraße 42", "24103", "+49 431 9987610",
                                                       "info@hansen-steuerberatung.example")
    assert (k.konfidenz, k.bundesland, k.berufstraeger_anzahl, k.teamgroesse) == (Konfidenz.HOCH, "Schleswig-Holstein", 3, 6)
    assert [a.name for a in k.ansprechpartner] == ["Jens Hansen", "Maren Petersen", "Ole Brandt"]
    # Kandidat: nur leere Felder ergänzt (Websuche-Name bleibt)
    kand = storage.get_kandidat(kid)
    assert (kand.name, kand.telefon, kand.plz) == ("Startseite", "+49 431 9987610", "24103")
    assert kand.extract_status == SchrittStatus.FERTIG
    o = storage.kanzlei_fuer_kandidat(ohne)
    # ohne Website, ohne Fund des amtlichen Verzeichnisses: kein Positivsignal aus 1B → niedrig (Zu prüfen)
    assert (o.kanzleiname, o.website_vorhanden, o.konfidenz) == ("Kanzlei Ohneweb", False, Konfidenz.NIEDRIG)
    assert storage.kanzlei_fuer_kandidat(fehl) is None  # steht in „Nicht gefunden"
    (e,) = [x for x in storage.lauf_protokoll("SH") if x.schritt == "extract"]
    assert (e.adapter, e.anfragen, e.trefferzahl) == ("qwen3.5:9b", 1, 2)


def test_wiederaufnahme_und_status_bleibt(storage, ausschluss):
    kid = _kandidat_mit_seiten(storage, "foerde-steuer.example")
    run_extract(storage, "SH", AufzeichnungsExtractor(), None, ausschluss, "l1")
    k = storage.kanzlei_fuer_kandidat(kid)
    storage.upsert_kanzlei(k.model_copy(update={"status": Status.KONTAKTIERT}))
    ex = AufzeichnungsExtractor()
    run_extract(storage, "SH", ex, None, ausschluss, "l2")
    assert ex.aufrufe == 0  # schon erledigt
    storage.set_extract_status(kid, SchrittStatus.OFFEN)
    run_extract(storage, "SH", AufzeichnungsExtractor(), None, ausschluss, "l3")
    assert storage.kanzlei_fuer_kandidat(kid).status == Status.KONTAKTIERT  # erneute Extraktion setzt Status nicht zurück
    assert len(storage.kanzleien("SH")) == 1


def test_ohne_llm(storage, ausschluss):
    kid = _kandidat_mit_seiten(storage, "steuerbuero-schuett.example")
    stat = run_extract(storage, "SH", None, None, ausschluss, "l1")
    k = storage.kanzlei_fuer_kandidat(kid)
    assert stat.llm_aufrufe == 0
    assert (k.strasse, k.telefon, k.email) == ("Großflecken 3", "+49 4321 559012", "kanzlei@steuerbuero-schuett.example")
    assert k.kanzleiname is None and k.ansprechpartner == [] and k.berufstraeger_anzahl is None


def test_ollama_nicht_erreichbar_beendet_lauf(storage, ausschluss):
    a = _kandidat_mit_seiten(storage, "steuerbuero-schuett.example")
    _kandidat_mit_seiten(storage, "foerde-steuer.example")
    ex = AufzeichnungsExtractor(AnbieterNichtErreichbar("Ollama nicht erreichbar unter http://localhost:11434"))
    stat = run_extract(storage, "SH", ex, None, ausschluss, "l1")
    assert stat.abbruch and ex.aufrufe == 1
    assert storage.get_kandidat(a).extract_status == SchrittStatus.OFFEN  # nicht als Fehler markiert


def test_fehler_eines_kandidaten(storage, ausschluss):
    kid = _kandidat_mit_seiten(storage, "steuerbuero-schuett.example")
    stat = run_extract(storage, "SH", AufzeichnungsExtractor(ExtraktionFehler("ungültige Antwort")), None, ausschluss, "l1")
    assert stat.fehler == 1
    assert storage.get_kandidat(kid).extract_status == SchrittStatus.FEHLER
    assert storage.fehler_zuruecksetzen("SH", "extract") == 1
    assert run_extract(storage, "SH", AufzeichnungsExtractor(), None, ausschluss, "l2").fertig == 1


def test_kanzlei_pruefung_laesst_alle_beispielkanzleien_stehen(storage, ausschluss):
    domains = sorted(p.name for p in BSP.iterdir() if (p / "meta.json").exists())
    for d in domains:
        _kandidat_mit_seiten(storage, d)
    run_extract(storage, "SH", AufzeichnungsExtractor(), _pruefer(), ausschluss, "l1",
                kanzlei_rechtsformen=["PartG mbB", "PartG", "Einzelkanzlei"])
    assert {k.domain: k.status for k in storage.kanzleien("SH")} == dict.fromkeys(domains, Status.NEU)


def test_hinweis_robots_nicht_abrufbar_in_abweichung(storage, ausschluss):
    kid = _kandidat_mit_seiten(storage, "foerde-steuer.example")
    start = storage.kandidat_seiten(kid)[__import__("recherche.models", fromlist=["Seitentyp"]).Seitentyp.START]
    host = "/".join(start.url.split("/")[:3])
    storage.cache_put("robots", host, {"status": 500, "text": "", "url": host})
    run_extract(storage, "SH", AufzeichnungsExtractor(), None, ausschluss, "l1")
    assert "robots.txt nicht abrufbar (HTTP 500), Seite gelesen" in storage.kanzlei_fuer_kandidat(kid).abweichung


def test_ausserhalb_ohne_llm_aufruf(storage, ausschluss):
    """Kleingemeinden: Brave liefert bundesweite Kanzleien – PLZ außerhalb SH → kein LLM-Aufruf."""
    import httpx

    from recherche.normalize import PlzPruefer

    kid = _kandidat_mit_seiten(storage, "hansen-steuerberatung.example")  # Impressum: 24103 Kiel
    sachsen = {"federalState": {"name": "Sachsen"}}
    pruefer = PlzPruefer(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=[{"name": "Kiel", **sachsen}]))))  # tut so, als läge 24103 in Sachsen
    ex = AufzeichnungsExtractor()
    stat = run_extract(storage, "SH", ex, pruefer, ausschluss, "l1", ziel_land="Schleswig-Holstein")
    k = storage.kanzlei_fuer_kandidat(kid)
    assert (ex.aufrufe, stat.ohne_llm_ausserhalb, k.status) == (0, 1, Status.AUSSERHALB)
    assert "ohne LLM: PLZ 24103 liegt in Sachsen" in k.abweichung
    # im Land: LLM läuft wie gewohnt
    storage.set_extract_status(kid, SchrittStatus.OFFEN)
    ex2 = AufzeichnungsExtractor()
    run_extract(storage, "SH", ex2, _pruefer(), ausschluss, "l2", ziel_land="Schleswig-Holstein")
    assert ex2.aufrufe == 1 and storage.kanzlei_fuer_kandidat(kid).status == Status.NEU


def test_unerwarteter_fehler_blockiert_den_lauf_nicht(storage, ausschluss, monkeypatch):
    """Ein Kandidat mit unerwartetem Fehler (z. B. in der Vorprüfung) hielt den ganzen Lauf an; jeder
    Neustart scheiterte wieder an ihm. Jetzt: Kandidat als Fehler markiert, Lauf geht weiter."""
    import recherche.extract.lauf as lauf

    kaputt = _kandidat_mit_seiten(storage, "foerde-steuer.example")
    gut = _kandidat_mit_seiten(storage, "hansen-steuerberatung.example")
    original = lauf.zusammenfuehren

    def zusammenfuehren(k, *a, **kw):
        if k.id == kaputt:
            raise KeyError("seltsame Seite")
        return original(k, *a, **kw)

    monkeypatch.setattr(lauf, "zusammenfuehren", zusammenfuehren)
    meldungen = []
    stat = run_extract(storage, "SH", AufzeichnungsExtractor(), _pruefer(), ausschluss, "l1",
                       ziel_land="Schleswig-Holstein", fortschritt=meldungen.append)
    assert (stat.fertig, stat.fehler) == (1, 1)
    assert storage.kanzlei_fuer_kandidat(gut) is not None
    assert storage.get_kandidat(kaputt).extract_status == SchrittStatus.FEHLER
    assert "KeyError" in storage.get_kandidat(kaputt).fehler and any("übersprungen" in m for m in meldungen)


def test_fehler_in_der_vorpruefung_faellt_auf_llm_zurueck(storage, ausschluss, monkeypatch):
    import recherche.extract.lauf as lauf

    kid = _kandidat_mit_seiten(storage, "hansen-steuerberatung.example")
    original, aufrufe = lauf.zusammenfuehren, []

    def zusammenfuehren(k, funde, seiten, regel, llm, *a, **kw):
        aufrufe.append(llm is not None)
        if llm is None:
            raise ValueError("Vorprüfung kaputt")
        return original(k, funde, seiten, regel, llm, *a, **kw)

    monkeypatch.setattr(lauf, "zusammenfuehren", zusammenfuehren)
    ex = AufzeichnungsExtractor()
    run_extract(storage, "SH", ex, _pruefer(), ausschluss, "l1", ziel_land="Schleswig-Holstein")
    assert ex.aufrufe == 1 and aufrufe == [False, True]
    assert storage.kanzlei_fuer_kandidat(kid).kanzleiname


@pytest.mark.parametrize("impressum", [
    # PLZ und Ort in getrennten Zeilen – die strenge Adressregel findet nichts, das LLM schon
    ["Impressum", "Steuerkanzlei Steusloff", "Musterweg 3", "10115", "Berlin", "Telefon 030 1234567"],
    ["Impressum", "Steuerkanzlei Steusloff · Musterweg 3 · 10115 Berlin Mitte bei uns"],
])  # fmt: skip
def test_ohne_llm_auch_ohne_regel_adresse(storage, ausschluss, impressum):
    """steusloff.tax: Impressum mit PLZ außerhalb, aber die Regel-Adresse fehlte → bisher immer LLM (10 s)."""
    from recherche.models import Seite

    kid = storage.add_kandidat(Kandidat(bundesland="SH", domain="steusloff.example", website="https://steusloff.example/",
                                        crawl_status=SchrittStatus.FERTIG))
    html = "".join(f"<p>{z}</p>" for z in impressum)
    s = Seite(url="https://steusloff.example/impressum", domain="steusloff.example", seitentyp=Seitentyp.IMPRESSUM,
              http_status=200, html=html, text="\n".join(impressum))
    storage.save_seite(s)
    storage.setze_kandidat_seite(kid, Seitentyp.IMPRESSUM, s.url)
    berlin = PlzPruefer(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=[{"name": "Berlin", "federalState": {"name": "Berlin"}}]))))
    ex, zeilen = AufzeichnungsExtractor(), []
    stat = run_extract(storage, "SH", ex, berlin, ausschluss, "l1", ziel_land="Schleswig-Holstein",
                       fortschritt=zeilen.append)
    k = storage.kanzlei_fuer_kandidat(kid)
    assert (ex.aufrufe, stat.ohne_llm_ausserhalb, k.status) == (0, 1, Status.AUSSERHALB)
    assert "ohne LLM: PLZ 10115" in k.abweichung and "ohne LLM" in zeilen[0]


def test_versicherung_ausserhalb_reicht_nicht(storage, ausschluss):
    """SH-Kanzlei, deren eigene Adresse die Regel nicht findet, mit Versicherung in Hannover: LLM entscheidet."""
    from recherche.extract.lauf import plz_in_seiten
    from recherche.models import Seite

    zeilen = ["Impressum", "Kanzlei Nord", "24103", "Kiel", "Berufshaftpflichtversicherung:",
              "HDI Versicherung AG, HDI-Platz 1, 30659 Hannover"]
    s = Seite(url="https://n.example/impressum", domain="n.example", seitentyp=Seitentyp.IMPRESSUM, text="\n".join(zeilen))
    assert plz_in_seiten({Seitentyp.IMPRESSUM: s}, ausschluss) == ["24103"]  # Versicherungsabschnitt ist fremd


def test_ohne_impressum_gelesen_steht_in_abweichung(storage, ausschluss):
    """Prüfkatalog 3: ohne Impressum aus Start-/Kontaktseite gelesen → Hinweis in „Abweichung"."""
    domain = "nordlicht-steuerberatung.example"
    fx = Fixture.laden(BSP / domain)
    kid = storage.add_kandidat(Kandidat(bundesland="SH", domain=domain, website=f"https://{domain}/",
                                        crawl_status=SchrittStatus.FERTIG))
    for typ, s in fx.seiten.items():
        if typ != Seitentyp.IMPRESSUM:
            storage.save_seite(s)
            storage.setze_kandidat_seite(kid, typ, s.url)
    run_extract(storage, "SH", None, _pruefer(), ausschluss, "lauf")
    assert "Kein Impressum: Daten aus Start-/Kontaktseite" in storage.kanzleien("SH")[0].abweichung
