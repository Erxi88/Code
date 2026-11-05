"""Stichprobe Seed 3 (04.11.2026): Ansprechpartner – Webagentur-Block, Namensliste hinter der Berufsbezeichnung,
einzige genannte Person einer Einzelkanzlei. Fiktive Nachbauten der Befunde."""

from recherche.extract.base import LlmErgebnis, LlmPerson
from recherche.extract.lauf import regeln_fuer
from recherche.extract.merge import zusammenfuehren
from recherche.extract.personen import erkenne_personen
from recherche.extract.rules import Ausschluss
from recherche.models import Kandidat, Seite, Seitentyp
from recherche.normalize import PlzPruefung

KOPF = ["Impressum", "Steuerkanzlei Nordlicht", "Markt 1", "24103 Kiel", "Telefon 0431 900000"]


def _merge(zeilen, llm=None, domain="nordlicht.example"):
    from pathlib import Path

    seiten = {Seitentyp.IMPRESSUM: Seite(url=f"https://{domain}/impressum", domain=domain,
                                         seitentyp=Seitentyp.IMPRESSUM, text="\n".join(zeilen),
                                         html="<body>" + "".join(f"<p>{z}</p>" for z in zeilen) + "</body>")}
    aus = Ausschluss.laden(Path(__file__).parent.parent / "config" / "impressum_ausschluss.yaml")
    return zusammenfuehren(Kandidat(id=1, bundesland="SH", domain=domain), [], seiten, regeln_fuer(seiten, aus, domain),
                           llm or LlmErgebnis(), lambda p, o: PlzPruefung(True, "Schleswig-Holstein", ort_kanonisch=o),
                           aus)


def test_geschaeftsfuehrer_der_webagentur_ist_kein_ansprechpartner():
    """jaich-vangerow.de: „Webdesign und Realisierung" ⏎ Agentur ⏎ „Geschäftsführer: …" – auch nicht über das LLM."""
    agentur = ["Zuständige Kammer: Steuerberaterkammer Schleswig-Holstein", "Webdesign und Realisierung",
               "Muster Medien GmbH", "Geschäftsführer: Kai Webmann", "Hafenstraße 9", "24103 Kiel"]
    for block in (agentur, ["Agentur", "Muster Medien GmbH", "Geschäftsführer: Kai Webmann"]):
        assert _merge(KOPF + block).ansprechpartner == []
        k = _merge(KOPF + block, LlmErgebnis(kanzleiname="Steuerkanzlei Nordlicht",
                                             personen=[LlmPerson(name="Kai Webmann", funktion="Geschäftsführer")]))
        assert k.ansprechpartner == []


def test_geschaeftsfuehrer_hinter_kammer_adresse_bleibt():
    """Gegenprobe: „Geschäftsführer: …" direkt hinter dem Kammer-Block ist die Kanzlei."""
    k = _merge(KOPF[:2] + ["Nordlicht Steuerberatungsgesellschaft mbH", *KOPF[2:],
                           "Zuständige Kammer: Steuerberaterkammer Schleswig-Holstein", "Hopfenstraße 2d", "24114 Kiel",
                           "Geschäftsführer: Steuerberaterin Ina Lund"])
    assert [p.name for p in k.ansprechpartner] == ["Ina Lund"]


def test_namensliste_hinter_berufsbezeichnung():
    """steuerkanzlei-mbm.de: „Steuerberater Christian Backmann, Torsten Mitterhuber" in einer Zeile."""
    assert [(p.name, p.funktion) for p in erkenne_personen("Steuerberater Ole Rau, Eva Lund")] == [
        ("Ole Rau", "Steuerberater"), ("Eva Lund", "Steuerberater")]
    assert [p.name for p in erkenne_personen("Steuerberater: Ole Rau und Eva Lund")] == ["Ole Rau", "Eva Lund"]
    k = _merge(KOPF + ["Steuerberater Ole Rau, Eva Lund", "Zuständige Kammer: Steuerberaterkammer Schleswig-Holstein"],
               LlmErgebnis(kanzleiname="Steuerkanzlei Nordlicht"))
    assert [p.name for p in k.ansprechpartner] == ["Ole Rau", "Eva Lund"]


def test_einzige_genannte_person_einer_einzelkanzlei():
    """andruleit.de: Name ohne Berufstitel und ohne Schlüsselwort, keine weitere Person – diese Person."""
    k = _merge(["Impressum", "Steuerbüro Rau", "Dörte Rau", "Markt 1", "24103 Kiel", "Telefon 0431 900000",
                "Zuständige Kammer: Steuerberaterkammer Schleswig-Holstein"], LlmErgebnis(kanzleiname="Steuerbüro Rau"))
    assert [(p.name, p.funktion) for p in k.ansprechpartner] == [("Dörte Rau", "einzige genannte Person (Einzelkanzlei)")]
    # zwei genannte Personen ohne Funktion: keine Wahl
    k = _merge(["Impressum", "Steuerbüro Rau", "Dörte Rau", "Markt 1", "24103 Kiel"],
               LlmErgebnis(kanzleiname="Steuerbüro Rau", personen=[LlmPerson(name="Jan Ott", funktion="Sekretariat")]))
    assert k.ansprechpartner == []


# ---------------------------------------------------------------------- an den echten Seiten (fixture-eval 05.11.2026)


