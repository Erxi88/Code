"""Regelbasierte Erkennung von Personen und Mitarbeiterzahlen – unabhängig vom LLM.

Personen: Zeilen bzw. Kacheln mit einem Namen (2–4 großgeschriebene Wörter, Titel erlaubt)
und daneben oder darunter einer Funktion aus config/personen_funktionen.yaml
(„Jens Hansen" / „Steuerberater, Partner" oder „Jens Hansen – Steuerberater").

Mitarbeiterzahl: Textstellen wie „Team aus 12 Mitarbeitenden", „28 Expert:innen",
„über 20 Mitarbeiterinnen und Mitarbeiter". Eine genaue Angabe ergibt eine Zahl;
„über/mehr als N" ist eine Untergrenze (Klasse oberhalb von N); „rund/ca./etwa N"
ergibt die Klasse von N.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import PROJEKT_DIR
from ..models import TeamKlasse
from ..textnorm import text_normalisieren

STANDARD_FUNKTIONEN = PROJEKT_DIR / "config" / "personen_funktionen.yaml"

# Titel und vorangestellte Berufsbezeichnungen („StB Dipl.-Kfm.", „Diplom-Betriebswirt", „Steuerberater …")
_BERUF_VORN = r"(?:StB|WP|RA|RAin|vBP|StBin|Steuerberater(?:in)?|Rechtsanw(?:alt|ältin)|Wirtschaftsprüfer(?:in)?)"
_TITEL = re.compile(
    r"^(?:(?:Prof\.|Dr\.(?:\s*(?:rer\.\s*pol\.|jur\.|oec\.))?|Dipl\.-?\s?[A-ZÄÖÜa-zäöü]+\.?(?:\s*\((?:FH|BA|DH)\))?|"
    r"Diplom-[A-ZÄÖÜ][a-zäöüß]+(?:\s*\((?:FH|BA|DH)\))?|"
    r"M\.\s?(?:Sc|A)\.|B\.\s?(?:Sc|A)\.|LL\.\s?[MB]\.|MBA|Mag\.|" + _BERUF_VORN + r"(?:\s*/\s*" + _BERUF_VORN + r")*" +
    r"(?=\s))\s*,?\s*)+"
)
_TITEL_HINTEN = re.compile(
    r",\s*((?:M\.\s?Sc\.|M\.\s?A\.|B\.\s?Sc\.|B\.\s?A\.|LL\.\s?M\.|MBA|Dipl\.-?\s?\w+\.?|StB|WP|RA|vBP)"
    r"(?:\s*\(\w+\))?(?:\s*,\s*(?:StB|WP|RA|vBP))*)\s*$"
)
_PARTIKEL = frozenset({"von", "van", "de", "der", "den", "zu", "la", "le", "di", "da", "ten"})
_NAME_WORT = re.compile(r"^[A-ZÄÖÜ][a-zäöüßé]+(?:-[A-ZÄÖÜ][a-zäöüßé]+)?\.?$")
_INITIALE = re.compile(r"^[A-ZÄÖÜ]\.$")  # „W. Drews" – nur als erstes Wort
_ANREDE = re.compile(r"^(?:Frau|Herr|Herrn)\s+")
_KEINE_NAMEN = frozenset(
    """unser unsere unseres team kanzlei kontakt steuerberatung leistungen impressum datenschutz startseite home über
    uns karriere jobs mehr erfahren sie ihr ihre wir telefon fax termin willkommen standort standorte anfahrt
    mandanten service aktuelles news referenzen downloads links partnerschaft gesellschaft sprechzeiten
    öffnungszeiten ansprechpartner ansprechpartnerin geschäftsführung leitung verwaltung angaben berufsrechtliche
    rechtliche hinweise register registergericht amtsgericht handelsregister partnerschaftsregister ust
    steuernummer umsatzsteuer gesellschaft kammer versicherung aufsichtsbehörde berufsbezeichnung haftung
    inhalte urheberrecht verantwortlich inhaltlich sitz deutschland bundesrepublik zuständige zuständiger
    berufshaftpflicht berufsordnung streitschlichtung information informationen allgemeine
    vertretungsberechtigte vertretungsberechtigter gesellschafter
    berufsträger berufstraeger firmenphilosophie philosophie historie geschichte leitbild werte vision
    mandantenportal portal aktuelles blog presse veranstaltungen seminare newsletter formulare rechner
    partner netzwerk kooperationen branchen schwerpunkte kompetenzen digitalisierung ausbildung
    stellenangebote unternehmen privatpersonen existenzgründer existenzgründung nachfolge erbschaft
    buchhaltung jahresabschluss beratung wirtschaftsprüfung rechtsberatung sanierung international galerie
    faq glossar login suche menü menu zurück weiter kontaktformular rückruf termine expertise tätigkeitsfelder
    tätigkeitsschwerpunkte mitarbeiter mitarbeitende experten historie anfahrt chaussee straße strasse weg
    allee platz ring damm vorstand vorstände aufsichtsrat geschäftsleitung
    rating advisor consultant consulting manager office service senior junior associate""".split()
)
# Institutionen, keine Personen: „Wirtschaftsprüferkammer Berlin", „Amtsgericht Kiel"
_INSTITUTION = re.compile(r"(?:kammer|gericht|register|versicherung|verband|bank|gmbh|straße|strasse|chaussee)$", re.I)
_TRENNER = re.compile(r"\s+[–—|·]\s+|\s+-\s+|\s*\|\s*|,\s+|\s*\(")


@dataclass(frozen=True)
class Funktionen:
    berufstraeger: tuple[str, ...]
    leitung: tuple[str, ...]
    mitarbeitende: tuple[str, ...]

    @property
    def alle(self) -> tuple[str, ...]:
        return self.berufstraeger + self.leitung + self.mitarbeitende

    def _muster(self, woerter: tuple[str, ...]) -> re.Pattern:
        # Wortgrenze vorn – außer bei „*kauffrau": auch als Wortende von Komposita
        # („Einzelhandelskauffrau", „Diplom-Kauffrau"), aber nicht allein (Nachname „Kaufmann")
        ganz = sorted((re.escape(w) for w in woerter if not w.startswith("*")), key=len, reverse=True)
        ende = sorted((re.escape(w[1:]) for w in woerter if w.startswith("*")), key=len, reverse=True)
        # kein Bindestrich davor: „BD-Partner" ist ein Firmenname, keine Funktion
        alternativen = [r"(?<![a-zäöüß-])(?:" + "|".join(ganz) + ")"] if ganz else []
        if ende:
            alternativen.append(r"(?<=[a-zäöüß-]{3})(?:" + "|".join(ende) + ")")
        # Gender-Formen: Steuerberater:in, Steuerberater/in, Steuerberater*in, Steuerberater_in
        return re.compile(r"(?:" + "|".join(alternativen) + r")(?:[:*/_]?in(?:nen)?)?(?![a-zäöüß])", re.I)

    @property
    def muster_alle(self) -> re.Pattern:
        return _muster_cache(self, "alle")

    @property
    def muster_berufstraeger(self) -> re.Pattern:
        return _muster_cache(self, "berufstraeger")

    @property
    def muster_leitung(self) -> re.Pattern:
        return _muster_cache(self, "leitung")


@lru_cache(maxsize=32)
def _muster_cache(f: Funktionen, art: str) -> re.Pattern:
    return f._muster(f.alle if art == "alle" else getattr(f, art))


@lru_cache(maxsize=8)
def lade_funktionen(pfad: str | Path = STANDARD_FUNKTIONEN) -> Funktionen:
    d = yaml.safe_load(Path(pfad).read_text(encoding="utf-8-sig")) or {}
    return Funktionen(*(tuple(d.get(k) or ()) for k in ("berufstraeger", "leitung", "mitarbeitende")))


@dataclass(frozen=True)
class ErkanntePerson:
    name: str
    titel: str | None
    funktion: str
    zeile: int


def name_zerlegen(text: str) -> tuple[str, str | None] | None:
    """'Dipl.-Kfm. Jens Hansen' → ('Jens Hansen', 'Dipl.-Kfm.'); kein Name → None."""
    text = _ANREDE.sub("", " ".join(text.strip(" :;").split()))
    titel_teile = []
    if m := _TITEL.match(text):
        titel_teile.append(m.group(0).strip())
        text = text[m.end():].strip()
    if m := _TITEL_HINTEN.search(text):
        titel_teile.append(m.group(1).strip())
        text = text[: m.start()].strip()
    text = text.rstrip(" .")
    woerter = text.split()
    gross = [w for w in woerter if w.lower() not in _PARTIKEL]
    if not 2 <= len(gross) <= 4 or len(woerter) > 6:
        return None
    if not all(_NAME_WORT.match(w) or (k == 0 and _INITIALE.match(w)) for k, w in enumerate(gross)) \
            or any(w.lower().strip(".") in _KEINE_NAMEN or _INSTITUTION.search(w) for w in woerter):
        return None
    return text, " ".join(titel_teile) or None


# Kundenstimme / Referenz: „Geschäftsführer der Muster GmbH", „Mandant seit 2010"
_FREMDE_FIRMA = re.compile(r"\b(?:gmbh|ag|kg|ohg|gbr|e\.\s?k\.|e\.\s?v\.|ug|se|mandant\w*|kunde\w*)\b", re.I)


# Firmen- oder Adresszeile („Hansen & Petersen Steuerberater PartG mbB · Holstenstraße 42 · 24103 Kiel")
_FIRMENZEILE = re.compile(r"\b\d{5}\b|\b(?:partg|mbb|partmbb)\b", re.I)


def ist_externe_funktion(funktion: str | None) -> bool:
    return bool(funktion) and bool(_FREMDE_FIRMA.search(funktion))


def _ist_funktion(zeile: str, f: Funktionen) -> bool:
    # Aufzählungen dürfen länger sein („Dipl.-Betriebswirt, Steuerberater, Fachberater für … (DStV e.V.), …")
    grenze = (220, 30) if zeile.count(",") >= 2 else (120, 12)
    return (len(zeile) <= grenze[0] and len(zeile.split()) <= grenze[1] and bool(f.muster_alle.search(zeile))
            and not ist_externe_funktion(zeile) and not _FIRMENZEILE.search(zeile))  # fmt: skip


# Kontaktzeilen zwischen Funktion und Qualifikation in einer Kachel („trenner@…", „Tel. 0431 …", „vCard")
_KONTAKTZEILE = re.compile(
    r"^(?:\S+@\S+|(?:tel|telefon|fon|fax|mobil|e-?mail|mail)\b.*|\+?\d[\d\s/()\-–.]{5,}|vcard|kontakt|mehr\s+erfahren|"
    r"profil|lebenslauf|zur\s+person)$",
    re.I,
)


def _funktion_saeubern(text: str) -> str:
    text = text.strip(" ,;:–-")
    # Klammer nur entfernen, wenn sie nicht geschlossen wird: „(DStV e.V.)" bleibt
    while text.startswith("(") and ")" not in text:
        text = text[1:].strip()
    while text.endswith(")") and text.count("(") < text.count(")"):
        text = text[:-1].strip()
    return text


def _beginnt_mit_name(zeile: str) -> bool:
    """„Lars Behrens – Steuerberater", „Sabine Schulz (Steuerfachwirtin)": neue Person, keine Funktionszeile."""
    return bool(name_zerlegen(zeile) or name_zerlegen(_TRENNER.split(zeile, maxsplit=1)[0]))


def _nur_titel(zeile: str) -> bool:
    """„Dipl.-Volkswirt", „Dr. rer. pol.", „M.Sc." – eine Zeile, die nur aus Titeln besteht."""
    m = _TITEL.match(zeile.strip() + " ")
    return bool(zeile.strip()) and m is not None and m.end() >= len(zeile.strip())


