"""Zusammenführung von Discovery, Regeln und LLM zu einer Kanzlei.

Vorrang:
- Adresse (als Einheit Straße/PLZ/Ort) und Telefon: Discovery (Stufe B) > Regeln > LLM
- Name: Discovery (Stufe B) > LLM. Websuche-Titel gelten NICHT als Discovery-Name.
- E-Mail: Regeln > LLM; Rechtsform: LLM > aus dem Namen
- Discovery-Daten werden nie überschrieben. Unterscheiden sich Discovery und Website,
  steht ein Hinweis in „Abweichung".

Ansprechpartner sind nur Berufsträger und Leitung (aus LLM, Regel-Erkennung der Teamseiten und
den Vertretungsberechtigten im Impressum); die Teamgröße zählt alle Personen. Rechtsform und Ort
werden kanonisch ausgegeben (config/rechtsformen.yaml, OpenPLZ-Ortsname).
Konfidenz nach der (vorläufigen) Regel aus CLAUDE.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from ..crawl import html_zu_text
from ..models import (
    Ansprechpartner,
    Fund,
    Kandidat,
    Kanzlei,
    Konfidenz,
    Seite,
    Seitentyp,
    TeamKlasse,
)
from ..normalize import (
    PlzPruefung,
    normalize_rechtsform,
    normalize_telefon,
    rechtsform_aus_text,
)
from ..search.dedupe import adresse_key, name_tokens, telefon_key
from ..textnorm import text_normalisieren
from .base import LlmErgebnis, LlmPerson, RegelErgebnis
from .personen import (
    _KEINE_NAMEN as _MENUEWOERTER,
)
from .personen import (
    ErkanntePerson,
    Mitarbeiterzahl,
    erkenne_personen,
    ist_berufstraeger_funktion,
    ist_externe_funktion,
    ist_leitung_funktion,
    mitarbeiterzahl,
    name_zerlegen,
    personen_aus_attributen,
    personen_aus_json_ld,
)
from .prompt import seiten_rahmen
from .rules import Ausschluss, strasse_zerlegen


def _text(s: Seite) -> str:
    return html_zu_text(s.html) if s.html else (text_normalisieren(s.text) or "")

# keine Berufsträger, obwohl „Steuerberater" im Wort steckt
_KEIN_BERUFSTRAEGER = re.compile(
    r"\w*fach(?:angestellt|wirt|assistent|gehilf|kraft)\w*|"  # Rechtsanwaltsfachangestellte, Steuerfachwirt …
    r"(?:steuerberater(?:in)?|stb|wp|ra)[-\s]?(?:anwärter|assistent)\w*|angehende[rn]?\s+steuerberater\w*|"
    r"(?:ausbildung|prüfung|vorbereitung)\s+zu[mr]?\s+steuerberater\w*",
    re.I,
)
_KLASSEN = list(TeamKlasse)
_TITEL = re.compile(r"\b(dr|prof|dipl|mag|ll\.?m|m\.?a|m\.?sc|b\.?a|b\.?sc|rer|pol|oec|kfm|kffr|ing|jur)\b\.?", re.I)


def _stufe_b_quellen() -> frozenset[str]:
    from ..search.registry import ADAPTER_KLASSEN

    return frozenset(n for n, k in ADAPTER_KLASSEN.items() if k.stufe == "B")


def discovery_werte(funde: list[Fund]) -> dict[str, str | None]:
    """Name, Adresse und Telefon aus Stufe-B-Funden (Places, Verzeichnisse); Websuche zählt nicht.

    Die Adresse wird als Einheit aus dem ersten Fund mit PLZ genommen.
    """
    b = [f for f in funde if f.quelle in _stufe_b_quellen()]
    werte: dict[str, str | None] = {"name": None, "strasse": None, "plz": None, "ort": None, "telefon": None,
                                    "quelle_name": None, "quelle_adresse": None, "quelle_telefon": None}
    for f in b:
        if werte["name"] is None and f.name:
            werte["name"], werte["quelle_name"] = f.name, f.quelle
        if werte["plz"] is None and f.plz:
            werte.update(strasse=f.strasse, plz=f.plz, ort=f.ort, quelle_adresse=f.quelle)
        if werte["telefon"] is None and f.telefon:
            werte["telefon"], werte["quelle_telefon"] = f.telefon, f.quelle
    return werte


def ist_berufstraeger(p: Ansprechpartner | LlmPerson) -> bool:
    """StB, WP, RA, vBP (laut config/personen_funktionen.yaml) – nicht Anwärter oder Assistenz."""
    text = _KEIN_BERUFSTRAEGER.sub(" ", " ".join(x for x in (p.funktion, p.titel) if x))
    return ist_berufstraeger_funktion(text)


_EHEMALIG = re.compile(r"[^,;]*(?:\bbis\s+(?:\d|zum|ende)|ehemalig|a\.\s?D\.|i\.\s?R\.)[^,;]*", re.I)


def ist_leitung(p: Ansprechpartner | LlmPerson) -> bool:
    """Partner, Geschäftsführer … – nicht „Partner bis 01.01.2025" oder „ehemaliger Partner"."""
    return ist_leitung_funktion(_EHEMALIG.sub(" ", p.funktion or ""))


def _namens_tokens(name: str) -> set[str]:
    name = _TITEL.sub(" ", name.lower())
    return {t for t in re.split(r"[^a-zäöüß]+", name) if len(t) >= 2}


_BERUFSWORT = re.compile(
    r"^(?:steuerberater(?:in)?|rechtsanw(?:alt|ältin)|wirtschaftsprüfer(?:in)?|stb|wp|ra|rain|vbp|frau|herr|herrn)$"
)
_PARTIKEL = frozenset({"von", "van", "de", "der", "den", "zu", "la", "le", "di", "da", "ten"})


def _name_teile(name: str) -> tuple[list[str], str] | None:
    """(Vornamen, Nachname) ohne Titel und Anrede; Initialen als „w."."""
    name = re.sub(r"^(?:frau|herr|herrn)\s+", "", _TITEL.sub(" ", name.lower()).strip(" .-,"))
    woerter = [w.strip(",;-") for w in re.split(r"\s+", name) if w.strip(",;.-")]
    # Berufsbezeichnungen und Kürzel sind keine Vornamen („Steuerberater Schreiner", „StB Slama")
    woerter = [w for w in woerter if w not in _PARTIKEL and re.search(r"[a-zäöüß]", w) and not _BERUFSWORT.match(w)]
    if not woerter:
        return None
    return [w if w.endswith(".") and len(w) == 2 else w.rstrip(".") for w in woerter[:-1]], woerter[-1].rstrip(".")


