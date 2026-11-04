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