def erkenne_personen(
    text: str, funktionen: Funktionen | None = None, rahmen: set[str] | None = None
) -> list[ErkanntePerson]:
    """Personen mit Funktion aus zeilenweisem Seitentext (Kacheln, Listen, „Name – Funktion").

    Ein Name gilt nur mit einer Funktion aus config/personen_funktionen.yaml in derselben Zeile,
    in den Folgezeilen (Titelzeilen wie „Dipl.-Volkswirt" und bis zu zwei Kontaktzeilen dazwischen
    unterbrechen nicht) oder darüber – als Rolle der Kachel oder als Abschnittsüberschrift („Partner"),
    die für alle Kacheln darunter gilt bis zur nächsten Überschrift oder zu anderem Text.
    Zeilen aus Navigation/Fußzeile (`rahmen`) sind nie Namen, als Funktion unter einem Namen zählen
    sie („Steuerberater" steht oft auch im Logo).
    """
    f = funktionen or lade_funktionen()
    rahmen = rahmen or set()
    zeilen = [z.strip() for z in (text_normalisieren(text) or "").splitlines()]
    im_rahmen = {i for i, z in enumerate(zeilen) if " ".join(z.lower().split()) in rahmen}
    gefunden: list[ErkanntePerson] = []
    vergeben: set[int] = set()
    abschnitt: str | None = None  # Überschrift über mehreren Kacheln („Partner")

    def hinzu(name_titel, funktion: str, i: int) -> None:
        name, titel = name_titel
        schluessel = frozenset(name.lower().split())
        if any(frozenset(p.name.lower().split()) == schluessel for p in gefunden):
            return
        gefunden.append(ErkanntePerson(name, titel or None, _funktion_saeubern(funktion), i))

    def name_in(j: int):
        return None if j in im_rahmen or not zeilen[j] else name_zerlegen(zeilen[j])

    def naechster_name(j: int) -> bool:
        k = j + 1
        while k < len(zeilen) and not zeilen[k]:
            k += 1
        return k < len(zeilen) and name_in(k) is not None

    for i, z in enumerate(zeilen):
        if not z or i in vergeben:
            continue
        # „Jens Hansen – Steuerberater" / „Dr. Henrik Lorenzen, Geschäftsführer" / „Sabine Schulz (Steuerfachwirtin)"
        teile = _TRENNER.split(z, maxsplit=1)
        if i not in im_rahmen and len(teile) == 2 and (nt := name_zerlegen(teile[0])) and _ist_funktion(teile[1], f):
            hinzu(nt, teile[1], i)
            vergeben.add(i)
            continue
        nt = name_in(i)
        if nt is None:
            # Abschnittsüberschrift: kurze Funktionszeile direkt über einem Namen
            if _ist_funktion(z, f) and len(z.split()) <= 4 and not _beginnt_mit_name(z) and naechster_name(i):
                abschnitt = z
            elif not _KONTAKTZEILE.match(z) and not _nur_titel(z):
                abschnitt = None  # anderer Text beendet den Abschnitt
            continue
        # Folgezeilen: Funktionen, Titel, Kontaktzeilen („Sven Drews" / „Gesellschafter" / „Steuerberater")
        folge, titel_zusatz, kontakt = [], [], 0
        for j in range(i + 1, min(len(zeilen), i + 8)):
            zj = zeilen[j]
            if not zj or _beginnt_mit_name(zj) and j not in im_rahmen and not _ist_funktion(zj, f):
                break
            if j in im_rahmen and len(zj.split()) > 3:
                break  # Fußzeile („Hansen & Petersen Steuerberater PartG mbB · …") gehört nicht zur Kachel
            if _ist_funktion(zj, f) and not _beginnt_mit_name(zj):
                folge.append(j)
            elif _nur_titel(zj):
                titel_zusatz.append(j)  # „Dipl.-Volkswirt" zwischen Funktion und Berufsbezeichnung
            elif folge and kontakt < 2 and _KONTAKTZEILE.match(zj):
                kontakt += 1  # Kontaktzeile innerhalb der Kachel überspringen
            else:
                break
        titel = " ".join(x for x in [nt[1], *(zeilen[j] for j in titel_zusatz)] if x) or None
        funktion = ", ".join(zeilen[j].strip(" ,;") for j in folge)
        if folge:
            if abschnitt and abschnitt.lower() not in funktion.lower():
                funktion = f"{abschnitt}, {funktion}"
            hinzu((nt[0], titel), funktion, i)
            vergeben.update({i, *folge, *titel_zusatz})
        elif abschnitt:
            hinzu((nt[0], titel), abschnitt, i)  # Kachel unter der Überschrift („Partner"), ohne eigene Funktion
            vergeben.update({i, *titel_zusatz})
        elif i > 0 and zeilen[i - 1] and i - 1 not in vergeben and _ist_funktion(zeilen[i - 1], f) \
                and not _beginnt_mit_name(zeilen[i - 1]):
            hinzu((nt[0], titel), zeilen[i - 1], i)  # Funktion über dem Namen (Kachel mit Rolle als Überschrift)
            vergeben.update({i - 1, i})
        elif titel and (b := f.muster_berufstraeger.search(titel)):
            hinzu((nt[0], titel), b.group(0), i)  # „StB Justyna Slama": die Berufsbezeichnung steht vor dem Namen
            vergeben.update({i, *titel_zusatz})
    return gefunden