_TITEL_VORN = re.compile(r"^(?:(?:prof|dr|dipl|mag|rer|pol|oec|kfm|kffr|ing|jur|stb|wp|ra)\b\.?[-\w.]*\s*)+", re.I)


def name_reihenfolge(name: str) -> str:
    """„Schreiner, Andreas" / „Dipl.-Kfm. Schreiner, Andreas" → „Andreas Schreiner" (Titel bleiben vorn)."""
    if name.count(",") != 1:
        return name
    m = _TITEL_VORN.match(name)
    titel, rest = (m.group(0), name[m.end():]) if m else ("", name)
    nach, vor = (x.strip() for x in rest.split(","))
    if not nach or not vor or len(vor.split()) > 3 or len(nach.split()) > 2:
        return name
    return f"{titel}{vor} {nach}".strip()


def gleiche_person(a: str | None, b: str | None) -> bool:
    """Wie `_gleiche_person`, auch bei vertauschter Reihenfolge: „Schreiner, Andreas" und
    „Schreiner Andreas" = „Andreas Schreiner"."""
    if not a or not b:
        return False
    a, b = name_reihenfolge(a), name_reihenfolge(b)
    if _gleiche_person(a, b):
        return True
    # „Schreiner Andreas" ohne Komma: zwei Wörter vertauscht
    ta = _TITEL_VORN.sub("", a).split()
    return len(ta) == 2 and _gleiche_person(f"{ta[1]} {ta[0]}", b)


def _gleiche_person(a: str | None, b: str | None) -> bool:
    """Gleicher Nachname UND gleicher Vorname (oder passende Initiale: „W. Drews" = „Wiebke Drews").
    Nie nur über den Nachnamen: „W. Drews" ≠ „Sven Drews", „Drews" ≠ „Sven Drews"."""
    if not a or not b:
        return False
    ta, tb = _name_teile(a), _name_teile(b)
    if not ta or not tb or ta[1] != tb[1] or not ta[0] or not tb[0]:
        return False
    va, vb = ta[0], tb[0]
    voll_a = {v for v in va if not v.endswith(".")}
    voll_b = {v for v in vb if not v.endswith(".")}
    if voll_a & voll_b:
        return True
    # Vorname Teil des anderen: „Alexander Hamdorf" = „Johann-Alexander Hamdorf" (Doppelname, zweiter Vorname)
    teile_a = {x for v in voll_a for x in v.split("-") if len(x) >= 3}
    teile_b = {x for v in voll_b for x in v.split("-") if len(x) >= 3}
    if teile_a & teile_b:
        return True
    if voll_a and voll_b:
        return False  # „Jens Hansen" ≠ „Jan Hansen"
    # mindestens eine Seite nur mit Initiale(n): erste Vornamen müssen im Anfangsbuchstaben passen
    return va[0][0] == vb[0][0]


_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _email_passt(email: str, name: str) -> bool:
    """Lokaler Teil passt zum Namen: baur@, y.baur@, yvonne.baur@, ybaur@."""
    teile = _name_teile(name)
    if not teile:
        return False
    lokal = re.sub(r"[^a-zäöüß]", "", email.split("@", 1)[0].lower())
    nachname = re.sub(r"[^a-zäöüß]", "", teile[1])
    vorname = re.sub(r"[^a-zäöüß]", "", teile[0][0]) if teile[0] else ""
    return bool(nachname) and nachname in lokal or (len(vorname) > 2 and lokal.startswith(vorname))


def _ohne_fremde_email(name: str, funktion: str | None) -> str | None:
    """E-Mail-Adressen in der Funktion nur behalten, wenn sie zur Person passen."""
    if not funktion or "@" not in funktion:
        return funktion
    text = _EMAIL_IN_TEXT.sub(lambda m: m.group(0) if _email_passt(m.group(0), name) else "", funktion)
    return ", ".join(x for x in (s.strip() for s in re.split(r"\s*,\s*", text)) if x) or None


def _funktion_vereinigen(a: str | None, b: str | None) -> str | None:
    teile: list[str] = []
    for f in (a, b):
        for teil in re.split(r"\s*[,;/&]\s*|\s+und\s+", f or ""):
            kern = teil.lower().replace(" (laut impressum)", "")
            if teil and not any(kern == x.lower().replace(" (laut impressum)", "") for x in teile):
                teile.append(teil)
    return ", ".join(teile) or None


def personen_vereinigen(personen: list[LlmPerson]) -> list[LlmPerson]:
    """Mehrfache Nennungen derselben Person zusammenführen (Funktionen und Titel vereinigt).

    Das LLM nennt Partner oft zweimal – aus dem Impressum nur als „Partner", von der Teamseite
    als „Steuerberater, Partner". Würde die erste Nennung gewinnen, ginge die Berufsbezeichnung verloren.
    """
    ergebnis: list[LlmPerson] = []
    for p in personen:
        if ist_externe_funktion(p.funktion):
            continue  # Kundenstimme / Referenz („Geschäftsführer der Muster GmbH")
        p = LlmPerson(name=name_reihenfolge(p.name), titel=p.titel, funktion=_ohne_fremde_email(p.name, p.funktion))
        for i, q in enumerate(ergebnis):
            if gleiche_person(p.name, q.name):
                name = q.name if len(q.name) >= len(p.name) else p.name
                ergebnis[i] = LlmPerson(name=name, titel=q.titel or p.titel,
                                        funktion=_funktion_vereinigen(q.funktion, p.funktion))
                break
        else:
            ergebnis.append(p)
    # „Steuerberater Schreiner" (ohne Vornamen) neben „Andreas Schreiner": kein eigener Mensch
    def ohne_vorname(q: LlmPerson) -> bool:
        teile = _name_teile(q.name)
        return bool(teile) and not teile[0]

    ergebnis = [q for q in ergebnis if not (ohne_vorname(q) and any(
        not ohne_vorname(x) and (_name_teile(x.name) or ([], ""))[1] == _name_teile(q.name)[1] for x in ergebnis))]
    return ergebnis


