"""robots.txt nach RFC 9309 gegen reale Muster (WordPress, Yoast, Cloudflare, KI-Bot-Sperren)."""

import pytest

from recherche.robots import Robots, produkt_token

UA = "kanzlei-recherche/0.1 (Recherche-Tool; Kontakt siehe README)"

WORDPRESS = "User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n\nSitemap: https://a.example/wp-sitemap.xml"
YOAST_LEER = "User-agent: *\nDisallow:\n\nSitemap: https://a.example/sitemap_index.xml"
NUR_KI_BOTS = ("User-agent: GPTBot\nDisallow: /\n\nUser-agent: ClaudeBot\nDisallow: /\n\nUser-agent: CCBot\n"
               "Disallow: /\n\nUser-agent: *\nAllow: /")
KI_BOTS_OHNE_STERN = "User-agent: GPTBot\nUser-agent: CCBot\nUser-agent: ClaudeBot\nDisallow: /"
CLOUDFLARE = ("# As a condition of accessing this website, you agree to abide by the following content signals:\n"
              "User-agent: *\nContent-Signal: search=yes,ai-train=no\nAllow: /\n\n"
              "User-agent: Amazonbot\nDisallow: /\n\nUser-agent: Bytespider\nDisallow: /\n\nUser-agent: GPTBot\nDisallow: /")
ALLES_GESPERRT = "User-agent: *\nDisallow: /"
PORTAL = "User-agent: Googlebot\nAllow: /\n\nUser-agent: bingbot\nAllow: /\n\nUser-agent: *\nDisallow: /"


@pytest.mark.parametrize("name,text,start,impressum", [
    ("WordPress-Standard", WORDPRESS, True, True),
    ("Yoast, Disallow leer", YOAST_LEER, True, True),
    ("nur KI-Bots gesperrt", NUR_KI_BOTS, True, True),
    ("KI-Bots ohne *-Gruppe", KI_BOTS_OHNE_STERN, True, True),
    ("Cloudflare Content-Signals", CLOUDFLARE, True, True),
    ("alles gesperrt", ALLES_GESPERRT, False, False),
    ("Portal: nur Suchmaschinen", PORTAL, False, False),
    ("BOM und CRLF", "﻿User-agent: *\r\nDisallow: /wp-admin/\r\n", True, True),
    ("HTML statt robots.txt", "<!doctype html><html><body>Willkommen</body></html>", True, True),
    ("leer", "", True, True),
])  # fmt: skip
def test_reale_muster(name, text, start, impressum):
    r = Robots.parse(text)
    assert r.erlaubt(UA, "https://a.example/") is start, name
    assert r.erlaubt(UA, "https://a.example/impressum") is impressum, name


def test_eigene_gruppe_genau_nach_produkt_token():
    # „re" ist ein Teilstring von „kanzlei-recherche" – darf nicht treffen (urllib.robotparser tat es)
    assert Robots.parse("User-agent: re\nDisallow: /\n\nUser-agent: *\nDisallow:").erlaubt(UA, "https://a.example/")
    eigene = "User-agent: Kanzlei-Recherche\nDisallow: /\n\nUser-agent: *\nAllow: /"
    assert not Robots.parse(eigene).erlaubt(UA, "https://a.example/")
    assert produkt_token(UA) == "kanzlei-recherche"


def test_laengste_regel_gewinnt_und_wildcards():
    r = Robots.parse("User-agent: *\nDisallow: /\nAllow: /$\nAllow: /impressum\nDisallow: /*.pdf$\nDisallow: /*?")
    assert r.erlaubt(UA, "https://a.example/")
    assert r.erlaubt(UA, "https://a.example/impressum/")
    assert not r.erlaubt(UA, "https://a.example/team")
    assert not r.erlaubt(UA, "https://a.example/preise.pdf")
    # Länge des Musters entscheidet: „Allow: /impressum" (10) schlägt „Disallow: /*.pdf$" (7)
    assert r.erlaubt(UA, "https://a.example/impressum.pdf")
    gleich = Robots.parse("User-agent: *\nDisallow: /team\nAllow: /team")
    assert gleich.erlaubt(UA, "https://a.example/team")  # Gleichstand: Allow


def test_mehrere_gruppen_fuer_stern_werden_vereinigt():
    r = Robots.parse("User-agent: *\nDisallow: /intern/\n\nUser-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /tmp/")
    assert not r.erlaubt(UA, "https://a.example/intern/x") and not r.erlaubt(UA, "https://a.example/tmp/x")
    assert r.erlaubt(UA, "https://a.example/impressum")
