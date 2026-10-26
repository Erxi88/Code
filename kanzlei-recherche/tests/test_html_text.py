from recherche.crawl import cf_email_decode, haupttext, html_zu_text, links

HTML = """<html><head><title>T</title><script>var x = "geheim";</script><style>p{}</style></head>
<body><nav><a href="/">Start</a><a href="/team/">Unser Team</a></nav>
<main><h1>Impressum</h1><p>Hansen &amp; Petersen<br>Holstenstraße&nbsp;42<br>24103 Kiel</p>
<!-- Kommentar --><table><tr><td>Tel.</td><td>0431 1</td></tr></table></main>
<footer><span>Fußzeile</span> <a href="mailto:info@a.example">Mail</a> <a href="tel:+49431999">Anruf</a></footer>
<noscript>Bitte JavaScript aktivieren</noscript></body></html>"""


def test_text_mit_zeilen_navigation_und_fusszeile():
    text = html_zu_text(HTML)
    zeilen = text.splitlines()
    assert "Holstenstraße 42" in zeilen and "24103 Kiel" in zeilen
    assert "Hansen & Petersen" in zeilen
    assert any("Unser Team" in z for z in zeilen)  # Navigation bleibt
    assert any("Fußzeile" in z for z in zeilen)  # Fußzeile bleibt
    assert "Tel. 0431 1" in zeilen
    assert "geheim" not in text and "Kommentar" not in text and "JavaScript aktivieren" not in text


def test_cloudflare_email():
    kodiert = "42" + "".join(f"{ord(c) ^ 0x42:02x}" for c in "info@a.example")
    assert cf_email_decode(kodiert) == "info@a.example"
    html = f'<body><a href="/cdn-cgi/l/email-protection#{kodiert}"><span data-cfemail="{kodiert}">[email protected]</span></a></body>'
    assert "info@a.example" in html_zu_text(html)
    assert ("mailto:info@a.example", "info@a.example") in links(html, "https://a.example/")
    assert cf_email_decode("zz") is None


def test_links_absolut_mit_mailto_und_tel():
    lnks = dict(links(HTML, "https://www.a.example/impressum/"))
    assert lnks["https://www.a.example/team/"] == "Unser Team"
    assert "mailto:info@a.example" in lnks and "tel:+49431999" in lnks


def test_haupttext_ohne_navigation():
    text = haupttext("<html><body><nav>Menü Startseite Kontakt</nav><article><h1>Unser Team</h1>"
                     + "<p>Jens Hansen, Steuerberater, berät seit vielen Jahren Unternehmen in Kiel.</p>" * 3
                     + "</article><footer>Fußzeile</footer></body></html>")
    assert text and "Jens Hansen" in text and "Menü" not in text