# Starke Schlüsselwörter: Namen dürfen direkt folgen („vertreten durch Christian Wermke").
# Schwache („Partner", „Gesellschafter") nur als Label mit Doppelpunkt – sonst trifft die Navigation
# („Partner Berufsträger Firmenphilosophie Historie").
_VERTRETUNG_STARK = re.compile(
    r"vertretungs?berechtigt\w*|vertreten\s+durch|geschäftsführ\w*|geschaeftsfuehr\w*|inhaber\w*", re.I
)
# nicht in Domains und Firmennamen („bd-partner.de", „BD-Partner")
_VERTRETUNG_SCHWACH = re.compile(r"(?<![\w-])(?:gesellschafter\w*|partner(?:in|innen)?\b(?![.-]\w)|vorstand)", re.I)
# „Verantwortlich gemäß § 5 TMG: Olaf Müller-Madaus" – nachrangig nach den Vertretungsberechtigten
_VERANTWORTLICH = re.compile(r"\bverantwortlich\w*", re.I)
# Wörter, die selbst einen Fremd-Abschnitt kennzeichnen (Schlüsselwort-Zeile bleibt dann gesperrt)
_FREMD_WORT = re.compile(r"kammer|versicher|aufsicht|hoster|hosting|webdesign|datenschutzbeauftragt", re.I)
_LABEL = re.compile(r"^[^:]{2,60}:")  # „Registergericht: …" beendet einen Abschnitt
_BERUFSBEZEICHNUNG = re.compile(r"berufsbezeichnung\w*\s*:?\s*(.*)", re.I)
_BEZEICHNUNGEN = {
    "Steuerberater": re.compile(r"steuerberater", re.I),
    "Wirtschaftsprüfer": re.compile(r"wirtschaftspr[uü]fer", re.I),
    "Rechtsanwalt": re.compile(r"rechtsanw[aä]lt", re.I),
    "vereidigter Buchprüfer": re.compile(r"buchpr[uü]fer", re.I),
}
ABSCHNITT_ZEILEN = 8


_KLAMMER = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")


def _klammer_teilen(teil: str) -> list[str]:
    return [m.group(1), m.group(2)] if (m := _KLAMMER.match(teil)) else [teil]


def _namen_mit_funktion(text: str) -> list[tuple[str, str | None]]:
    """„die Partner: Dipl.-Kfm. Jens Hansen, Maren Petersen und Ole Brandt" → Namen;
    „Yvonne Baur, Steuerberaterin / Sven Drews, Steuerberater" → Namen mit Berufsbezeichnung."""
    text = text.rsplit(":", 1)[-1]
    gefunden: list[tuple[str, str | None]] = []
    teile = [x for x in re.split(r"\s*[,;]\s*|\s+/\s+|\s+und\s+|\s+sowie\s+|\s*&\s*", text) if x.strip()]
    # „Yvonne Baur (Steuerberaterin)" → Name und Funktion als eigene Teile
    teile = [x for teil in teile for x in _klammer_teilen(teil)]
    for k, teil in enumerate(teile):
        woerter = teil.split()
        for start in range(min(3, len(woerter))):  # einleitende Wörter („die Partner") überspringen
            if nt := name_zerlegen(" ".join(woerter[start:])):
                folge = teile[k + 1] if k + 1 < len(teile) else ""
                funktion = folge if folge and not name_zerlegen(folge) and ist_berufstraeger_funktion(folge) else None
                gefunden.append((nt[0], funktion))
                break
    return gefunden


def _namen_aus(text: str) -> list[str]:
    return [n for n, _ in _namen_mit_funktion(text)]


# Überschrift ohne Doppelpunkt: „Vertretungsberechtigte Gesellschafter" – danach nur Rollenwörter
_ROLLENWORT = re.compile(
    r"^(?:gesellschafter\w*|geschäftsführ\w*|geschaeftsfuehr\w*|partner\w*|vorstand\w*|inhaber\w*|"
    r"person(?:en)?|vertret\w*|sind|ist|die|der|des|und|durch|mitglieder?|kanzlei|"
    r"inhaltlich|redaktionell|presserechtlich|journalistisch|verantwortlich\w*|für|den|inhalt|seite)$",
    re.I,
)


def _schluessel(z: str) -> tuple[re.Match, bool] | None:
    """(Treffer, stark). Schwache Schlüsselwörter („Partner", „Gesellschafter") zählen ohne Doppelpunkt
    nur mit Namen in derselben Zeile oder als reine Überschrift („Die Partner")."""
    if m := _VERTRETUNG_STARK.search(z):
        return m, True
    if m := _VERTRETUNG_SCHWACH.search(z):
        return m, False
    return None


_GENDER_KLAMMER = re.compile(r"\((?:r|e|in|innen|n)\)", re.I)  # „Vertretungsberechtigte(r)", „Partner(in)"
# Paragrafenangabe hinter dem Schlüsselwort: „gemäß § 18 Abs. 2 MStV", „nach § 5 TMG", „im Sinne des DDG"
_RECHTSGRUNDLAGE = re.compile(
    r"\b(?:gem(?:äß|aess|\.)|nach|i\.?\s?s\.?\s?(?:d|v)\.?|im\s+sinne\s+de[rs]|laut)\b.*$|§.*$", re.I
)


def _nur_rollenwoerter(text: str) -> bool:
    text = _GENDER_KLAMMER.sub("", text)
    return all(_ROLLENWORT.match(w.strip(",.:()")) for w in text.split() if w.strip(",.:()"))


def _wie_navigation(text: str) -> bool:
    """Mehrere Menüwörter („Geschäftsführung Kontakt Impressum") – keine Überschrift eines Abschnitts."""
    woerter = [w.strip(",.:") for w in text.lower().split()]
    return sum(1 for w in woerter if w in _MENUEWOERTER and not _ROLLENWORT.match(w)
               and not w.startswith("vertretungsberechtigt")) >= 2  # fmt: skip


def _abschnitt_namen(
    zeilen: list[str], i: int, m: re.Match, stark: bool, gesperrt: Callable[[int], bool]
) -> tuple[list[tuple[str, str | None]], str]:
    """Namen hinter dem Schlüsselwort in Zeile i – oder, wenn dort keine stehen und die Zeile eine
    Überschrift ist, in den Folgezeilen bis zur ersten Leerzeile oder Zeile ohne Namen.
    Gibt (Namen, Begründung fürs Protokoll) zurück."""
    z = zeilen[i]
    if namen := _namen_mit_funktion(z[m.end():]):
        return namen, f"{len(namen)} Namen in der Zeile"
    rest = _RECHTSGRUNDLAGE.sub("", _GENDER_KLAMMER.sub("", z[m.end():])).strip(" :.")
    label = ":" in z[m.end():] or z.rstrip().endswith(":")
    ueberschrift = _nur_rollenwoerter(rest) and _nur_rollenwoerter(z[: m.start()])
    if stark:
        ueberschrift = ueberschrift or (len(rest.split()) <= 8 and not re.search(r"\d", rest) and not _wie_navigation(z))
    if not (label or ueberschrift):
        return [], "keine Namen, keine Überschrift"
    namen = []
    for j in range(i + 1, min(len(zeilen), i + 9)):
        folge = zeilen[j].strip()
        if not folge or gesperrt(j) or _FREMD_WORT.search(folge):
            break
        # neue Schlüsselwort-Zeile („Geschäftsführer: …") beginnt einen eigenen Abschnitt –
        # eine Namenszeile mit Funktion am Ende („Yvonne Baur, Geschäftsführerin") nicht
        if (n := _schluessel(folge)) and _nur_rollenwoerter(folge[: n[0].start()]):
            break
        if (n := _VERANTWORTLICH.search(folge)) and _nur_rollenwoerter(folge[: n.start()]):
            break
        gefunden = _namen_mit_funktion(folge)
        if not gefunden:
            break
        namen += gefunden
    return namen, (f"{len(namen)} Namen in den Folgezeilen" if namen else "Überschrift, aber keine Namen darunter")