def ist_berufstraeger_funktion(funktion: str | None, f: Funktionen | None = None) -> bool:
    return bool(funktion) and bool((f or lade_funktionen()).muster_berufstraeger.search(funktion))


def ist_leitung_funktion(funktion: str | None, f: Funktionen | None = None) -> bool:
    return bool(funktion) and bool((f or lade_funktionen()).muster_leitung.search(funktion))


# ---------------------------------------------------------------------- Mitarbeiterzahl

_G = r"(?:[:*/_]?[iI]nnen|[:*/_]?[iI]n)?"  # Gender-Endungen: :innen, *innen, _innen, /innen, Innen
_PERSONEN_WORT = (
    r"(?:Mitarbeiterinnen\s+und\s+Mitarbeiter(?:n)?|Mitarbeiter(?:n)?" + _G + r"|Mitarbeitende(?:n)?|"
    r"Kolleginnen\s+und\s+Kollegen|Kolleg(?:en|" + _G[3:-2] + r")|Kolleg" + _G + r"|Beschäftigte(?:n)?|"
    r"Expertinnen\s+und\s+Experten|Expert(?:en|" + _G[3:-2] + r")|Experte" + _G + r"|Fachkräfte(?:n)?|"
    r"Berater(?:n)?" + _G + r"|Beraterinnen\s+und\s+Berater(?:n)?|Köpfe(?:n)?|Spezialist(?:en)?" + _G + r"|"
    r"Teammitglieder(?:n)?|Menschen|Personen)"
)
_EIGENSCHAFT = r"(?:(?:engagierte|qualifizierte|erfahrene|motivierte|kompetente|fachkundige|hochqualifizierte)[nr]?\s+)?"
_MITARBEITERZAHL = re.compile(
    r"(?<![\w.,])(?P<vor>über|mehr\s+als|rund|ca\.?|circa|etwa|knapp|fast|insgesamt)?\s*(?P<zahl>\d{1,3})"
    r"(?:\s*(?:-|\s)köpfig\w*|\s*" + _EIGENSCHAFT + _PERSONEN_WORT + r"(?![\w]))",
    re.I,
)
# „Team von 28", „Team aus über 20" – ohne Personenwort dahinter
_TEAM_VON = re.compile(
    r"\bTeam\s+(?:von|aus|mit)\s+(?P<vor>über|mehr\s+als|rund|ca\.?|circa|etwa|knapp|fast)?\s*(?P<zahl>\d{1,3})(?![\d.,])",
    re.I,
)
_UNTERGRENZE = {"über", "mehr als"}
_UNGEFAEHR = {"rund", "ca", "ca.", "circa", "etwa", "knapp", "fast"}


