import httpx

from recherche.normalize import PlzPruefer

KIEL = [{"postalCode": "24103", "name": "Kiel", "municipality": {"key": "01002000", "name": "Kiel, Landeshauptstadt"},
         "federalState": {"key": "01", "name": "Schleswig-Holstein"}}]
STPO = [{"postalCode": "25826", "name": "St. Peter-Ording", "federalState": {"name": "Schleswig-Holstein"}}]


def _pruefer(antworten, cache=None):
    anfragen = []

    def handler(req):
        anfragen.append(str(req.url))
        a = antworten.get(req.url.params["postalCode"], (200, []))
        if isinstance(a, Exception):
            raise a
        return httpx.Response(a[0], json=a[1])

    return PlzPruefer("https://openplzapi.org", cache=cache, client=httpx.Client(transport=httpx.MockTransport(handler))), anfragen


def test_passend():
    p, anfragen = _pruefer({"24103": (200, KIEL)})
    e = p.pruefe("24103", "Kiel")
    assert (e.verifiziert, e.bundesland) == (True, "Schleswig-Holstein")
    assert anfragen[0].startswith("https://openplzapi.org/de/Localities?postalCode=24103")


def test_schreibweisen():
    p, _ = _pruefer({"24103": (200, KIEL), "25826": (200, STPO)})
    assert p.pruefe("24103", "Kiel, Landeshauptstadt").verifiziert
    assert p.pruefe("25826", "Sankt Peter-Ording").verifiziert


def test_ort_passt_nicht():
    p, _ = _pruefer({"24103": (200, KIEL)})
    e = p.pruefe("24103", "Lübeck")
    assert e.verifiziert is False and "passt nicht" in e.hinweis
    assert e.bundesland == "Schleswig-Holstein"


def test_unbekannte_und_ungueltige_plz():
    p, _ = _pruefer({})
    assert p.pruefe("00000", "Nirgendwo").verifiziert is False
    assert p.pruefe("2410", "Kiel").verifiziert is False
    assert p.pruefe(None, "Kiel").verifiziert is None


def test_nicht_erreichbar_ist_nicht_verifiziert():
    p, _ = _pruefer({"24103": httpx.ConnectError("x")})
    e = p.pruefe("24103", "Kiel")
    assert e.verifiziert is None and e.hinweis == "OpenPLZ nicht erreichbar"


def test_eine_anfrage_je_plz_und_cache(storage):
    p, anfragen = _pruefer({"24103": (200, KIEL)}, cache=storage)
    p.pruefe("24103", "Kiel")
    p.pruefe("24103", "Kiel")
    assert len(anfragen) == 1
    p2, anfragen2 = _pruefer({}, cache=storage)
    assert p2.pruefe("24103", "Kiel").verifiziert and anfragen2 == []


def test_schreibweise_laut_openplz():
    ahrensburg = [{"postalCode": "22926", "name": "Ahrensburg", "federalState": {"name": "Schleswig-Holstein"}}]
    wyk = [{"postalCode": "25938", "name": "Wyk auf Föhr", "federalState": {"name": "Schleswig-Holstein"}},
           {"postalCode": "25938", "name": "Nieblum", "federalState": {"name": "Schleswig-Holstein"}}]
    p, _ = _pruefer({"22926": (200, ahrensburg), "25938": (200, wyk), "24103": (200, KIEL)})
    assert p.pruefe("22926", "AHRENSBURG").ort_kanonisch == "Ahrensburg"
    assert p.pruefe("22926", "Ahrensburg").ort_kanonisch is None  # schon richtig
    e = p.pruefe("25938", "Wyk/Insel Föhr")
    assert (e.verifiziert, e.ort_kanonisch) == (True, "Wyk auf Föhr")
    assert p.pruefe("24103", "Kielbrücke").verifiziert is False  # erstes Wort anders („kielbruecke")


def test_telefon_wird_beim_laden_neu_normalisiert(storage):
    from recherche.models import Kanzlei

    storage.upsert_kanzlei(Kanzlei(lauf_bundesland="SH", domain="a.example", telefon="+494513888100"))
    assert storage.get_kanzlei("a.example").telefon == "+49 451 3888100"