@dataclass
class Vertretung:
    namen: list[str]  # Vertretungsberechtigte in Impressum-Reihenfolge, danach Verantwortliche
    funktionen: dict[str, str]  # Berufsbezeichnung direkt am Namen („Yvonne Baur, Steuerberaterin")
    bezeichnung: str | None  # einzige Berufsbezeichnung des Abschnitts
    protokoll: list[str] = field(default_factory=list)  # je Schlüsselwort-Zeile: Ergebnis (Diagnose)


def vertretungsberechtigte(
    impressum: str, fremd: set[int] | None = None, rahmen: set[str] | None = None
) -> tuple[list[str], str | None]:
    v = vertretung(impressum, fremd, rahmen)
    return v.namen, v.bezeichnung


def vertretung(impressum: str, fremd: set[int] | None = None, rahmen: set[str] | None = None) -> Vertretung:
    """Im Impressum genannte Vertretungsberechtigte (danach „Verantwortlich gemäß § 5 TMG") und –
    falls genau eine im selben Abschnitt (±8 Zeilen, außerhalb von Kammer-/Fremd-Abschnitten) –
    ihre Berufsbezeichnung. Zeilen aus Navigation/Fußzeile (`rahmen`, normalisiert) zählen nicht."""
    zeilen = (text_normalisieren(impressum) or "").splitlines()  # Zeilenzahl bleibt (fremd-Indizes)
    fremd = fremd or set()
    rahmen = rahmen or set()

    def gesperrt(j: int) -> bool:
        return j in fremd or " ".join(zeilen[j].lower().split()) in rahmen

    namen: list[str] = []
    verantwortliche: list[str] = []
    funktionen: dict[str, str] = {}
    bezeichnung: str | None = None
    protokoll: list[str] = []
    for i, z in enumerate(zeilen):
        if len(z) > 300:
            continue
        if treffer := _schluessel(z):
            (m, stark), ziel = treffer, namen
        elif m := _VERANTWORTLICH.search(z):
            stark, ziel = True, verantwortliche
        else:
            continue
        # Eine Zeile, die mit dem Schlüsselwort beginnt („Geschäftsführer: …"), beginnt einen eigenen
        # Abschnitt – auch direkt hinter den Kammer-Adressen (Fremd-Abschnitt); ein Satz im Fremd-Abschnitt
        # („… Versicherung AG, vertreten durch den Vorstand …") bleibt gesperrt
        im_rahmen = " ".join(z.lower().split()) in rahmen
        eigener_abschnitt = i in fremd and _nur_rollenwoerter(z[: m.start()]) and not _FREMD_WORT.search(z)
        if im_rahmen or (i in fremd and not eigener_abschnitt):
            protokoll.append(f"Zeile {i + 1} „{z[:70]}“ → übersprungen ({'Navigation/Fußzeile' if im_rahmen else 'Fremd-Abschnitt'})")
            continue
        folge_gesperrt = (lambda j: " ".join(zeilen[j].lower().split()) in rahmen) if eigener_abschnitt else gesperrt
        gefunden_namen, grund = _abschnitt_namen(zeilen, i, m, stark, folge_gesperrt)
        protokoll.append(f"Zeile {i + 1} „{z[:70]}“ → {grund}")
        for name, funktion in gefunden_namen:
            if not any(gleiche_person(name, n) for n in namen + verantwortliche):
                ziel.append(name)
                if funktion:
                    funktionen[name] = funktion
        if not gefunden_namen:
            continue
        bereich = range(max(0, i - ABSCHNITT_ZEILEN), min(len(zeilen), i + ABSCHNITT_ZEILEN + 1))
        gefunden = set()
        for j in bereich:
            if j in fremd or not (b := _BERUFSBEZEICHNUNG.search(zeilen[j])):
                continue
            text = b.group(1) or (zeilen[j + 1] if j + 1 < len(zeilen) else "")
            gefunden |= {name for name, muster in _BEZEICHNUNGEN.items() if muster.search(text)}
        if len(gefunden) == 1 and bezeichnung is None:
            bezeichnung = gefunden.pop()
    return Vertretung(namen + verantwortliche, funktionen, bezeichnung, protokoll)


def bezeichnung_aus_partnerschaftsname(name: str | None) -> str | None:
    """„Brandenburg und Partner mbB Steuerberater" → „Steuerberater": Der Name einer Partnerschaft nennt
    die Berufe aller Partner (§ 2 PartGG) – steht dort genau einer, gilt er für die Vertretungsberechtigten."""
    if not name or not _PARTNERSCHAFT.search(name):
        return None
    berufe = {b for b, muster in _BEZEICHNUNGEN.items() if muster.search(name)}
    return berufe.pop() if len(berufe) == 1 else None