@dataclass(frozen=True)
class Mitarbeiterzahl:
    zahl: int | None  # genaue Angabe
    klasse: TeamKlasse | None  # bei Unter- oder ungefährer Angabe
    beleg: str


def mitarbeiterzahl(texte: list[str]) -> Mitarbeiterzahl | None:
    """Größte plausible Mitarbeiterangabe (2–999) aus den Seitentexten."""
    beste: tuple[int, Mitarbeiterzahl] | None = None
    for text in texte:
        for m in [*_MITARBEITERZAHL.finditer(text or ""), *_TEAM_VON.finditer(text or "")]:
            n = int(m.group("zahl"))
            if not 2 <= n <= 999:
                continue
            vor = " ".join((m.group("vor") or "").lower().split())
            beleg = " ".join(m.group(0).split())
            if vor in _UNTERGRENZE:
                eintrag, rang = Mitarbeiterzahl(None, TeamKlasse.aus_zahl(n + 1), beleg), n + 1
            elif vor in _UNGEFAEHR:
                eintrag, rang = Mitarbeiterzahl(None, TeamKlasse.aus_zahl(n), beleg), n
            else:
                eintrag, rang = Mitarbeiterzahl(n, None, beleg), n
            if beste is None or rang > beste[0]:
                beste = (rang, eintrag)
    return beste[1] if beste else None


