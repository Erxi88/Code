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
from ..normalize import PlzPruefung, normalize_rechtsform, normalize_telefon
from ..search.dedupe import adresse_key, name_tokens, telefon_key
from .base import LlmErgebnis, LlmPerson, RegelErgebnis
from .personen import (
    ErkanntePerson,
    Mitarbeiterzahl,
    erkenne_personen,
    ist_berufstraeger_funktion,
    ist_leitung_funktion,
    mitarbeiterzahl,
    name_zerlegen,
)
from .rules import Ausschluss, rechtsform_aus_name


def _text(s: Seite) -> str:
    return html_zu_text(s.html) if s.html else (s.text or "")

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


def ist_leitung(p: Ansprechpartner | LlmPerson) -> bool:
    return ist_leitung_funktion(p.funktion)


def _namens_tokens(name: str) -> set[str]:
    name = _TITEL.sub(" ", name.lower())
    return {t for t in re.split(r"[^a-zäöüß]+", name) if len(t) >= 2}


def gleiche_person(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ta, tb = _namens_tokens(a), _namens_tokens(b)
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def _funktion_vereinigen(a: str | None, b: str | None) -> str | None:
    teile: list[str] = []
    for f in (a, b):
        for teil in re.split(r"\s*[,;/&]\s*|\s+und\s+", f or ""):
            if teil and not any(teil.lower() == x.lower() for x in teile):
                teile.append(teil)
    return ", ".join(teile) or None


def personen_vereinigen(personen: list[LlmPerson]) -> list[LlmPerson]:
    """Mehrfache Nennungen derselben Person zusammenführen (Funktionen und Titel vereinigt).

    Das LLM nennt Partner oft zweimal – aus dem Impressum nur als „Partner", von der Teamseite
    als „Steuerberater, Partner". Würde die erste Nennung gewinnen, ginge die Berufsbezeichnung verloren.
    """
    ergebnis: list[LlmPerson] = []
    for p in personen:
        for i, q in enumerate(ergebnis):
            if gleiche_person(p.name, q.name):
                name = q.name if len(q.name) >= len(p.name) else p.name
                ergebnis[i] = LlmPerson(name=name, titel=q.titel or p.titel,
                                        funktion=_funktion_vereinigen(q.funktion, p.funktion))
                break
        else:
            ergebnis.append(p)
    return ergebnis


_VERTRETUNG = re.compile(
    r"vertretungsberechtigt\w*|vertreten\s+durch|gesellschafter\w*|geschäftsführ\w*|geschaeftsfuehr\w*|"
    r"inhaber\w*|partner(?:in|innen)?\b(?!schafts)|vorstand",
    re.I,
)
_BERUFSBEZEICHNUNG = re.compile(r"berufsbezeichnung\w*\s*:?\s*(.*)", re.I)
_BEZEICHNUNGEN = {
    "Steuerberater": re.compile(r"steuerberater", re.I),
    "Wirtschaftsprüfer": re.compile(r"wirtschaftspr[uü]fer", re.I),
    "Rechtsanwalt": re.compile(r"rechtsanw[aä]lt", re.I),
    "vereidigter Buchprüfer": re.compile(r"buchpr[uü]fer", re.I),
}
ABSCHNITT_ZEILEN = 8


def _namen_aus(text: str) -> list[str]:
    """Namen aus „die Partner: Dipl.-Kfm. Jens Hansen, Maren Petersen und Ole Brandt"."""
    text = text.rsplit(":", 1)[-1]
    namen = []
    for teil in re.split(r"\s*[,;]\s*|\s+und\s+|\s+sowie\s+|\s*&\s*", text):
        woerter = teil.split()
        for start in range(min(3, len(woerter))):  # einleitende Wörter („die Partner") überspringen
            if nt := name_zerlegen(" ".join(woerter[start:])):
                namen.append(nt[0])
                break
    return namen


def vertretungsberechtigte(impressum: str, fremd: set[int] | None = None) -> tuple[list[str], str | None]:
    """Im Impressum genannte Vertretungsberechtigte und – falls genau eine im selben Abschnitt
    (±8 Zeilen, außerhalb von Kammer-/Fremd-Abschnitten) – ihre Berufsbezeichnung."""
    zeilen = impressum.splitlines()
    fremd = fremd or set()
    namen: list[str] = []
    bezeichnung: str | None = None
    for i, z in enumerate(zeilen):
        if i in fremd or not (m := _VERTRETUNG.search(z)) or len(z) > 300:
            continue
        # Namen hinter dem Schlüsselwort – oder, wenn dort keine stehen, in den nächsten Zeilen
        gefunden_namen = _namen_aus(z[m.end():])
        if not gefunden_namen:
            for j in (i + 1, i + 2):
                if j < len(zeilen) and j not in fremd:
                    gefunden_namen += _namen_aus(zeilen[j])
        for name in gefunden_namen:
            if not any(gleiche_person(name, n) for n in namen):
                namen.append(name)
        bereich = range(max(0, i - ABSCHNITT_ZEILEN), min(len(zeilen), i + ABSCHNITT_ZEILEN + 1))
        gefunden = set()
        for j in bereich:
            if j in fremd or not (b := _BERUFSBEZEICHNUNG.search(zeilen[j])):
                continue
            text = b.group(1) or (zeilen[j + 1] if j + 1 < len(zeilen) else "")
            gefunden |= {name for name, muster in _BEZEICHNUNGEN.items() if muster.search(text)}
        if len(gefunden) == 1 and bezeichnung is None:
            bezeichnung = gefunden.pop()
    return namen, bezeichnung


def ansprechpartner(
    llm: LlmErgebnis | None,
    erkannte: list[ErkanntePerson] | None = None,
    impressum: str | None = None,
    fremd: set[int] | None = None,
) -> list[Ansprechpartner]:
    """Berufsträger und Leitung aus LLM, Regel-Erkennung und Impressum.

    Reihenfolge: Verantwortliche/r laut Impressum, dann Leitung, dann Berufsträger. Vertretungs-
    berechtigte gelten als Berufsträger, wenn im selben Impressum-Abschnitt genau eine
    Berufsbezeichnung steht.
    """
    roh: list[LlmPerson] = list(llm.personen) if llm else []
    roh += [LlmPerson(name=p.name, titel=p.titel, funktion=p.funktion) for p in erkannte or []]
    vertreter, bezeichnung = vertretungsberechtigte(impressum, fremd) if impressum else ([], None)
    # Hauptansprechpartner: erste/r Vertretungsberechtigte laut Impressum, sonst die vom LLM genannte Person
    verantwortlich = vertreter[0] if vertreter else (llm.verantwortliche_person if llm else None)
    for v in vertreter:
        if not any(gleiche_person(v, p.name) for p in roh):
            roh.append(LlmPerson(name=v, funktion="laut Impressum vertretungsberechtigt"))
    personen = personen_vereinigen(roh)
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
        """Vertretungsberechtigte in Impressum-Reihenfolge, dann Leitung, dann übrige Berufsträger –
        jeweils in der Reihenfolge der Teamseite (sonst der LLM-Liste)."""
        for i, v in enumerate(vertreter):
            if gleiche_person(p.name, v):
                return (0, i)
        if not vertreter and gleiche_person(p.name, verantwortlich):
            return (0, 0)
        pos = next((i for i, n in enumerate(team_reihenfolge) if gleiche_person(p.name, n)), None)
        pos = pos if pos is not None else 1000 + auswahl.index(p)
        return (1 if ist_leitung(p) else 2, pos)

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
    abweichungen: list[str] = []

    # ---- Name: Discovery (Stufe B) > LLM
    llm_name = llm.kanzleiname if llm else None
    name = disc["name"] or llm_name
    if disc["name"] and llm_name and not (name_tokens(disc["name"]) & name_tokens(llm_name)):
        abweichungen.append(f"Name: {disc['quelle_name']} „{disc['name']}“ / Website „{llm_name}“")

    # ---- Adresse als Einheit: Discovery > Regeln > LLM
    web_adresse = (regel.strasse, regel.plz, regel.ort) if regel.plz else (
        (llm.strasse, llm.plz, llm.ort) if llm and llm.plz else (None, None, None)
    )
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
    rechtsform = normalize_rechtsform(llm.rechtsform if llm else None) or rechtsform_aus_name(name)

    # ---- Personen und Teamgröße: Regel-Erkennung auf den Teamseiten + LLM + Impressum
    team_seiten = [seiten[t] for t in (Seitentyp.TEAM, Seitentyp.TEAM2) if t in seiten]
    erkannte: list[ErkanntePerson] = []
    for s in team_seiten:
        for p in erkenne_personen(_text(s)):
            if not any(gleiche_person(p.name, q.name) for q in erkannte):
                erkannte.append(p)
    impressum_text = _text(seiten[Seitentyp.IMPRESSUM]) if Seitentyp.IMPRESSUM in seiten else None
    fremd = ausschluss.fremd_zeilen(impressum_text.splitlines()) if ausschluss and impressum_text else None
    personen = ansprechpartner(llm, erkannte, impressum_text, fremd)
    angabe = mitarbeiterzahl([_text(s) for s in seiten.values()])
    zahl, klasse, basis = teamgroesse(llm, team_seiten, erkannte, angabe)
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
    adresszusatz = regel.adresszusatz if regel.plz == plz and (strasse or "") == (regel.strasse or "") else None
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
        berufstraeger_anzahl=sum(1 for p in personen if ist_berufstraeger(p)) if hat_personeninfo else None,
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