def ansprechpartner(
    llm: LlmErgebnis | None,
    erkannte: list[ErkanntePerson] | None = None,
    impressum: str | None = None,
    fremd: set[int] | None = None,
    rahmen: set[str] | None = None,
    kanzleiname: str | None = None,
) -> list[Ansprechpartner]:
    """Berufsträger und Leitung aus LLM, Regel-Erkennung und Impressum.

    Reihenfolge: Verantwortliche/r laut Impressum, dann Leitung, dann Berufsträger. Vertretungs-
    berechtigte gelten als Berufsträger, wenn im selben Impressum-Abschnitt genau eine
    Berufsbezeichnung steht.
    """
    # Grundlage: strukturell erkannte Personen (Teamseiten) und Impressum; das LLM ergänzt nur
    roh: list[LlmPerson] = [LlmPerson(name=p.name, titel=p.titel, funktion=p.funktion) for p in erkannte or []]
    v = vertretung(impressum, fremd, rahmen) if impressum else Vertretung([], {}, None)
    vertreter, bezeichnung = v.namen, v.bezeichnung
    # Hauptansprechpartner: erste/r Vertretungsberechtigte laut Impressum, sonst die vom LLM genannte Person
    verantwortlich = vertreter[0] if vertreter else (llm.verantwortliche_person if llm else None)
    for name in vertreter:
        if funktion := v.funktionen.get(name):
            roh.append(LlmPerson(name=name, funktion=f"{funktion} (laut Impressum)"))
        elif not any(gleiche_person(name, p.name) for p in roh):
            roh.append(LlmPerson(name=name, funktion="laut Impressum vertretungsberechtigt"))
    roh += list(llm.personen) if llm else []
    personen = personen_vereinigen(roh)
    if not bezeichnung and vertreter:
        bezeichnung = bezeichnung_aus_partnerschaftsname(kanzleiname)
    if bezeichnung:
        personen = [
            LlmPerson(name=p.name, titel=p.titel, funktion=_funktion_vereinigen(p.funktion, f"{bezeichnung} (laut Impressum)"))
            if any(gleiche_person(p.name, v) for v in vertreter) and not ist_berufstraeger(p) else p
            for p in personen
        ]
    auswahl = [p for p in personen if ist_berufstraeger(p) or ist_leitung(p) or any(gleiche_person(p.name, v) for v in vertreter)]
    if verantwortlich and not any(gleiche_person(verantwortlich, p.name) for p in auswahl):
        # im Impressum genannt, aber ohne Funktion in der Personenliste: als Leitung aufnehmen
        auswahl.insert(0, LlmPerson(name=verantwortlich, funktion="laut Impressum verantwortlich"))
    team_reihenfolge = [p.name for p in erkannte or []]

    def rang(p: LlmPerson) -> tuple[int, int]:
        """Vertretungsberechtigte in Impressum-Reihenfolge, dann alle übrigen in der Reihenfolge der
        Teamseite (erste Nennung), zuletzt Personen nur aus der LLM-Liste."""
        for i, v in enumerate(vertreter):
            if gleiche_person(p.name, v):
                return (0, i)
        if not vertreter and gleiche_person(p.name, verantwortlich):
            return (0, 0)
        # danach die Reihenfolge der Teamseite (Leitung und Berufsträger gleichrangig), sonst der LLM-Liste
        pos = next((i for i, n in enumerate(team_reihenfolge) if gleiche_person(p.name, n)), None)
        return (1, pos) if pos is not None else (2, auswahl.index(p))

    return [Ansprechpartner(name=p.name, titel=p.titel, funktion=p.funktion) for p in sorted(auswahl, key=rang)]


def _groesser(klasse: TeamKlasse | None, andere: TeamKlasse | None) -> TeamKlasse | None:
    if klasse is None or andere is None:
        return klasse or andere
    return klasse if _KLASSEN.index(klasse) >= _KLASSEN.index(andere) else andere


def teamgroesse(
    llm: LlmErgebnis | None,
    team_seiten: list[Seite] | Seite | None,
    erkannte: list[ErkanntePerson] | None = None,
    angabe: Mitarbeiterzahl | None = None,
) -> tuple[int | None, TeamKlasse | None, str | None]:
    """(Zahl, Klasse, Basis).

    Vorrang: genannte Zahl (LLM, dann Regel) > gezählte Personen der Teamseiten (≥ 2; Regel-Erkennung
    oder LLM-Personen, die im Teamtext stehen) > Klasse aus dem Text. Gezählte Personen sind eine
    Untergrenze: Nennt der Text eine größere Klasse („über 20 Mitarbeitende", aber nur 2 Personen
    abgebildet), gilt die Klasse.
    """
    if isinstance(team_seiten, Seite):
        team_seiten = [team_seiten]
    team_seiten = [s for s in team_seiten or [] if s is not None]
    if llm and llm.mitarbeiterzahl:
        beleg = llm.mitarbeiterzahl_beleg
        return llm.mitarbeiterzahl, None, f"Text: „{beleg}“" if beleg else "Mitarbeiterzahl im Text genannt"
    if angabe and angabe.zahl:
        return angabe.zahl, None, f"Text: „{angabe.beleg}“"
    klasse = _groesser(llm.teamgroesse_klasse if llm else None, angabe.klasse if angabe else None)
    gezaehlt: list[str] = [p.name for p in erkannte or []]
    if llm and team_seiten:
        teamtext = "\n".join((s.text or "") for s in team_seiten).lower()
        for p in llm.personen:
            nachname = sorted(_namens_tokens(p.name), key=len)[-1:] if p.name else []
            if nachname and nachname[0] in teamtext and not any(gleiche_person(p.name, q) for q in gezaehlt):
                gezaehlt.append(p.name)
    n = len(gezaehlt)
    if n >= 2 and (klasse is None or _KLASSEN.index(TeamKlasse.aus_zahl(n)) >= _KLASSEN.index(klasse)):
        return n, None, f"{n} Personen auf Teamseite"
    if klasse:
        if angabe and angabe.klasse == klasse:
            return None, klasse, f"Text: „{angabe.beleg}“"
        return None, klasse, "Einschätzung aus dem Text"
    return None, None, None


# Berufsbezeichnungen, die zum Namen einer Partnerschaft gehören („… PartG mbB Steuerberater")
_NAMENS_BERUF = re.compile(
    r"^(?:steuerberater\w*|wirtschaftspr[uü]f\w*|rechtsanw[aä]lt\w*|vereidigte[rn]?|buchpr[uü]fer\w*|"
    r"steuerbevollm[aä]chtigt\w*|und|&|\+|,|/|·|-|–)$",
    re.I,
)


_PARTNERSCHAFT = re.compile(r"partnerschaft|partg|\bund\s+partner\b|&\s*partner\b", re.I)


def _woerter(text: str) -> list[str]:
    return [w for w in re.split(r"[\s,;·|]+", text.lower()) if w]