# ---------------------------------------------------------------------- versteckte Quellen

def _json_ld_objekte(html: str) -> list[dict]:
    import json

    from bs4 import BeautifulSoup

    objekte: list[dict] = []

    def sammeln(x) -> None:
        if isinstance(x, list):
            for y in x:
                sammeln(y)
        elif isinstance(x, dict):
            objekte.append(x)
            for v in x.values():
                if isinstance(v, (list, dict)):
                    sammeln(v)

    for tag in BeautifulSoup(html, "lxml").find_all("script", type="application/ld+json"):
        try:
            sammeln(json.loads(tag.string or ""))
        except ValueError:
            continue
    return objekte


def personen_aus_json_ld(html: str | None, funktionen: Funktionen | None = None) -> list[ErkanntePerson]:
    """Personen aus strukturierten Daten (schema.org Person mit jobTitle)."""
    if not html or "ld+json" not in html:
        return []
    f = funktionen or lade_funktionen()
    gefunden = []
    for o in _json_ld_objekte(html):
        typ = o.get("@type")
        if (typ == "Person" or (isinstance(typ, list) and "Person" in typ)) and isinstance(o.get("name"), str):
            funktion = o.get("jobTitle") if isinstance(o.get("jobTitle"), str) else ""
            if (nt := name_zerlegen(o["name"])) and _ist_funktion(funktion, f):
                gefunden.append(ErkanntePerson(nt[0], nt[1], funktion, -1))
    return gefunden


