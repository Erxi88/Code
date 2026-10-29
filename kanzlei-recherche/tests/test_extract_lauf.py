
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
from recherche.models import Fund, Kandidat, Konfidenz, SchrittStatus, Status
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
    assert (o.kanzleiname, o.website_vorhanden, o.konfidenz) == ("Kanzlei Ohneweb", False, Konfidenz.MITTEL)
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