def name_vervollstaendigen(name: str | None, impressum: str | None) -> str | None:
    """„Baur Drews Partnerschaftsgesellschaft mbB" → „… mbB Steuerberater", wenn das Impressum den Namen
    mit angehängter Berufsbezeichnung führt (in derselben oder der nächsten Zeile). Nur bei
    Partnerschaften – dort gehören die Berufsbezeichnungen zum Namen (§ 2 PartGG); bei einer
    Einzelkanzlei ist „Steuerberater" unter dem Namen die Berufsbezeichnung des Inhabers.
    Nur Berufswörter werden ergänzt, nie anderer Text."""
    if not name or not impressum or not _PARTNERSCHAFT.search(name):
        return name
    soll = _woerter(name)
    zeilen = [z.strip() for z in (text_normalisieren(impressum) or "").splitlines()][:60]
    for i, z in enumerate(zeilen):
        ist = _woerter(z)
        if ist[: len(soll)] != soll:
            continue
        rest = ist[len(soll):]
        if rest and len(rest) <= 6 and all(_NAMENS_BERUF.match(w) for w in rest):
            return z.rstrip(" ,;")
        folge = _woerter(zeilen[i + 1]) if not rest and i + 1 < len(zeilen) else []
        if folge and len(folge) <= 6 and all(_NAMENS_BERUF.match(w) for w in folge):
            return f"{z.rstrip(' ,;')} {zeilen[i + 1].strip()}"
    return name


# nur ein echter Registereintrag – „Eine Eintragung im Handelsregister besteht nicht" zählt nicht
_REGISTER = re.compile(r"\b(?:HR\s?[AB]|PR|GnR|VR)\s?\d|registernummer\s*:?\s*\w*\d", re.I)
_REGISTERGERICHT = re.compile(r"amtsgericht|registergericht|\bAG\s+[A-ZÄÖÜ][a-zäöü]+\s*(?:,|\b(?:HR|PR))", re.I)
_KURZFORMEN = {"AG", "KG", "UG (haftungsbeschränkt)", "OHG", "GbR"}
_NAME_VORSILBE = re.compile(r"^(?:steuerbüro|steuerkanzlei|kanzlei|steuerberatung|büro)\s+", re.I)
_GESELLSCHAFT_HINWEIS = re.compile(r"&|\bund\b|\+|partner|gesellschaft|\|", re.I)


def _impressum_kopf(impressum: str | None, fremd: set[int] | None, rahmen: set[str] | None, n: int = 40) -> list[str]:
    """Die ersten n Zeilen des Impressums ohne Fremd-Abschnitte und ohne Navigation/Fußzeile."""
    zeilen = (text_normalisieren(impressum) or "").splitlines()
    fremd, rahmen = fremd or set(), rahmen or set()
    # erst filtern, dann zählen: lange Navigation am Seitenanfang verdrängt sonst den Inhalt
    return [z.strip() for i, z in enumerate(zeilen)
            if z.strip() and i not in fremd and " ".join(z.lower().split()) not in rahmen][:n]  # fmt: skip


def rechtsform_aus_seiten(name: str | None, impressum: str | None, fremd: set[int] | None = None,
                          rahmen: set[str] | None = None) -> str | None:
    """Rechtsform per Regel – vor dem LLM: zuerst aus dem Kanzleinamen, dann aus dem Impressum-Kopf
    („Partnerschaft mbB", „GmbH"; Fremd-Abschnitte wie Hoster und Kammer zählen nicht)."""
    if form := rechtsform_aus_text(name):
        return form
    namens_woerter = {w for w in _woerter(name or "") if len(w) >= 4}
    kopf = _impressum_kopf(impressum, fremd, rahmen)
    for z in kopf:
        if len(z) > 160 or _REGISTERGERICHT.search(z) or not (form := rechtsform_aus_text(z)) or form == "Einzelkanzlei":
            continue
        # Kurzformen („AG", „KG" …) nur in der Namenszeile selbst – „AG" ist oft das Amtsgericht
        if form in _KURZFORMEN and not (namens_woerter and any(w in z.lower() for w in namens_woerter)):
            continue
        return form
    # Registereintrag im Partnerschaftsregister → PartG; „mbB"/„beschränkte Berufshaftung" irgendwo im
    # Impressum (oft im Haftpflicht-Abschnitt) → PartG mbB
    voll = text_normalisieren(impressum) or ""
    if re.search(r"partnerschaftsregister|\bPR\s?\d", voll, re.I):
        return "PartG mbB" if re.search(r"(?:\b|part)mbb\b|beschränkter?\s+berufshaftung", voll, re.I) else "PartG"
    return None


def name_mit_vorname(name: str | None, personen: list) -> str | None:
    """„Steuerberater Schreiner" + Person „Andreas Schreiner" → „Steuerberater Andreas Schreiner"."""
    woerter = (name or "").split()
    if len(woerter) != 2 or not _BERUFSWORT.match(woerter[0].lower()):
        return name
    treffer = [p for p in personen if (teile := _name_teile(p.name)) and teile[1] == woerter[1].lower() and teile[0]]
    return f"{woerter[0]} {treffer[0].name}" if len(treffer) == 1 else name


def rechtsform_belegt(form: str | None, name: str | None, impressum: str | None, fremd: set[int] | None = None,
                      rahmen: set[str] | None = None) -> bool:
    """Steht die vom LLM genannte Rechtsform irgendwo in Name oder Impressum? Sonst nicht übernehmen (nie raten)."""
    if not form:
        return False
    texte = [name or "", *_impressum_kopf(impressum, fremd, rahmen, 400)]
    return any(normalize_rechtsform(rechtsform_aus_text(z)) == form for z in texte if not _REGISTERGERICHT.search(z))


def ist_einzelkanzlei(name: str | None, impressum: str | None, vertreter: list[str]) -> bool:
    """Einzelperson mit Berufstitel, keine Gesellschaftsform: „Steuerberater Andreas Schreiner",
    „Steuerbüro Thomas Schütt" – oder genau ein Inhaber/Vertreter und kein Registereintrag."""
    if name and not _GESELLSCHAFT_HINWEIS.search(name):
        rest = _NAME_VORSILBE.sub("", name.strip())
        if (nt := name_zerlegen(rest)) and len(nt[0].split()) <= 3 and (nt[1] or rest != name.strip()):
            return True
        # „Steuerberater Schreiner": Berufsbezeichnung und Nachname
        woerter = rest.split()
        if len(woerter) == 2 and _BERUFSWORT.match(woerter[0].lower()) and re.match(r"^[A-ZÄÖÜ][a-zäöüß-]+$", woerter[1]):
            return True
    return len(vertreter) == 1 and bool(impressum) and not _REGISTER.search(impressum or "")


def _namens_zusatz(soll: list[str], z: str) -> list[str] | None:
    """Wörter von z, die über den Namen hinausgehen – None, wenn z nicht alle Namenswörter enthält
    (auch als Wortanfang: „STEUERBERATUNG" ⊂ „Steuerberatungsgesellschaft") oder mehr als 3 dazukommen."""
    ist = [w for w in _woerter(z) if w not in "&+|-–"]
    if len(ist) <= len(soll) or len(ist) > len(soll) + 3:
        return None
    rest = list(ist)
    for w in soll:
        treffer = next((x for x in rest if x == w or (len(w) >= 5 and x.startswith(w))), None)
        if treffer is None:
            return None
        rest.remove(treffer)
    return rest


