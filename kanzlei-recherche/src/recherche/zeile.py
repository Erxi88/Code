"""`recherche zeile --domain X`: alles zu einem Kandidaten – Kandidat, Funde, Kanzlei-Zeile, Grund, Abweichungen,
Quell-URLs, Lauf und das Blatt, in dem die Zeile im Export des Landes steht (mit Begründung, wenn in keinem).

Anlass: kanzlei-pruefung -b SH meldete 8 Domains als „keine Kanzlei", die in keinem Blatt des SH-Exports standen
(04.11.2026)."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .extract.kanzlei import GRUND_PRAEFIX, grund_aus_abweichung
from .models import BUNDESLAENDER, Kandidat, Kanzlei, NichtGefunden, Status
from .storage import Storage


def domain_normal(eingabe: str) -> str:
    """„https://www.Kanzlei.de/impressum" → „kanzlei.de"."""
    e = eingabe.strip()
    host = urlsplit(e if "://" in e else f"//{e}").netloc or e
    return host.lower().split("@")[-1].split(":")[0].removeprefix("www.").rstrip(".")


@dataclass
class ZeilenBericht:
    domain: str
    kandidat: Kandidat | None = None
    kanzlei: Kanzlei | None = None
    funde: list = field(default_factory=list)
    nicht_gefunden: list[NichtGefunden] = field(default_factory=list)
    seiten: dict = field(default_factory=dict)  # Seitentyp → URL
    blatt: str | None = None
    blatt_grund: str | None = None


def export_blatt(k: Kanzlei, land: str | None, mit_kontrollblaettern: bool) -> tuple[str | None, str]:
    """(Blatt, Begründung) im Export `recherche export -b <Land>` – nach denselben Regeln wie export.py."""
    from .export import _land_status, _land_von
    from .qualitaet import zu_pruefen_grund

    if not land:
        return None, "Lauf-Bundesland unbekannt"
    kontroll = "" if mit_kontrollblaettern else " – nur mit --mit-kontrollblaettern"
    plz_land = _land_von(k)
    if plz_land != land:
        if k.status == Status.AUSSERHALB or k.lauf_bundesland and BUNDESLAENDER.get(k.lauf_bundesland.upper()) == land:
            wo = f"PLZ in {plz_land}" if plz_land else "keine (gültige) deutsche PLZ bzw. Ausland"
            if k.status == Status.AUSSERHALB:
                return (f"Außerhalb Bundesland{kontroll}" if mit_kontrollblaettern else None,
                        f"{wo}: nicht in der Lieferliste {land}; Blatt „Außerhalb Bundesland“ des Laufs{kontroll}")
            return None, (f"{wo}: nicht in der Lieferliste {land} (Status „{k.status.value}“ – „Keine Kanzlei“ zeigt "
                          f"nur Zeilen mit PLZ im Land); im Export {plz_land or 'keines Landes'} bzw. export --gesamt")
        return None, f"PLZ in {plz_land or 'keinem Land'}, Lauf {k.lauf_bundesland}: gehört nicht zu {land}"
    k = _land_status(k, land)
    if k.status == Status.KEINE_KANZLEI:
        return ("Keine Kanzlei" if mit_kontrollblaettern else None), f"Status „keine Kanzlei“{kontroll}"
    if k.status == Status.AUSSERHALB:
        return ("Außerhalb Bundesland" if mit_kontrollblaettern else None), f"Status „außerhalb Bundesland“{kontroll}"
    if grund := zu_pruefen_grund(k):
        return "Zu prüfen", grund
    return "Kanzleien", "PLZ im Land, keine Sperre (weitere Domains derselben Kanzlei stehen nur als Zentrale)"