def _merge_mit_start(impressum, start, llm=None, domain="nordlicht.example"):
    """Impressum und Startseite – Zeilen auf beiden Seiten sind Rahmen (Seitenleiste/Fußzeile)."""
    from pathlib import Path

    def seite(typ, zeilen):
        return Seite(url=f"https://{domain}/{typ.value}", domain=domain, seitentyp=typ, text="\n".join(zeilen),
                     html="<body>" + "".join(f"<p>{z}</p>" for z in zeilen) + "</body>")

    seiten = {Seitentyp.IMPRESSUM: seite(Seitentyp.IMPRESSUM, impressum), Seitentyp.START: seite(Seitentyp.START, start)}
    aus = Ausschluss.laden(Path(__file__).parent.parent / "config" / "impressum_ausschluss.yaml")
    return zusammenfuehren(Kandidat(id=1, bundesland="SH", domain=domain), [], seiten, regeln_fuer(seiten, aus, domain),
                           llm or LlmErgebnis(), lambda p, o: PlzPruefung(True, "Schleswig-Holstein", ort_kanonisch=o),
                           aus)


def test_gesetzliche_vertreter_mit_titeln_davor_und_dahinter():
    """jaich-vangerow.de: „Gesetzliche Vertreter:" ist ein Schlüsselwort, kein Name; Titel und Berufskürzel vor und
    hinter dem Namen („Dipl.-Kaufm. … StB", „… B.A., StBin")."""
    k = _merge(["Impressum", "Rau und Ott", "Steuerberatungsgesellschaft mbH", "Markt 1", "24376 Kappeln",
                "Telefon: 04642 900000", "Gesetzliche Vertreter:", "Dipl.-Kaufm. Ole Rau StB",
                "Dipl.-Betriebsw. (FH), MBA Eva Ott StB", "Mia Rau B.A., StBin",
                "Handelsregistereintrag: Amtsgericht Flensburg, HRB 900KA"])
    assert [p.name for p in k.ansprechpartner] == ["Ole Rau", "Eva Ott", "Mia Rau"]
    assert all("Gesetzliche" not in p.name for p in k.ansprechpartner)


def test_verantwortlich_mit_name_in_der_seitenleiste():
    """andruleit.de: „Verantwortlich:" im Inhalt, Name und Anschrift darunter stehen als Seitenleiste auf jeder Seite."""
    leiste = ["Dörte Rau", "Buschberg 25", "24161 Altenholz"]
    k = _merge_mit_start(["Datenschutzerklärung und Impressum", "RAU Steuerberatungsgesellschaft", "Verantwortlich:",
                          *leiste, "Telefon +494319000000"], ["Rau Steuerberater", "Kontakt", *leiste],
                         LlmErgebnis(kanzleiname="RAU Steuerberatungsgesellschaft"))
    assert [p.name for p in k.ansprechpartner] == ["Dörte Rau"]


def test_namensliste_als_zweite_kopfzeile():
    """steuerkanzlei-mbm.de: „Steuerkanzlei Rau · Ott" ⏎ „Ole Rau, Eva Ott", Berufsbezeichnung im Kammer-Block."""
    k = _merge(["Impressum", "Steuerkanzlei Rau · Ott", "Ole Rau, Eva Ott", "Steuerkanzlei Rau · Ott", "Markt 1",
                "23564 Lübeck", "Telefon: 0451 900000", "Berufsbezeichnung:", "Steuerberater",
                "Zuständige Kammer:", "Steuerberaterkammer Schleswig-Holstein"],
               LlmErgebnis(kanzleiname="Steuerkanzlei Rau · Ott"))
    assert [p.name for p in k.ansprechpartner] == ["Ole Rau", "Eva Ott"]


def test_firmierung_unter_impressum_auch_als_fusszeile():
    """panares-nord.de: „Impressum" ⏎ „PANARES nord GmbH" (auch Fußzeile jeder Seite), HRB im Impressum – das LLM nannte
    „Wirtschaftsprüfer Ralf Helfmann" (Geschäftsführer) → Firmierung, Rechtsform GmbH, Helfmann Ansprechpartner."""
    from recherche.extract.merge import firmierung_unter_impressum

    fuss = ["Nordlicht nord GmbH", "Hof Siek 7", "24250 Bothkamp"]
    impressum = ["Impressum", *fuss, "04302 900000", "Geschäftsführer: Ralf Rau", "HRB 9000 Registergericht Kiel", *fuss]
    assert firmierung_unter_impressum("\n".join(impressum)) == "Nordlicht nord GmbH"
    assert firmierung_unter_impressum("\n".join(i for i in impressum if not i.startswith("HRB"))) is None  # ohne Register
    assert firmierung_unter_impressum("Impressum\nETL NORD\nSteuerberatungsgesellschaft mbH\nHRB 1 KI") == \
        "ETL NORD Steuerberatungsgesellschaft mbH"
    for llm_name in (None, "Wirtschaftsprüfer Ralf Rau"):
        k = _merge_mit_start(impressum, ["Willkommen", *fuss], LlmErgebnis(kanzleiname=llm_name))
        assert (k.kanzleiname, k.rechtsform) == ("Nordlicht nord GmbH", "GmbH"), llm_name
        assert k.hauptansprechpartner.name == "Ralf Rau"