def name_aus_impressum(name: str | None, impressum: str | None, fremd: set[int] | None = None,
                       rahmen: set[str] | None = None) -> str | None:
    """Der Name im Impressum hat Vorrang vor Logo/Startseite: „900 GRAD STEUERBERATUNG" →
    „900 GRAD Steuerberatungsgesellschaft PartmbB", „Steuerberater Schreiner" → „Steuerberater
    Andreas Schreiner".

    1. Eine Zeile irgendwo im Impressum (auch im Kammer-/Haftpflicht-Abschnitt, auch hinter „Firma:"),
       die alle Wörter des Namens enthält und als Zusatz eine Rechtsform.
    2. Steht der Name genau so im Impressum-Kopf: unverändert.
    3. Eine Zeile (oder zwei kurze) im Kopf mit allen Namenswörtern und höchstens 3 weiteren –
       Rechtsform, Berufswörter oder Namen, nie Menüwörter."""
    if not name or not impressum:
        return name
    soll = [w for w in _woerter(name) if w not in "&+|-–"]
    if not soll:
        return name
    alle = [z.strip().rsplit(":", 1)[-1].strip() for z in (text_normalisieren(impressum) or "").splitlines() if z.strip()]
    for z in alle:
        if len(z) <= 120 and (rest := _namens_zusatz(soll, z)) and any(rechtsform_aus_text(w) for w in rest):
            return z.rstrip(" ,;.")
    kopf = _impressum_kopf(impressum, fremd, rahmen, 25)
    if any([w for w in _woerter(z) if w not in "&+|-–"] == soll for z in kopf):
        return name  # steht genau so im Impressum
    # auch zwei kurze Zeilen hintereinander („Steuerberater" / „Andreas Schreiner")
    paare = [f"{a} {b}" for a, b in zip(kopf, kopf[1:], strict=False) if len(a) <= 40 and len(b) <= 40 and ":" not in a + b]
    for z in kopf + paare:
        if ":" in z or len(z) > 90 or (rest := _namens_zusatz(soll, z)) is None:
            continue
        original = {w.lower(): w for w in re.split(r"[\s,;·|]+", z) if w}
        if all((rechtsform_aus_text(w) or _NAMENS_BERUF.match(w) or original.get(w, w)[:1].isupper())
               and w not in _MENUEWOERTER for w in rest):
            return z.rstrip(" ,;")
    return name
    soll = [w for w in _woerter(name) if w not in "&+|-–"]
    kopf = _impressum_kopf(impressum, fremd, rahmen, 25)
    # auch zwei kurze Zeilen hintereinander („Steuerberater" / „Andreas Schreiner")
    paare = [f"{a} {b}" for a, b in zip(kopf, kopf[1:], strict=False) if len(a) <= 40 and len(b) <= 40 and ":" not in a + b]
    if any([w for w in _woerter(z) if w not in "&+|-–"] == soll for z in kopf):
        return name  # steht genau so im Impressum
    for z in kopf + paare:
        if ":" in z or len(z) > 90 or not (1 <= len(soll)):
            continue
        ist = [w for w in _woerter(z) if w not in "&+|-–"]
        if len(ist) <= len(soll) or len(ist) > len(soll) + 3:
            continue
        rest = list(ist)
        for w in soll:
            treffer = next((x for x in rest if x == w or (len(w) >= 5 and x.startswith(w))), None)
            if treffer is None:
                break
            rest.remove(treffer)
        else:
            original = {w.lower(): w for w in re.split(r"[\s,;·|]+", z) if w}
            if all((rechtsform_aus_text(w) or _NAMENS_BERUF.match(w) or original.get(w, w)[:1].isupper())
                   and w not in _MENUEWOERTER for w in rest):
                return z.rstrip(" ,;")
    return name


def erkannte_personen(team_seiten: list[Seite], rahmen: set[str] | None = None) -> list[ErkanntePerson]:
    """Personen beider Teamseiten aus Text, JSON-LD und Bild-Attributen; dieselbe Person mehrfach →
    Funktionen und Titel vereinigt (Reihenfolge der ersten Nennung)."""
    erkannte: list[ErkanntePerson] = []
    for s in team_seiten:
        for p in erkenne_personen(_text(s), rahmen=rahmen) + personen_aus_json_ld(s.html) + personen_aus_attributen(s.html):
            for i, q in enumerate(erkannte):
                if gleiche_person(p.name, q.name):
                    erkannte[i] = ErkanntePerson(q.name if len(q.name) >= len(p.name) else p.name, q.titel or p.titel,
                                                 _funktion_vereinigen(q.funktion, p.funktion) or "", q.zeile)
                    break
            else:
                erkannte.append(p)
    return erkannte


def _adresse_text(strasse: str | None, plz: str | None, ort: str | None) -> str:
    return ", ".join(x for x in (strasse, " ".join(y for y in (plz, ort) if y)) if x)


def konfidenz(k: Kanzlei, impressum_gelesen: bool) -> Konfidenz:
    """Vorläufige Regel (CLAUDE.md): hoch = Impressum gelesen und PLZ/Ort verifiziert;
    mittel = Kernfelder vollständig; niedrig = Kernfelder fehlen."""
    if impressum_gelesen and k.plz_verifiziert:
        return Konfidenz.HOCH
    kern = all((k.kanzleiname, k.strasse, k.plz, k.ort)) and bool(k.telefon or k.email)
    return Konfidenz.MITTEL if kern else Konfidenz.NIEDRIG