def bericht(storage: Storage, eingabe: str, land: str | None = None, mit_kontrollblaettern: bool = False) -> ZeilenBericht:
    d = domain_normal(eingabe)
    b = ZeilenBericht(d)
    b.kandidat = storage.kandidat_nach_domain(d) or storage.kandidat_nach_domain(f"www.{d}")
    b.kanzlei = storage.get_kanzlei(d) or storage.get_kanzlei(f"www.{d}") or (
        storage.kanzlei_fuer_kandidat(b.kandidat.id) if b.kandidat and b.kandidat.id is not None else None)
    if b.kandidat and b.kandidat.id is not None:
        b.funde = storage.funde_kandidat(b.kandidat.id)
        b.seiten = {t.value: (s.final_url or s.url) for t, s in storage.kandidat_seiten(b.kandidat.id).items()}
    b.nicht_gefunden = [n for lauf in storage.laeufe() for n in storage.nicht_gefunden(lauf)
                        if n.domain.lower().removeprefix("www.") == d]
    if b.kanzlei:
        ziel = land or BUNDESLAENDER.get((b.kanzlei.lauf_bundesland or "").upper())
        b.blatt, b.blatt_grund = export_blatt(b.kanzlei, ziel, mit_kontrollblaettern)
    return b


def zeilen(b: ZeilenBericht) -> list[str]:
    """Textausgabe (eine Angabe je Zeile)."""
    out = [f"Domain: {b.domain}"]
    if b.kandidat is None and b.kanzlei is None:
        return out + ["Kein Kandidat und keine Kanzlei-Zeile mit dieser Domain in der Datenbank."]
    if k := b.kandidat:
        out += [f"Kandidat #{k.id} · Lauf {k.bundesland} · Quelle {k.quelle or '–'} · Suchort {k.suchort or '–'}",
                f"  Website {k.website or '–'} · crawl {k.crawl_status.value} · extract {k.extract_status.value}"
                + (f" · Fehler: {k.fehler}" if k.fehler else ""),
                f"  Discovery: {k.name or '–'} · {k.strasse or ''} {k.plz or ''} {k.ort or ''} · {k.telefon or '–'}"]
    for f in b.funde:
        out.append(f"  Fund {f.quelle} ({f.suchort or '–'}): {f.name or '–'} – {(f.snippet or '')[:100]}")
    for typ, url in b.seiten.items():
        out.append(f"  Seite {typ}: {url}")
    for n in b.nicht_gefunden:
        out.append(f"  Nicht gefunden ({n.bundesland}): {n.grund}")
    if (z := b.kanzlei) is None:
        return out + ["Keine Kanzlei-Zeile (nicht extrahiert)."]
    grund = grund_aus_abweichung(z.abweichung)
    out += [f"Status: {z.status.value} · Lauf {z.lauf_bundesland} · Bundesland laut PLZ {z.bundesland or '–'}"
            f" (verifiziert: {z.plz_verifiziert})",
            f"Kanzleiname: {z.kanzleiname or '–'} · Rechtsform {z.rechtsform or '–'} · Konfidenz {z.konfidenz.value}",
            f"Adresse: {z.strasse or '–'}{f' ({z.adresszusatz})' if z.adresszusatz else ''}, {z.plz or '–'} "
            f"{z.ort or ''} · Telefon {z.telefon or '–'} · E-Mail {z.email or '–'}",
            "Ansprechpartner: " + ("; ".join(f"{p.name} ({p.funktion or '–'})" for p in z.ansprechpartner) or "–"),
            f"Berufsträger {z.berufstraeger_anzahl if z.berufstraeger_anzahl is not None else '–'} · Teamgröße "
            f"{z.teamgroesse_anzeige or '–'}",
            f"Grund „keine Kanzlei“: {grund or '–'}",
            "Abweichungen:"]
    teile = [t.strip() for t in (z.abweichung or "").split("; ") if t.strip() and not t.startswith(GRUND_PRAEFIX)]
    out += [f"  - {t}" for t in teile] or ["  –"]
    out += ["Quell-URLs:"] + ([f"  {u}" for u in z.quell_urls] or ["  –"])
    out.append(f"Abrufdatum: {z.abrufdatum or '–'}")
    out.append(f"Export-Blatt: {b.blatt or 'keines'} – {b.blatt_grund}")
    return out
