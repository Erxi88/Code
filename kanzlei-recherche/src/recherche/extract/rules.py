"""Stufe 1: regelbasierte Extraktion (Straße, PLZ, Ort, Telefon, E-Mail) aus dem Impressum.

Arbeitet zeilenweise auf dem Text aus crawl.html_zu_text(). Fallstrick: Fast jedes
Kanzlei-Impressum nennt auch die Steuerberaterkammer (Pflichtangabe), oft auch Hoster und
Webdesigner. Adressen und Telefonnummern in solchen Fremd-Abschnitten werden ignoriert
(config/impressum_ausschluss.yaml); genommen wird die erste verbleibende Angabe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..normalize import normalize_telefon, rechtsform_aus_text
from ..search.dedupe import adresse_key
from ..textnorm import text_normalisieren
from .base import RegelErgebnis

_PLZ_ORT = re.compile(
    r"(?<![\d.,])(?:D\s*-\s*)?(\d{5})\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-/() ]*?[A-Za-zäöüß.)])"
    r"(?=\s*$|\s*[,;|·•–]|\s+(?:Tel|Fon|Fax|Telefon|E-?Mail|Mail|Deutschland|Germany|DE)\b)"
)
_STRASSE = re.compile(
    r"^([A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-' ]{1,50}?\.?\s+\d{1,4}\s*[a-zA-Z]?(?:\s*[-/]\s*\d{1,4}\s*[a-zA-Z]?)?)$"
)
# Zahlen nach diesen Kennzeichen sind keine PLZ (Registernummern, Konten, Jahresangaben …)
_KEINE_PLZ_DAVOR = re.compile(r"(?:HR\s?[AB]|PR|VR|GnR|Nr\.?|Nummer|Konto|IBAN|BLZ|seit|ab|bis|von|ca\.?)\s*:?\s*$", re.I)
_STRASSE_NEIN = re.compile(r"^(tel|fon|fax|postfach|ust|hrb|hra|amtsgericht|steuer-?nr|steuernummer|pr \d)", re.I)
_TEL_LABEL = re.compile(
    r"(?:(?<![a-zäöü])(?:tel(?:efon)?(?:nummer)?|rufnummer|fon|phone|t|zentrale|büro|sekretariat)|[☎📞✆])"
    r"\s*\.?\s*:?\s*(\(?\+?\d[\d\s/()\-–.]{4,}\d)", re.I
)
_FAX_DAVOR = re.compile(r"(fax|telefax|\bF)\s*\.?\s*:?\s*$", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_AT = re.compile(r"\s*(?:\(|\[|\{)\s*(?:at|ät|@)\s*(?:\)|\]|\})\s*", re.I)
_DOT = re.compile(r"\s*(?:\(|\[|\{)\s*(?:dot|punkt)\s*(?:\)|\]|\})\s*", re.I)


@dataclass
class Ausschluss:
    abschnitt_zeilen: int = 6
    abschnitt_muster: list[re.Pattern] = field(default_factory=list)
    adressen: dict[str, str] = field(default_factory=dict)  # adresse_key → Name der Institution
    email_nachrangig: tuple[str, ...] = ()

    @classmethod
    def laden(cls, pfad: str | Path | None) -> Ausschluss:
        if pfad is None or not Path(pfad).exists():
            return cls()
        d = yaml.safe_load(Path(pfad).read_text(encoding="utf-8-sig")) or {}
        return cls(
            abschnitt_zeilen=int(d.get("abschnitt_zeilen", 6)),
            abschnitt_muster=[re.compile(m, re.I) for m in d.get("abschnitt_woerter", [])],
            adressen={k: a.get("name") or "" for a in d.get("adressen", [])
                      if (k := adresse_key(a.get("strasse"), str(a.get("plz"))))},
            email_nachrangig=tuple(d.get("email_nachrangig", [])),
        )

    def fremd_zeilen(self, zeilen: list[str]) -> set[int]:
        """Indizes der Zeilen in Fremd-Abschnitten (Kammer, Hoster, Webdesign …)."""
        fremd: set[int] = set()
        for i, z in enumerate(zeilen):
            if any(m.search(z) for m in self.abschnitt_muster):
                fremd.update(range(i, min(len(zeilen), i + self.abschnitt_zeilen + 1)))
        return fremd


@dataclass
class Adresse:
    strasse: str | None
    plz: str
    ort: str
    zeile: int
    zusatz: str | None = None


def _ort_bereinigen(ort: str) -> str:
    ort = re.sub(r"\s+(?:OT|Ortsteil)\s.*$", "", ort.strip())
    return ort.strip(" .,-")


_ZUSATZ_WORT = (
    r"(?:Haus|Gebäude|Geb\.|Eingang|Aufgang|Etage|Stockwerk|Stock|\d+\.\s*(?:OG|Etage|Stock|Obergeschoss)|"
    r"OG|EG|DG|UG|Obergeschoss|Erdgeschoss|Dachgeschoss|Hinterhof|Hinterhaus|Seitenflügel|Zimmer|Raum|Büro|c/o)"
)
_ZUSATZ_ZEILE = re.compile(rf"^{_ZUSATZ_WORT}(?=\b|\s|$)", re.I)
_STRASSE_MIT_ZUSATZ = re.compile(
    rf"^(?P<strasse>.+?\d{{1,4}}\s*[a-zA-Z]?(?:\s*-\s*\d{{1,4}}\s*[a-zA-Z]?)?)\s*(?:/|,|\s-\s|–|\(|\s)\s*"
    rf"(?P<zusatz>{_ZUSATZ_WORT}(?=\b|\s|$).*)$",
    re.I,
)
_SEGMENTE = re.compile(r"\s*[,;|·•–]\s*")


def ist_zusatz(text: str) -> bool:
    return bool(_ZUSATZ_ZEILE.match(text.strip())) and len(text.strip()) <= 60


def strasse_zerlegen(text: str) -> tuple[str, str | None] | None:
    """'Luruper Chaussee 125 / Haus 8 Süd' → ('Luruper Chaussee 125', 'Haus 8 Süd'); keine Straße → None."""
    text = text.strip()
    if _ist_strasse(text):
        return text, None
    if (m := _STRASSE_MIT_ZUSATZ.match(text)) and _ist_strasse(m.group("strasse").strip()):
        zusatz = m.group("zusatz").strip(" ,/")
        if zusatz.endswith(")") and "(" not in zusatz:
            zusatz = zusatz[:-1].strip()
        return m.group("strasse").strip(), zusatz
    return None


def _strasse_aus_segmenten(segmente: list[str]) -> tuple[str, str | None] | None:
    """Letzte Straße in einer Segmentliste; nachfolgende Zusätze werden mitgenommen
    („Haus 8", auch in mehreren Teilen: „Haus 8 · Süd")."""
    segmente = [s for s in segmente if s]
    for k in range(len(segmente) - 1, -1, -1):
        if ist_zusatz(segmente[k]) or not (zerlegt := strasse_zerlegen(segmente[k])):
            continue
        rest = segmente[k + 1:]
        # dahinter nur Zusätze: erstes Segment mit Zusatzwort, danach kurze Ergänzungen („Süd")
        if rest and not (ist_zusatz(rest[0]) or zerlegt[1]):
            return None
        if any(len(s) > 30 for s in rest[1:]):
            return None
        strasse, zusatz = zerlegt
        return strasse, ", ".join(x for x in [zusatz, *rest] if x) or None
    return None


_ZEILE_OFFEN = re.compile(r"\s[/,;·•–-]\s*$|/$")


def _offene_zeilen_verbinden(zeilen: list[str]) -> list[str]:
    """„Luruper Chaussee 125 /" + Zeilenumbruch + „Haus 8 Süd" → eine Zeile; die Folgezeile
    bleibt leer, damit die Zeilennummern (Fremd-Abschnitte) stimmen."""
    zeilen = list(zeilen)
    for i in range(len(zeilen) - 1):
        if _ZEILE_OFFEN.search(zeilen[i]) and zeilen[i + 1].strip() and _ist_strasse(_ZEILE_OFFEN.sub("", zeilen[i])):
            zeilen[i] = f"{_ZEILE_OFFEN.sub('', zeilen[i]).rstrip()} / {zeilen[i + 1].strip()}"
            zeilen[i + 1] = ""
    return zeilen


def adressen(zeilen: list[str]) -> list[Adresse]:
    """Alle Adressen (PLZ + Ort) mit Straße aus derselben Zeile oder bis zu drei Zeilen darüber.

    Zusätze wie „Haus 8 Süd", „2. OG" oder „c/o …" – hinter der Hausnummer, in einer eigenen Zeile
    oder vor der Straße – werden toleriert und als Adresszusatz zurückgegeben.
    """
    gefunden = []
    zeilen = _offene_zeilen_verbinden(zeilen)
    for i, z in enumerate(zeilen):
        for m in _PLZ_ORT.finditer(z):
            plz, ort = m.group(1), _ort_bereinigen(m.group(2))
            if not ort or len(ort) > 40 or _KEINE_PLZ_DAVOR.search(z[: m.start()]):
                continue
            if len(ort) < 3 or (ort.isupper() and len(ort) <= 4):  # „KI" aus „HRB 12345 KI"
                continue
            strasse = zusatz = None
            if davor := _SEGMENTE.split(z[: m.start()].strip(" ,;|·•–")):
                if treffer := _strasse_aus_segmenten(davor):
                    strasse, zusatz = treffer
            if strasse is None:
                zusaetze: list[str] = []
                for j in (i - 1, i - 2, i - 3):
                    if j < 0 or not zeilen[j].strip():
                        continue
                    zeile = zeilen[j].strip()
                    if ist_zusatz(zeile):
                        zusaetze.insert(0, zeile)
                        continue
                    if treffer := _strasse_aus_segmenten(_SEGMENTE.split(zeile)):
                        strasse, zusatz = treffer
                        zusatz = ", ".join(x for x in [zusatz, *zusaetze] if x) or None
                        if j > 0 and ist_zusatz(zeilen[j - 1]) and zeilen[j - 1].lower().startswith("c/o"):
                            zusatz = ", ".join(x for x in [zeilen[j - 1].strip(), zusatz] if x)
                    break
            gefunden.append(Adresse(strasse, plz, ort, i, zusatz))
    return gefunden


def _ist_strasse(text: str) -> bool:
    text = text.strip()
    return bool(_STRASSE.match(text)) and not _STRASSE_NEIN.match(text) and len(text) <= 60


def telefone(zeilen: list[str]) -> list[tuple[str, int]]:
    """(normalisierte Nummer, Zeile) aller als Telefon gekennzeichneten Nummern (kein Fax)."""
    ergebnis = []
    for i, z in enumerate(zeilen):
        for m in _TEL_LABEL.finditer(z):
            if _FAX_DAVOR.search(z[: m.start()]):
                continue
            label = z[m.start() : m.start(1)].lower()
            if label.strip(" .:").startswith("fax"):
                continue
            if (nummer := normalize_telefon(m.group(1))) is not None:
                ergebnis.append((nummer, i))
    return ergebnis


def entschleiern(text: str) -> str:
    return _DOT.sub(".", _AT.sub("@", text))


def emails(text: str, links: list[str] | None = None) -> list[str]:
    """E-Mail-Adressen aus mailto-Links und Text (auch „info (at) kanzlei.de"), Reihenfolge erhalten."""
    # Text vor mailto-Links: der Link steht oft in der Fußzeile („wp@"), die Adresse im Impressum („info@")
    gefunden = [m.group(0).lower().rstrip(".") for m in _EMAIL.finditer(entschleiern(text))]
    for href in links or []:
        if href.lower().startswith("mailto:"):
            adresse = href[7:].split("?", 1)[0].strip()
            if _EMAIL.fullmatch(adresse):
                gefunden.append(adresse.lower())
    return list(dict.fromkeys(gefunden))


def beste_email(kandidaten: list[str], domain: str | None, nachrangig: tuple[str, ...]) -> str | None:
    def rang(mail: str) -> tuple[int, int]:
        lokal, _, host = mail.partition("@")
        eigene = domain is not None and (host == domain or host.endswith("." + domain))
        return (0 if eigene else 1, 1 if lokal.startswith(nachrangig) else 0)

    return min(kandidaten, key=rang) if kandidaten else None


_MOBIL = re.compile(r"^\+49 ?1[567]")
# Nummer ohne Beschriftung in eigener Zeile („0431 38575740", „+49 (0)431 3857 5740")
_NUMMER_ALLEIN = re.compile(r"^(?:\(?0|\+)[\d\s/()\-–.]{6,}\d$")  # mit Vorwahl – keine Steuer-/Registernummer


def ist_mobil(nummer: str | None) -> bool:
    return bool(nummer) and bool(_MOBIL.match(nummer))


def _festnetz_zuerst(nummern: list[str]) -> list[str]:
    return [n for n in nummern if not ist_mobil(n)] + [n for n in nummern if ist_mobil(n)]


def beste_telefon(nummern: list[str]) -> str | None:
    """Festnetz vor Mobil vor 0800/0180 (Prüfkatalog 2); innerhalb der Klasse die erste in Quell-Reihenfolge."""
    from ..normalize import ist_service

    def klasse(n: str) -> int:
        return 2 if ist_service(n) else 1 if ist_mobil(n) else 0

    nummern = list(dict.fromkeys(n for n in nummern if n))
    return min(nummern, key=lambda n: (klasse(n), nummern.index(n))) if nummern else None


def _fremde_institution(zeilen: list[str], i: int, name: str) -> bool:
    """Steht der Name der Institution („Steuerberaterkammer Schleswig-Holstein") bei der Adresse?"""
    woerter = [w for w in re.split(r"\W+", name.lower()) if len(w) >= 4]
    umgebung = " ".join(zeilen[max(0, i - 3): i + 1]).lower()
    return bool(woerter) and any(w in umgebung for w in woerter[:2])


def telefon_kandidaten(text: str, ausschluss: Ausschluss | None = None, impressum: bool = False) -> list[tuple[str, str]]:
    """(Nummer, Art) aller Telefonkandidaten einer Seite – für die Diagnose (fixture-prompt)."""
    zeilen = (text_normalisieren(text) or "").splitlines()
    fremd = (ausschluss or Ausschluss()).fremd_zeilen(zeilen)
    ergebnis = [(n, "beschriftet" + (", Fremd-Abschnitt" if i in fremd else "")) for n, i in telefone(zeilen)]
    if impressum:
        for i, z in enumerate(zeilen):
            if _NUMMER_ALLEIN.match(z.strip()) and (n := normalize_telefon(z)):
                ergebnis.append((n, "unbeschriftet" + (", Fremd-Abschnitt" if i in fremd else "")))
    return [(n, art + (", mobil" if ist_mobil(n) else "")) for n, art in ergebnis]


_NUMMER_IM_HTML = re.compile(r"(?:\+49|(?<![\d.])0)[\d\s()/\-–.]{7,22}\d")


def _entfernt_durch(html: str, nummer_roh: str) -> str | None:
    """Welcher Bereinigungsschritt die Nummer aus dem Text entfernt (Diagnose)."""
    from bs4 import BeautifulSoup

    from ..crawl import _COOKIE_ELEMENTE, _ENTFERNEN

    soup = BeautifulSoup(html, "lxml")
    for s in soup.find_all(string=lambda x: x and nummer_roh in x):
        for el in s.parents:
            if el.name in _ENTFERNEN:
                return f"<{el.name}> wird entfernt"
            kennung = " ".join([el.get("id") or "", *(el.get("class") or [])]) if hasattr(el, "get") else ""
            if kennung.strip() and _COOKIE_ELEMENTE.search(kennung):
                return f"Cookie-Filter (id/class „{kennung.strip()[:40]}“)"
    return None


def telefon_im_html(html: str | None) -> list[tuple[str, str]]:
    """(Nummer, Fundstelle) aller telefonartigen Zahlen im rohen HTML – auch in Attributen, Skripten,
    <template>/<svg> und tel:-Links. Zeigt, ob eine Nummer vor der Textumwandlung verloren geht.
    Ohne Trennzeichen zählt eine Zahl nur als tel:-Link (lange Ziffernfolgen sind meist SVG-Pfade)."""
    gefunden: list[tuple[str, str]] = []
    for m in _NUMMER_IM_HTML.finditer(html or ""):
        roh = m.group(0)
        davor = html[max(0, m.start() - 60): m.start()]
        tel_link = davor.rstrip().endswith("tel:")
        if not (n := normalize_telefon(roh)) or (not tel_link and not re.search(r"[\s/()\-–.]", roh)):
            continue
        stelle = ("tel:-Link" if tel_link else
                  "Skript" if davor.rfind("<script") > davor.rfind("</script") else
                  "Attribut" if re.search(r"=\s*[\"'][^\"'<>]*$", davor) else "Text")
        tag = re.findall(r"<(\w+)", davor)
        stelle += f", in <{tag[-1]}>" if tag else ""
        if stelle.startswith("Text") and (grund := _entfernt_durch(html, roh)):
            stelle += f", {grund}"
        if (n, stelle) not in gefunden:
            gefunden.append((n, stelle))
    return gefunden


def festnetz_aus_html(seiten_html: list[str | None], plz: str | None) -> str | None:
    """Rückfall, wenn der bereinigte Text kein Festnetz liefert: Festnetznummer aus Text oder tel:-Link im
    rohen HTML – bei mehreren die, die der Kanzlei-PLZ am nächsten steht (Standort-Blöcke mit mehreren
    Niederlassungen: „24103 Kiel … 0431 …", „24768 Rendsburg … 04331 …")."""
    beste: tuple[int, int, str] | None = None
    for rang, html in enumerate(h for h in seiten_html if h):
        orte = [m.start() for m in re.finditer(re.escape(plz), html)] if plz else []
        for m in _NUMMER_IM_HTML.finditer(html):
            roh = m.group(0)
            davor = html[max(0, m.start() - 60): m.start()]
            tel_link = davor.rstrip().endswith("tel:")
            if davor.rfind("<script") > davor.rfind("</script") or (not tel_link and re.search(r"=\s*[\"'][^\"'<>]*$", davor)):
                continue
            if not tel_link and not re.search(r"[\s/()\-–.]", roh):
                continue
            if not (n := normalize_telefon(roh)) or ist_mobil(n) or _FAX_DAVOR.search(re.sub(r"<[^>]+>", " ", davor)):
                continue
            abstand = min((abs(m.start() - o) for o in orte), default=10**6)
            kandidat = (abstand, rang, n)
            if beste is None or kandidat < beste:
                beste = kandidat
    return beste[2] if beste else None


def extract_impressum(
    text: str,
    ausschluss: Ausschluss | None = None,
    links: list[str] | None = None,
    domain: str | None = None,
    weitere_texte: list[tuple[str, list[str]]] | None = None,
    rahmen: set[str] | None = None,
    schema_telefone: list[str] | None = None,
) -> RegelErgebnis:
    """Erste Adresse, Telefonnummer und E-Mail außerhalb von Fremd-Abschnitten.

    `weitere_texte` (z. B. Kontakt- und Startseite, zuletzt Meta-Beschreibungen) werden in Reihenfolge nur
    für Felder herangezogen, die im Impressum fehlen. Telefon (Prüfkatalog 2): Festnetz vor Mobil vor
    0800/0180; innerhalb der Klasse tel:-Link → Schema.org (`schema_telefone`) → Impressum-Text →
    Kontaktseite → übrige Seiten. tel:-Links mit einer Nummer aus einem Fremd-Abschnitt (Kammer) zählen nicht.
    E-Mail: Impressum-Text vor Navigation/Fußzeile (`rahmen`) vor mailto-Links.
    """
    ausschluss = ausschluss or Ausschluss()
    rahmen = rahmen or set()
    text = text_normalisieren(text) or ""
    weitere_texte = [(text_normalisieren(x) or "", lnks) for x, lnks in weitere_texte or []]
    erg = RegelErgebnis()
    mails: list[str] = []
    tel_links: list[str] = []
    text_nummern: list[str] = []
    for nr, (txt, lnks) in enumerate([(text, links or []), *(weitere_texte or [])]):
        zeilen = txt.splitlines()
        fremd = ausschluss.fremd_zeilen(zeilen)
        if erg.plz is None:
            kandidaten = [a for a in adressen(zeilen) if a.zeile not in fremd]
            bekannt = {id(a): ausschluss.adressen.get(adresse_key(a.strasse, a.plz)) for a in kandidaten}
            gewaehlt = next((a for a in kandidaten if bekannt[id(a)] is None), None)
            if gewaehlt is None:
                # nur eine Adresse der Ausschlussliste, nicht im Kammer-Abschnitt und ohne den Namen der
                # Institution davor: die Kanzlei sitzt dort (z. B. im Haus der Kammer) – mit Hinweis
                gewaehlt = next((a for a in kandidaten if bekannt[id(a)]
                                 and not _fremde_institution(zeilen, a.zeile, bekannt[id(a)])), None)
                if gewaehlt is not None:
                    erg.hinweise.append(f"Adresse {gewaehlt.strasse}, {gewaehlt.plz} gleicht {bekannt[id(gewaehlt)]} "
                                        "(Ausschlussliste) – als Kanzleiadresse übernommen, bitte prüfen")
            if gewaehlt is not None:
                erg.strasse, erg.plz, erg.ort, erg.adresszusatz = gewaehlt.strasse, gewaehlt.plz, gewaehlt.ort, gewaehlt.zusatz
        fremde_nummern = {n for n, i in telefone(zeilen) if i in fremd}
        tel_links += [n for h in lnks if h.lower().startswith("tel:") and (n := normalize_telefon(h[4:]))
                      and n not in fremde_nummern]
        text_nummern += [n for n, i in telefone(zeilen) if i not in fremd]
        if nr == 0:  # Impressum: unbeschriftete Nummer in eigener Zeile (nicht hinter „Fax"/„F:")
            for i, z in enumerate(zeilen):
                davor = zeilen[i - 1] if i > 0 else ""
                if i in fremd or not _NUMMER_ALLEIN.match(z.strip()) or re.search(r"(?:fax|\bF)\W*$", davor, re.I):
                    continue
                if n := normalize_telefon(z):
                    text_nummern.append(n)
        if not mails:
            eigene = [z for i, z in enumerate(zeilen) if i not in fremd and " ".join(z.lower().split()) not in rahmen]
            im_rahmen = [z for i, z in enumerate(zeilen) if i not in fremd and " ".join(z.lower().split()) in rahmen]
            mails = emails("\n".join(eigene)) + emails("\n".join(im_rahmen),
                                                       [h for h in lnks if h.lower().startswith("mailto:")])
            mails = list(dict.fromkeys(mails))
    erg.telefon = beste_telefon([*tel_links, *(schema_telefone or []), *text_nummern])
    erg.email = beste_email(mails, domain, ausschluss.email_nachrangig)
    return erg


def rechtsform_aus_name(name: str | None) -> str | None:
    """Rechtsform, wenn sie im Kanzleinamen steht (Rückfall, wenn das LLM keine liefert).
    Tabelle der Varianten: config/rechtsformen.yaml."""
    return rechtsform_aus_text(name)