def zusammenfuehren(
    kandidat: Kandidat,
    funde: list[Fund],
    seiten: dict[Seitentyp, Seite],
    regel: RegelErgebnis | None,
    llm: LlmErgebnis | None,
    plz_pruefen: Callable[[str | None, str | None], PlzPruefung] | None = None,
    ausschluss: Ausschluss | None = None,
) -> Kanzlei:
    disc = discovery_werte(funde)
    regel = regel or RegelErgebnis()
    abweichungen: list[str] = list(regel.hinweise)

    # ---- Name: Discovery (Stufe B) > LLM
    impressum_text = _text(seiten[Seitentyp.IMPRESSUM]) if Seitentyp.IMPRESSUM in seiten else None
    rahmen = seiten_rahmen(seiten)
    fremd = ausschluss.fremd_zeilen(impressum_text.splitlines()) if ausschluss and impressum_text else None
    llm_name = name_vervollstaendigen(
        name_aus_impressum(llm.kanzleiname if llm else None, impressum_text, fremd, rahmen), impressum_text)
    name = disc["name"] or llm_name
    if disc["name"] and llm_name and not (name_tokens(disc["name"]) & name_tokens(llm_name)):
        abweichungen.append(f"Name: {disc['quelle_name']} „{disc['name']}“ / Website „{llm_name}“")

    # ---- Adresse als Einheit: Discovery > Regeln > LLM
    regel_zusatz = regel.adresszusatz
    llm_strasse, llm_zusatz = (strasse_zerlegen(llm.strasse) or (llm.strasse, None)) if llm and llm.strasse else (None, None)
    web_adresse = (regel.strasse, regel.plz, regel.ort) if regel.plz else (
        (llm_strasse, llm.plz, llm.ort) if llm and llm.plz else (None, None, None)
    )
    if regel.plz and not regel.strasse and llm_strasse and llm.plz == regel.plz:
        # Regel fand PLZ/Ort, aber keine Straße: die Straße des LLM zur selben PLZ übernehmen
        web_adresse, regel_zusatz = (llm_strasse, regel.plz, regel.ort), llm_zusatz
    elif not regel.plz:
        regel_zusatz = llm_zusatz
    if disc["plz"] and not disc["strasse"] and web_adresse[1] == disc["plz"] and web_adresse[0]:
        # Discovery kennt nur PLZ/Ort, die Website dieselbe PLZ mit Straße: vollständige Adresse nehmen
        strasse, plz, ort = web_adresse
    elif disc["plz"]:
        strasse, plz, ort = disc["strasse"], disc["plz"], disc["ort"]
        if web_adresse[1]:
            d_key, w_key = adresse_key(strasse, plz), adresse_key(web_adresse[0], web_adresse[1])
            if (d_key and w_key and d_key != w_key) or (not (d_key and w_key) and plz != web_adresse[1]):
                abweichungen.append(
                    f"Adresse: {disc['quelle_adresse']} {_adresse_text(strasse, plz, ort)} / "
                    f"Website {_adresse_text(*web_adresse)}"
                )
    else:
        strasse, plz, ort = web_adresse

    # ---- Telefon: Discovery > Regeln > LLM
    web_tel = regel.telefon or normalize_telefon(llm.telefon if llm else None)
    if disc["telefon"]:
        telefon = normalize_telefon(disc["telefon"]) or disc["telefon"]
        if web_tel and telefon_key(web_tel) != telefon_key(telefon):
            abweichungen.append(f"Telefon: {disc['quelle_telefon']} {telefon} / Website {web_tel}")
    else:
        telefon = web_tel

    email = regel.email or ((llm.email or "").strip().lower() or None if llm else None)
    # Rechtsform: Regel aus Name und Impressum vor dem LLM; Einzelkanzlei nur, wenn sonst nichts passt
    llm_form = normalize_rechtsform(llm.rechtsform if llm else None)
    rechtsform = rechtsform_aus_seiten(name, impressum_text, fremd, rahmen) or (
        llm_form if rechtsform_belegt(llm_form, name, impressum_text, fremd, rahmen) else None)

    # ---- Personen und Teamgröße: Regel-Erkennung auf den Teamseiten + LLM + Impressum
    team_seiten = [seiten[t] for t in (Seitentyp.TEAM, Seitentyp.TEAM2) if t in seiten]
    erkannte = erkannte_personen(team_seiten, rahmen)
    personen = ansprechpartner(llm, erkannte, impressum_text, fremd, rahmen, name)
    if rechtsform is None and ist_einzelkanzlei(name, impressum_text,
                                                vertretung(impressum_text, fremd, rahmen).namen if impressum_text else []):
        rechtsform = "Einzelkanzlei"
    angabe = mitarbeiterzahl([_text(s) for s in seiten.values()])
    zahl, klasse, basis = teamgroesse(llm, team_seiten, erkannte, angabe)
    berufstraeger = sum(1 for p in personen if ist_berufstraeger(p))
    if zahl is None and klasse is None and rechtsform == "Einzelkanzlei" and berufstraeger == 1 and len(erkannte) <= 1:
        zahl, basis = 1, "Einzelkanzlei: ein Berufsträger, keine weiteren Personen"
    if rechtsform == "Einzelkanzlei":
        name = name_mit_vorname(name, personen)
    hat_personeninfo = llm is not None or bool(erkannte) or bool(personen)

    # ---- Website, Quellen, Datum
    start = seiten.get(Seitentyp.START)
    website = (start.final_url or start.url) if start else kandidat.website
    quell_urls = [s.url for t, s in seiten.items() if t != Seitentyp.START]
    quell_urls += [f.roh_url for f in funde if f.quelle in _stufe_b_quellen() and f.roh_url]
    impressum = seiten.get(Seitentyp.IMPRESSUM)
    if impressum:
        abrufdatum = impressum.abgerufen_am.date()
    elif funde:
        abrufdatum = max(f.gefunden_am for f in funde).date()
    else:
        abrufdatum = date.today()

    # Adresszusatz nur, wenn die Adresse die der Regeln ist
    adresszusatz = regel_zusatz if (strasse, plz) == web_adresse[:2] else None
    pruefung = plz_pruefen(plz, ort) if plz_pruefen and plz else None
    if pruefung and pruefung.ort_kanonisch:
        ort = pruefung.ort_kanonisch  # „Hamburg-Rissen" → „Hamburg" (OpenPLZ-Ortsname)
    k = Kanzlei(
        kandidat_id=kandidat.id,
        domain=kandidat.domain,
        lauf_bundesland=kandidat.bundesland,
        kanzleiname=name,
        rechtsform=rechtsform,
        strasse=strasse,
        adresszusatz=adresszusatz,
        plz=plz,
        ort=ort,
        bundesland=pruefung.bundesland if pruefung else None,
        plz_verifiziert=pruefung.verifiziert if pruefung else None,
        telefon=telefon,
        email=email,
        website=website,
        website_vorhanden=bool(kandidat.domain or kandidat.website),
        ansprechpartner=personen,
        berufstraeger_anzahl=berufstraeger if hat_personeninfo else None,
        teamgroesse=zahl,
        teamgroesse_klasse=klasse,
        teamgroesse_basis=basis,
        quell_urls=list(dict.fromkeys(quell_urls)),
        abrufdatum=abrufdatum,
    )
    if pruefung and pruefung.verifiziert is False and pruefung.hinweis:
        abweichungen.append(f"PLZ/Ort: {pruefung.hinweis}")
    k.abweichung = "; ".join(abweichungen) or None
    k.konfidenz = konfidenz(k, impressum is not None)
    return k