def personen_aus_attributen(html: str | None, funktionen: Funktionen | None = None) -> list[ErkanntePerson]:
    """„Hans-Joachim Trenner, Geschäftsführer, Dipl.-Volkswirt, Steuerberater" in alt/title/aria-label eines
    Bildes – manche Teamseiten nennen die Qualifikationen nur dort. Nur mit Funktion aus der Liste."""
    if not html or not re.search(r"\b(?:alt|title|aria-label)=", html):
        return []
    from bs4 import BeautifulSoup

    f = funktionen or lade_funktionen()
    gefunden: list[ErkanntePerson] = []
    for el in BeautifulSoup(html, "lxml").find_all(True):
        for k in ("alt", "title", "aria-label"):
            wert = text_normalisieren(el.get(k)) if isinstance(el.get(k), str) else None
            if not wert or len(wert) > 220:
                continue
            teile = _TRENNER.split(wert, maxsplit=1)
            if len(teile) == 2 and (nt := name_zerlegen(teile[0])) and _ist_funktion(teile[1], f):
                if not any(p.name == nt[0] for p in gefunden):
                    gefunden.append(ErkanntePerson(nt[0], nt[1], _funktion_saeubern(teile[1]), -1))
    return gefunden


@dataclass
class VersteckteQuellen:
    """Wo auf einer Seite Text steckt, der nicht im sichtbaren Text landet (Diagnose)."""

    html_zeichen: int
    text_zeichen: int
    noscript_zeichen: int = 0
    skript_zeichen: int = 0  # Inline-Skripte (z. B. JSON für ein JavaScript-Team-Widget)
    json_ld_typen: tuple[str, ...] = ()
    json_ld_personen: int = 0
    attribut_namen: int = 0  # Namen in alt/title/aria-label/data-*
    skript_namen: int = 0  # Namen in Inline-Skripten
    noscript_namen: int = 0

    @property
    def anteil(self) -> float:
        return self.text_zeichen / self.html_zeichen if self.html_zeichen else 1.0


_NAME_IM_TEXT = re.compile(r"(?<![\w-])([A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?\s+[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?)(?![\w-])")


def _namen_zaehlen(texte: list[str]) -> int:
    namen = {n for t in texte for n in _NAME_IM_TEXT.findall(t) if name_zerlegen(n)}
    return len(namen)


def versteckte_quellen(html: str | None, text: str | None) -> VersteckteQuellen:
    from bs4 import BeautifulSoup

    html = html or ""
    q = VersteckteQuellen(len(html), len(text or ""))
    if not html:
        return q
    soup = BeautifulSoup(html, "lxml")
    noscript = [n.get_text(" ") for n in soup.find_all("noscript")]
    skripte = [s.string or "" for s in soup.find_all("script") if not s.get("src") and s.get("type") != "application/ld+json"]
    attribute = [str(v) for el in soup.find_all(True) for k, v in el.attrs.items()
                 if k in ("alt", "title", "aria-label") or k.startswith("data-")]  # fmt: skip
    q.noscript_zeichen = sum(len(n) for n in noscript)
    q.skript_zeichen = sum(len(s) for s in skripte)
    objekte = _json_ld_objekte(html)
    q.json_ld_typen = tuple(sorted({str(o.get("@type")) for o in objekte if o.get("@type")}))
    q.json_ld_personen = sum(1 for o in objekte if o.get("@type") == "Person")
    q.attribut_namen = _namen_zaehlen(attribute)
    q.skript_namen = _namen_zaehlen([s.replace("\\u00", "") for s in skripte])
    q.noscript_namen = _namen_zaehlen(noscript)
    return q
