"""Zusammenführung von Discovery, Regeln und LLM zu einer Kanzlei.

Vorrang:
- Adresse (als Einheit Straße/PLZ/Ort) und Telefon: Discovery (Stufe B) > Regeln > LLM
- Name: Discovery (Stufe B) > LLM. Websuche-Titel gelten NICHT als Discovery-Name.
- E-Mail: Regeln > LLM; Rechtsform: LLM > aus dem Namen
- Discovery-Daten werden nie überschrieben. Unterscheiden sich Discovery und Website,
  steht ein Hinweis in „Abweichung".

Ansprechpartner sind nur Berufsträger und Leitung; die Teamgröße zählt alle Personen.
Konfidenz nach der (vorläufigen) Regel aus CLAUDE.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

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
from ..normalize import PlzPruefung, normalize_telefon
from ..search.dedupe import adresse_key, name_tokens, telefon_key
from .base import LlmErgebnis, LlmPerson, RegelErgebnis
from .rules import rechtsform_aus_name

BERUFSTRAEGER = re.compile(
    r"steuerberater|\bstb\b|wirtschaftspr[uü]fer|\bwp\b|rechtsanw[aä]lt|\bra\b|vereidigte[rn]?\s+buchpr[uü]fer|"
    r"\bvbp\b|steuerbevollm[aä]chtigt",
    re.I,
)
LEITUNG = re.compile(
    r"inhaber|partner(?!schaft)|gesch[aä]ftsf[uü]hr|gesellschafter|kanzleileit|niederlassungsleit|vorstand",
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
    return bool(BERUFSTRAEGER.search(" ".join(x for x in (p.funktion, p.titel) if x)))


def ist_leitung(p: Ansprechpartner | LlmPerson) -> bool:
    return bool(LEITUNG.search(p.funktion or ""))


def _namens_tokens(name: str) -> set[str]:
    name = _TITEL.sub(" ", name.lower())
    return {t for t in re.split(r"[^a-zäöüß]+", name) if len(t) >= 2}


def gleiche_person(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ta, tb = _namens_tokens(a), _namens_tokens(b)
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def ansprechpartner(llm: LlmErgebnis | None) -> list[Ansprechpartner]:
    """Berufsträger und Leitung; Verantwortliche/r laut Impressum zuerst, dann Leitung, dann Berufsträger."""
    if llm is None:
        return []
    personen: list[LlmPerson] = []
    for p in llm.personen:
        if (ist_berufstraeger(p) or ist_leitung(p)) and not any(gleiche_person(p.name, q.name) for q in personen):
            personen.append(p)
    verantwortlich = llm.verantwortliche_person
    if verantwortlich and not any(gleiche_person(verantwortlich, p.name) for p in personen):
        # im Impressum genannt, aber nicht in der Personenliste: als Leitung aufnehmen
        personen.insert(0, LlmPerson(name=verantwortlich, funktion="laut Impressum verantwortlich"))

    def rang(p: LlmPerson) -> int:
        if gleiche_person(p.name, verantwortlich):
            return 0
        if ist_leitung(p):
            return 1
        return 2

    return [Ansprechpartner(name=p.name, titel=p.titel, funktion=p.funktion) for p in sorted(personen, key=rang)]


def teamgroesse(
    llm: LlmErgebnis | None, team: Seite | None
) -> tuple[int | None, TeamKlasse | None, str | None]:
    """(Zahl, Klasse, Basis): genannte Zahl > gezählte Personen der Teamseite > Klasse aus dem Text."""
    if llm is None:
        return None, None, None
    if llm.mitarbeiterzahl:
        beleg = llm.mitarbeiterzahl_beleg
        return llm.mitarbeiterzahl, None, f"Text: „{beleg}“" if beleg else "Mitarbeiterzahl im Text genannt"
    if team is not None and team.text:
        teamtext = team.text.lower()
        gezaehlt = []
        for p in llm.personen:
            nachname = sorted(_namens_tokens(p.name), key=len)[-1:] if p.name else []
            if nachname and nachname[0] in teamtext and not any(gleiche_person(p.name, q) for q in gezaehlt):
                gezaehlt.append(p.name)
        # Gezählte Personen sind eine Untergrenze: nennt der Text eine größere Klasse
        # („über 20 Mitarbeitende", aber nur 2 Personen abgebildet), gilt die Klasse.
        klasse = llm.teamgroesse_klasse
        if len(gezaehlt) >= 2 and (klasse is None or _KLASSEN.index(TeamKlasse.aus_zahl(len(gezaehlt))) >= _KLASSEN.index(klasse)):
            return len(gezaehlt), None, f"{len(gezaehlt)} Personen auf Teamseite"
    if llm.teamgroesse_klasse:
        return None, llm.teamgroesse_klasse, "Einschätzung aus dem Text"
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
    rechtsform = (llm.rechtsform if llm else None) or rechtsform_aus_name(name)

    personen = ansprechpartner(llm)
    zahl, klasse, basis = teamgroesse(llm, seiten.get(Seitentyp.TEAM))

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

    pruefung = plz_pruefen(plz, ort) if plz_pruefen and plz else None
    k = Kanzlei(
        kandidat_id=kandidat.id,
        domain=kandidat.domain,
        lauf_bundesland=kandidat.bundesland,
        kanzleiname=name,
        rechtsform=rechtsform,
        strasse=strasse,
        plz=plz,
        ort=ort,
        bundesland=pruefung.bundesland if pruefung else None,
        plz_verifiziert=pruefung.verifiziert if pruefung else None,
        telefon=telefon,
        email=email,
        website=website,
        website_vorhanden=bool(kandidat.domain or kandidat.website),
        ansprechpartner=personen,
        berufstraeger_anzahl=sum(1 for p in personen if ist_berufstraeger(p)) if llm else None,
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
