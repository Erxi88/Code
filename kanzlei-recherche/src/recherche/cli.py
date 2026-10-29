"""Kommandozeile: recherche search | fill | coverage | crawl | extract | export | run |
search-compare | orte | fixture-add | fixture-eval."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import Settings, load_settings
from .coverage import CoverageErgebnis, als_excel, berechne, load_referenz
from .crawl import DNS_GRUND, ausserhalb_laut_suchtreffer, run_crawl
from .export import export_excel
from .extract.base import AnbieterNichtErreichbar, ExtraktionFehler
from .extract.lauf import run_extract
from .extract.ollama import OllamaExtractor
from .extract.rules import Ausschluss
from .fixtures import (
    FELDER,
    KERNFELDER,
    EvalErgebnis,
    aufgezeichnete_modelle,
    auswerten,
    fixture_finden,
    fixture_holen_sync,
    fixtures_finden,
    prompt_diagnose,
)
from .models import LAND_SCHLUESSEL, LaufEintrag, SchrittStatus
from .normalize import PlzPruefer
from .orte import (
    OpenPlzOrte,
    OrteFehler,
    gemeinden_aus_localities,
    gv_quelle_laden,
    lade_gv,
    orte_erzeugen,
    stadtteile_nachsuchen,
    stufen_verteilung,
    vergleichen,
)
from .search.base import OrtsAdapter, QueryAdapter, SearchAdapter
from .search.compare import VergleichsErgebnis, compare_adapters
from .search.pipeline import (
    Ort,
    Suchplan,
    SuchStatistik,
    load_blocklist,
    load_orte,
    orte_filtern,
    run_search,
    schaetze_anfragen,
    schreibe_orte,
)
from .search.registry import aktive_adapter, build_adapter
from .storage import Storage

app = typer.Typer(
    name="recherche",
    help="Recherche von Steuerberaterkanzleien: Discovery, Crawling, Extraktion, Excel-Export.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

BundeslandOpt = Annotated[
    str | None, typer.Option("--bundesland", "-b", help="Kürzel, z. B. SH (Standard aus settings.yaml)")
]
DbOpt = Annotated[Path | None, typer.Option("--db", help="SQLite-Datei (Standard aus settings.yaml)")]
ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Pfad zu settings.yaml")]
OrteOpt = Annotated[
    str | None, typer.Option("--orte", help="Kommagetrennte Orte, z. B. Kiel,Husum (Standard: alle)")
]


class Kontext:
    def __init__(self, config: Path | None, bundesland: str | None, db: Path | None):
        self.settings: Settings = load_settings(config)
        self.bundesland = (bundesland or self.settings.standard_bundesland).upper()
        self.bl_cfg = self.settings.bundesland(self.bundesland)
        self.db = db or self.settings.db_pfad()

    def storage(self) -> Storage:
        return Storage(self.db)

    def orte(self, auswahl: str | None, mit_zusatz: bool = True) -> list[Ort]:
        orte = load_orte(self.settings.pfad(self.bl_cfg.orte_datei), mit_zusatz=mit_zusatz)
        return orte_filtern(orte, auswahl.split(",") if auswahl else None)

    def vorlagen(self) -> Suchplan:
        """Suchplan: Query-Vorlagen und Seiten je Einwohnerklasse (discovery.stufe_a)."""
        return Suchplan.aus_settings(self.settings)

    def blocklist(self) -> set[str]:
        return load_blocklist(self.settings.pfad(self.settings.pfade.blocklist))


def _lauf_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _kontext(config, bundesland, db) -> Kontext:
    try:
        return Kontext(config, bundesland, db)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Fehler:[/red] {e}")
        raise typer.Exit(2) from e


def _fehler(text: str) -> None:
    console.print(f"[red]{text}[/red]")
    raise typer.Exit(2)


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------- Schritte


def _adapter_pruefen(adapter: SearchAdapter, explizit: bool) -> None:
    if not adapter.aktiv and not explizit:
        _fehler(f"Quelle {adapter.name} ist in settings.yaml deaktiviert (discovery.quellen.{adapter.name}.aktiv)")
    if grund := adapter.nicht_konfiguriert_grund():
        _fehler(f"Quelle {adapter.name} nicht einsatzbereit: {grund}")


def _budget_hinweis(ctx: Kontext, st: Storage, adapter: QueryAdapter, orte: list[Ort], ja: bool) -> bool:
    """Schätzung vor dem Start gegen das Guthaben aus settings.yaml. False = Abbruch durch den Nutzer."""
    max_results = ctx.settings.discovery.stufe_a.max_results_per_query
    queries, anfragen = schaetze_anfragen(st, adapter, orte, ctx.vorlagen(), ctx.bundesland, max_results)
    kosten = adapter.kosten(anfragen)
    guthaben = adapter.cfg.guthaben
    console.print(f"Budget: {queries} offene Queries, höchstens etwa {anfragen} Anfragen ≈ {kosten:.2f} "
                  f"(kosten_je_1000 {adapter.cfg.kosten_je_1000}; dazu ggf. Zusatzseiten und Stadtteil-Nachsuche); "
                  f"Guthaben laut settings: {'nicht hinterlegt' if guthaben is None else f'{guthaben:.2f}'}"
                  f"; Budget je Lauf: {adapter.max_anfragen or 'unbegrenzt'} Anfragen")
    if guthaben is not None and kosten > guthaben:
        console.print(f"[yellow]Die Schätzung ({kosten:.2f}) liegt über dem Guthaben ({guthaben:.2f}) – der Lauf endet "
                      "voraussichtlich mit „Guthaben erschöpft“ und lässt sich nach dem Aufladen fortsetzen.[/yellow]")
        if not ja and not typer.confirm("Trotzdem starten?", default=False):
            return False
    return True


def _lauf(ctx: Kontext, st: Storage, adapter: SearchAdapter, orte: list[Ort], lauf_id: str,
          ja: bool = False) -> SuchStatistik | None:
    stufe = "Websuche (Stufe A)" if isinstance(adapter, QueryAdapter) else "Lückenfüller (Stufe B)"
    console.print(f"{stufe} mit [bold]{adapter.name}[/bold] in {len(orte)} Orten ({ctx.bundesland}) …")
    if isinstance(adapter, QueryAdapter) and not _budget_hinweis(ctx, st, adapter, orte, ja):
        console.print("Abgebrochen – nichts gesucht.")
        return None
    if hasattr(adapter, "melden"):
        adapter.melden = lambda m: console.print(f"  [yellow]{m}[/yellow]")
    s = ctx.settings
    nachsuchen = None
    if isinstance(adapter, QueryAdapter) and s.orte.stadtteile.aktiv:
        openplz = OpenPlzOrte(s.openplz.url, cache=st, page_size=s.openplz.page_size, pause_s=s.openplz.pause_s)
        nachsuchen = stadtteile_nachsuchen(openplz, LAND_SCHLUESSEL[ctx.bundesland], s.orte.stadtteile)
    stat = run_search(
        st, adapter, orte, ctx.vorlagen(), ctx.blocklist(), ctx.bundesland, lauf_id,
        s.discovery.stufe_a.max_results_per_query,
        fortschritt=lambda m: console.print(f"  {m}"), stadtteile=nachsuchen,
    )  # fmt: skip
    console.print(
        f"Fertig: {stat.queries} Abfragen, {stat.uebersprungen} schon erledigt, {stat.fehler} Fehler, "
        f"{stat.treffer} Treffer, {stat.neue_kandidaten} neue Kandidaten"
        + (f", {stat.zusatzseiten} Zusatzseiten wegen voller Kanzlei-Seiten" if stat.zusatzseiten else "")
        + (f", {stat.stadtteile} Stadtteil-Suchorte nachgesucht" if stat.stadtteile else "") + "."
    )
    if isinstance(adapter, OrtsAdapter):
        console.print(f"Ausbeute {adapter.name}: {stat.treffer} Treffer, davon {stat.neue_kandidaten} neu, "
                      f"{stat.bekannt} bereits bekannt (Telefon/Adresse/Domain), {stat.verworfen} verworfen "
                      "(Blocklist oder schon von dieser Quelle)")
    console.print(
        f"Kostenlog {adapter.name}: {stat.anfragen} Anfragen ≈ {stat.kosten:.2f} "
        f"(kosten_je_1000: {adapter.cfg.kosten_je_1000}, Budget je Lauf: {adapter.max_anfragen or 'unbegrenzt'})"
    )
    if stat.abbruch:
        console.print(f"[yellow]Lauf vorzeitig beendet:[/yellow] {stat.abbruch}")
        console.print("[yellow]Später denselben Befehl erneut starten – erledigte Abfragen werden übersprungen.[/yellow]")
    elif stat.fehler:
        console.print("[yellow]Fehlgeschlagene Abfragen werden beim nächsten Aufruf wiederholt.[/yellow]")
    return stat


def _search(ctx: Kontext, orte: str | None, adapter_name: str | None, lauf_id: str, ja: bool = False) -> bool:
    """False, wenn der Start nach dem Budget-Hinweis abgelehnt wurde."""
    with ctx.storage() as st:
        name = adapter_name or ctx.settings.discovery.stufe_a.adapter
        adapter = build_adapter(name, ctx.settings, cache=st)
        if not isinstance(adapter, QueryAdapter):
            _fehler(f"{name} ist ein Lückenfüller (Stufe B) – bitte `recherche fill --quelle {name}` verwenden")
        _adapter_pruefen(adapter, explizit=adapter_name is not None)
        return _lauf(ctx, st, adapter, ctx.orte(orte), lauf_id, ja) is not None


def _crawl(ctx: Kontext, limit: int | None = None, wiederholen: bool = False) -> None:
    with ctx.storage() as st:
        if wiederholen:
            n = st.fehler_zuruecksetzen(ctx.bundesland, "crawl", ausser=DNS_GRUND)
            console.print(f"{n} fehlgeschlagene Kandidaten wieder offen (tote Domains – {DNS_GRUND} – bleiben übersprungen).")
        offen = len(st.kandidaten(ctx.bundesland, crawl_status=SchrittStatus.OFFEN, nur_mit_website=True))
        if not offen:
            console.print("crawl: keine offenen Kandidaten mit Website.")
            return
        cfg = ctx.settings.crawl
        console.print(
            f"Crawl: {min(offen, limit or offen)} von {offen} offenen Kandidaten, {cfg.parallel_domains} Domains "
            f"parallel, {cfg.requests_per_second_per_domain:g} Anfrage/s je Domain"
            f"{', Playwright-Rückfall an' if cfg.playwright else ''} …"
        )
        start = time.monotonic()
        pruefer = PlzPruefer(ctx.settings.openplz.url, cache=st)
        ziel = ctx.bl_cfg.name

        def land_filter(k, funde):
            return ausserhalb_laut_suchtreffer(k, funde, lambda plz: pruefer.pruefe(plz, None).bundesland, ziel)

        stat = run_crawl(st, cfg, ctx.bundesland, ctx.blocklist(), limit=limit,
                         fortschritt=lambda m: console.print(f"  {m}"), gruendlich=wiederholen,
                         land_filter=land_filter)  # fmt: skip
        st.log_lauf(
            LaufEintrag(lauf_id=_lauf_id(), schritt="crawl", bundesland=ctx.bundesland,
                        suchbegriff=f"{stat.fertig} gelesen, {stat.nicht_gefunden} nicht gefunden",
                        trefferzahl=stat.fertig, anfragen=stat.anfragen,
                        laufzeit_s=round(time.monotonic() - start, 2))
        )  # fmt: skip
    console.print(f"Fertig: {stat.fertig} Websites gelesen, {stat.nicht_gefunden} nicht gefunden, {stat.anfragen} Anfragen.")
    if stat.trotz_robots:
        console.print(f"  davon {stat.trotz_robots} gelesen, obwohl die robots.txt nicht abrufbar war (Hinweis in „Abweichung\")")
    if stat.dns:
        console.print(f"  {stat.dns} tote Domains ({DNS_GRUND}) – werden bei --wiederholen übersprungen")
    if stat.ausserhalb:
        console.print(f"  {stat.ausserhalb} nicht gecrawlt: Suchtreffer nur mit PLZ außerhalb {ctx.bl_cfg.name}")
    for grund, n in sorted(stat.gruende.items(), key=lambda x: -x[1]):
        console.print(f"  {n:4d} × {grund}")


def _extractor(ctx: Kontext, modell: str | None) -> OllamaExtractor:
    return OllamaExtractor(ctx.settings.llm, modell=modell)


def _sekunden_je_kanzlei(st: Storage, bundesland: str, ohne_llm: bool) -> float:
    """Mittlere Laufzeit je Kanzlei aus früheren Extraktionen (Lauf-Protokoll); sonst Erfahrungswert."""
    laeufe = [e for e in st.lauf_protokoll(bundesland) if e.schritt == "extract" and e.trefferzahl
              and (e.adapter == "nur Regeln") == ohne_llm and e.laufzeit_s]
    if laeufe:
        return sum(e.laufzeit_s for e in laeufe) / sum(e.trefferzahl for e in laeufe)
    return 0.2 if ohne_llm else 15.0


def _dauer(sekunden: float) -> str:
    h, m = divmod(round(sekunden / 60), 60)
    return f"{h} h {m} min" if h else f"{m} min"


def _extract(
    ctx: Kontext, ohne_llm: bool = False, modell: str | None = None, limit: int | None = None,
    wiederholen: bool = False, alle: bool = False, ja: bool = False,
) -> None:
    s = ctx.settings
    try:
        import phonenumbers  # noqa: F401
    except ImportError:
        console.print("[yellow]Paket phonenumbers fehlt – Telefonnummern ohne Trennzeichen bleiben ungetrennt "
                      "(pip install -e .)[/yellow]")
    with ctx.storage() as st:
        if alle:
            n = sum(1 for k in st.kandidaten(ctx.bundesland)
                    if k.crawl_status == SchrittStatus.FERTIG or not (k.domain or k.website))
            je = _sekunden_je_kanzlei(st, ctx.bundesland, ohne_llm)
            console.print(f"Neu-Extraktion aller Zeilen in {ctx.bundesland}: {n} Kandidaten, geschätzt "
                          f"{_dauer(n * je)} ({je:.1f} s je Kanzlei{' laut Lauf-Protokoll' if st.lauf_protokoll(ctx.bundesland) else ''}). "
                          "Gepflegte Status bleiben; abbrechen und erneut aufrufen setzt fort.")
            if not ja and not typer.confirm("Alle Kanzleien neu extrahieren?", default=False):
                console.print("Abgebrochen – nichts geändert.")
                return
            console.print(f"{st.extract_alle_offen(ctx.bundesland)} Kandidaten wieder offen.")
        if wiederholen:
            console.print(f"{st.fehler_zuruecksetzen(ctx.bundesland, 'extract')} fehlgeschlagene Kandidaten wieder offen.")
        extractor = None if ohne_llm else _extractor(ctx, modell)
        pruefer = PlzPruefer(s.openplz.url, cache=st)
        ausschluss = Ausschluss.laden(s.pfad(s.pfade.impressum_ausschluss))
        console.print(f"Extraktion mit {'nur Regeln' if ohne_llm else 'Regeln + ' + extractor.modell} …")
        stat = run_extract(st, ctx.bundesland, extractor, pruefer, ausschluss, _lauf_id(), limit=limit,
                           fortschritt=lambda m: console.print(f"  {m}"),
                           kanzlei_rechtsformen=s.extract.kanzlei_rechtsformen if s.extract.kanzlei_pruefung else None,
                           ziel_land=ctx.bl_cfg.name)  # fmt: skip
    console.print(
        f"Fertig: {stat.fertig} Kanzleien ({stat.ohne_website} ohne Website), {stat.fehler} Fehler, "
        f"{stat.llm_aufrufe} LLM-Aufrufe, {pruefer.anfragen} OpenPLZ-Anfragen."
    )
    if stat.llm_aufrufe:
        console.print(f"  im Mittel {stat.mittlere_laufzeit_s:.1f} s und {stat.tokens / stat.llm_aufrufe:.0f} Token je Kanzlei")
    if hinweis := stat.hinweis():
        console.print(f"[yellow]  Hinweis: {hinweis} – Details mit recherche fixture-prompt bzw. -v[/yellow]")
    if stat.abbruch:
        console.print(f"[yellow]Lauf vorzeitig beendet:[/yellow] {stat.abbruch}")
        console.print("[yellow]Ollama starten (bzw. Modell laden) und denselben Befehl erneut aufrufen.[/yellow]")
    if stat.fehler:
        console.print("[yellow]Fehlgeschlagene Kandidaten erneut versuchen: `recherche extract --wiederholen`[/yellow]")


def _export(ctx: Kontext, out: Path | None) -> Path:
    pfad = out or ctx.settings.ausgabe_pfad(ctx.bundesland)
    with ctx.storage() as st:
        pfad = export_excel(st, ctx.bundesland, pfad)
        n = len(st.kanzleien(ctx.bundesland))
    console.print(f"Excel geschrieben: [bold]{pfad}[/bold] ({n} Kanzleien)")
    return pfad


def _coverage(ctx: Kontext, st: Storage) -> CoverageErgebnis:
    referenz = load_referenz(ctx.settings.pfad(ctx.settings.pfade.referenz), ctx.bundesland)
    return berechne(st, ctx.bundesland, ctx.orte(None), referenz, ziel_land=ctx.bl_cfg.name)


# ---------------------------------------------------------------------- Befehle


@app.command()
def search(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    orte: OrteOpt = None,
    adapter: Annotated[str | None, typer.Option(help="Websuche-Adapter statt discovery.stufe_a.adapter")] = None,
    ja: JaOpt = False,
) -> None:
    """Stufe A: Websuche je Ort mit allen Query-Vorlagen (wiederaufnehmbar)."""
    _search(_kontext(config, bundesland, db), orte, adapter, _lauf_id(), ja)


@app.command()
def fill(
    quelle: Annotated[str, typer.Option("--quelle", "-q", help="Stufe-B-Quelle, z. B. places")],
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    orte: OrteOpt = None,
    luecken: Annotated[bool, typer.Option("--luecken", help="Orte unter der Referenz (aus coverage)")] = False,
    alle: Annotated[bool, typer.Option("--alle", help="ausdrücklich alle Orte des Bundeslandes")] = False,
) -> None:
    """Stufe B: Lückenfüller für ausgewählte Orte – nie flächendeckend ohne --alle."""
    if sum(map(bool, (orte, luecken, alle))) != 1:
        _fehler("Genau eine Auswahl angeben: --orte …, --luecken oder --alle")
    ctx = _kontext(config, bundesland, db)
    with ctx.storage() as st:
        adapter = build_adapter(quelle, ctx.settings, cache=st)
        if not isinstance(adapter, OrtsAdapter):
            _fehler(f"{quelle} ist eine Websuche (Stufe A) – bitte `recherche search --adapter {quelle}` verwenden")
        _adapter_pruefen(adapter, explizit=False)
        if luecken:
            erg = _coverage(ctx, st)
            namen = erg.luecken_orte(ctx.settings.discovery.coverage.quote_ziel)
            if not namen:
                _fehler("Keine Orte unter der Referenz – Referenzzahlen in referenz_kammern.yaml hinterlegt?")
            auswahl = orte_filtern(ctx.orte(None, mit_zusatz=False), namen)
        else:
            auswahl = ctx.orte(orte if orte else None, mit_zusatz=False)
        _lauf(ctx, st, adapter, auswahl, _lauf_id())


@app.command()
def coverage(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Ergebnis als .xlsx")] = None,
) -> None:
    """Gefundene Kanzleien je Kreis, Ort und Quelle gegen die Referenzzahlen."""
    ctx = _kontext(config, bundesland, db)
    with ctx.storage() as st:
        try:
            erg = _coverage(ctx, st)
        except ValueError as e:
            _fehler(str(e))
    zeige_coverage(erg, ctx.settings.discovery.coverage.top_orte, ctx.settings.discovery.coverage.quote_ziel)
    if out:
        console.print(f"Excel geschrieben: {als_excel(erg, out)}")


def _zahl(wert: float | None, stellen: int = 0) -> str:
    return "–" if wert is None else f"{wert:.{stellen}f}"


def zeige_coverage(erg: CoverageErgebnis, top: int, quote_ziel: float) -> None:
    ref = erg.referenz
    if ref is None or erg.erwartet is None:
        console.print("[yellow]Keine (vollständigen) Referenzzahlen hinterlegt – nur Fundzahlen.[/yellow]")
    else:
        console.print(f"Referenz: {ref.quelle or 'ohne Quellenangabe'}, Stand {ref.stand or '–'}, Einheit {ref.einheit}")
    console.print(
        f"[bold]{erg.bundesland}[/bold]: {erg.gefunden} Kanzleien gefunden, erwartet {_zahl(erg.erwartet)}; "
        f"nur in einer Quelle: {erg.nur_eine_quelle} ({erg.anteil_nur_eine_quelle:.0%})"
    )
    if erg.modus == "kanzleien":
        console.print(f"  gezählt: nur Kanzleien im Land (Kreis aus der extrahierten PLZ); nicht gezählt: "
                      f"{erg.keine_kanzlei} keine Kanzlei, {erg.ausserhalb} außerhalb, {erg.ohne_bundesland} ohne "
                      f"geprüfte PLZ; offen (nicht gelesen): {erg.offen}")
    else:
        console.print("  gezählt: Kandidaten (noch keine Extraktion) – nach extract zählen nur echte Kanzleien im Land")

    t = Table(title="Je Kreis (größte Lücke zuerst)")
    for col in ("Kreis", "Gefunden", "Erwartet", "Basis", "Quote", "Lücke"):
        t.add_column(col, justify="left" if col in ("Kreis", "Basis") else "right")
    for k in sorted(erg.kreise, key=lambda k: (k.luecke is None, -(k.luecke or 0), k.kreis)):
        quote = "–" if k.quote is None else f"{k.quote:.0%}"
        t.add_row(k.kreis, str(k.gefunden), _zahl(k.erwartet), k.basis or "", quote, _zahl(k.luecke))
    console.print(t)

    q = Table(title="Je Quelle")
    for col in ("Quelle", "Kanzleien", "nur diese Quelle", "Anteil"):
        q.add_column(col, justify="left" if col == "Quelle" else "right")
    for z in erg.quellen:
        q.add_row(z.quelle, str(z.kanzleien), str(z.nur_diese), f"{z.anteil_nur_diese:.0%}")
    console.print(q)

    orte = sorted((o for o in erg.orte if o.luecke is not None), key=lambda o: -(o.luecke or 0))[:top]
    if orte:
        o_t = Table(title=f"Orte mit der größten Lücke (Top {top})")
        for col in ("Ort", "Kreis", "Gefunden", "Erwartet", "Lücke"):
            o_t.add_column(col, justify="left" if col in ("Ort", "Kreis") else "right")
        for o in orte:
            o_t.add_row(o.ort, o.kreis or "", str(o.gefunden), _zahl(o.erwartet, 1), _zahl(o.luecke, 1))
        console.print(o_t)
    luecken = erg.luecken_orte(quote_ziel)
    if luecken:
        console.print(f"Orte für `fill --luecken` ({len(luecken)}): {', '.join(luecken[:top])}{' …' if len(luecken) > top else ''}")


LimitOpt = Annotated[int | None, typer.Option("--limit", help="höchstens so viele Kandidaten (Test)")]
JaOpt = Annotated[bool, typer.Option("--ja", "-y", help="ohne Rückfrage starten (auch wenn die Schätzung über dem Guthaben liegt)")]
WiederholenOpt = Annotated[bool, typer.Option("--wiederholen", help="fehlgeschlagene Kandidaten erneut versuchen")]


@app.command()
def crawl(
    bundesland: BundeslandOpt = None, db: DbOpt = None, config: ConfigOpt = None, limit: LimitOpt = None,
    wiederholen: WiederholenOpt = False,
) -> None:
    """Kandidaten-Websites laden: Startseite, Impressum, Kontakt, Team (wiederaufnehmbar)."""
    _crawl(_kontext(config, bundesland, db), limit, wiederholen)


@app.command()
def extract(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    ohne_llm: Annotated[bool, typer.Option("--ohne-llm", help="nur Regeln, kein Ollama")] = False,
    modell: Annotated[str | None, typer.Option(help="Ollama-Modell statt llm.modell")] = None,
    limit: LimitOpt = None,
    wiederholen: WiederholenOpt = False,
    alle: Annotated[bool, typer.Option("--alle", help="alle Kanzleien neu extrahieren (z. B. nach Regeländerungen)")] = False,
    ja: Annotated[bool, typer.Option("--ja", "-y", help="ohne Rückfrage (mit --alle)")] = False,
) -> None:
    """Daten aus den gecachten Seiten extrahieren (Regeln + LLM) und mit der Discovery zusammenführen.

    --alle extrahiert auch bereits fertige Kandidaten neu (Rückfrage mit Zeitschätzung); fortsetzbar."""
    _extract(_kontext(config, bundesland, db), ohne_llm, modell, limit, wiederholen, alle, ja)


@app.command("fixture-add")
def fixture_add(
    domain: Annotated[str, typer.Option("--domain", help="Domain der Kanzlei, z. B. kanzlei-beispiel.de")],
    config: ConfigOpt = None,
    ziel: Annotated[Path | None, typer.Option(help="Zielordner (Standard: pfade.fixtures_echt)")] = None,
) -> None:
    """Start-, Impressum-, Kontakt- und Teamseite einer Domain als Fixture ablegen (nur lokal)."""
    s = load_settings(config)
    ordner = ziel or s.pfad(s.pfade.fixtures_echt)
    pfad, grund = fixture_holen_sync(domain.strip().lower(), s.crawl, ordner)
    seiten = sorted(p.name for p in pfad.glob("*.html"))
    console.print(f"Fixture abgelegt: [bold]{pfad}[/bold] ({', '.join(seiten) or 'keine Seiten'})")
    if grund:
        console.print(f"[yellow]Hinweis:[/yellow] {grund}")
    meta_pfad = pfad / "meta.json"
    meta = json.loads(meta_pfad.read_text(encoding="utf-8-sig")) if meta_pfad.exists() else {}
    for typ, hinweis in (meta.get("playwright") or {}).items():
        farbe = "green" if hinweis.startswith("gerendert (") else "yellow"
        console.print(f"[{farbe}]Playwright {typ}: {hinweis}[/{farbe}]")
    console.print("Jetzt erwartet.json von Hand ausfüllen und _status auf 'fertig' setzen.")


@app.command("blocklist-vorschlag")
def blocklist_vorschlag(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    mindestens: Annotated[int, typer.Option(help="nur Domains mit mindestens so vielen Suchorten")] = 1,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Liste als CSV (Semikolon)")] = None,
    anzahl: Annotated[int, typer.Option(help="höchstens so viele Zeilen anzeigen")] = 50,
) -> None:
    """Domains aus „Nicht gefunden" (robots.txt/HTTP 403) und mit Status „keine Kanzlei" nach Suchorten –
    Kandidaten für die Blocklist."""
    from .blocklist_vorschlag import PORTAL_AB_ORTEN, als_csv, vorschlaege

    ctx = _kontext(config, bundesland, db)
    with ctx.storage() as st:
        liste = [v for v in vorschlaege(st, ctx.bundesland, ctx.blocklist()) if len(v.suchorte) >= mindestens]
    if not liste:
        console.print("Keine Domains mit Grund robots.txt, HTTP 403 oder „keine Kanzlei\".")
        return
    t = Table(title=f"Blocklist-Vorschläge ({len(liste)} Domains, sortiert nach Suchorten)")
    for col in ("Domain", "Suchorte", "Treffer", "Grund", "Name / Snippet"):
        t.add_column(col, justify="right" if col in ("Suchorte", "Treffer") else "left")
    for v in liste[:anzahl]:
        text = " – ".join(x for x in (v.name, (v.snippet or "")[:80]) if x)
        domain = v.domain + (f" (+{len(v.domains) - 1} Subdomains)" if len(v.domains) > 1 else "")
        orte = f"[bold]{len(v.suchorte)}[/bold] Portal" if v.portal else str(len(v.suchorte))
        t.add_row(domain, orte, str(v.treffer), v.grund_kurz, text)
    console.print(t)
    console.print(f"Prüfen und übernehmen: config/domain_blocklist.txt (eine Domain je Zeile). "
                  f"Ab {PORTAL_AB_ORTEN} Suchorten ist es fast sicher ein Portal.")
    if out:
        console.print(f"CSV geschrieben: {als_csv(liste, out)}")


@app.command("robots-check")
def robots_check_cmd(
    domain: Annotated[str, typer.Option("--domain", "-d", help="Domain, z. B. obh-partner.de")],
    config: ConfigOpt = None,
    rendern: Annotated[bool, typer.Option("--rendern", help="Startseite zusätzlich mit Playwright rendern")] = False,
) -> None:
    """Diagnose einer Domain: robots.txt aller Varianten roh (Status, Fehler, Weiterleitungen), Gruppe und
    entscheidende Regel für die Startseite, dann die Startseite selbst. Live, ohne Cache."""
    import asyncio

    from .crawl import robots_check

    s = load_settings(config)
    for zeile in asyncio.run(robots_check(s.crawl, domain, rendern=rendern)):
        console.print(zeile, markup=False, highlight=False)


@app.command("kanzlei-pruefung")
def kanzlei_pruefung(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Kanzlei-Erkennung auf bereits extrahierte Kanzleien anwenden (ohne LLM, ohne Netz).

    Ohne Berufsträger, Kanzlei-Rechtsform, Kanzleiwort im Namen und Steuerberaterkammer im
    Impressum → Status „keine Kanzlei" (eigenes Excel-Blatt), Konfidenz höchstens „mittel".
    Von Hand gepflegte Status bleiben."""
    from .extract.kanzlei import run_pruefung

    ctx = _kontext(config, bundesland, db)
    with ctx.storage() as st:
        stat = run_pruefung(st, ctx.bundesland, tuple(ctx.settings.extract.kanzlei_rechtsformen),
                            fortschritt=lambda m: console.print(f"  {m}"), ziel_land=ctx.bl_cfg.name,
                            plz_pruefen=PlzPruefer(ctx.settings.openplz.url, cache=st).pruefe)
    console.print(f"Geprüft: {stat.geprueft} Kanzleien – {stat.keine_kanzlei} keine Kanzlei, {stat.ausserhalb} außerhalb "
                  f"{ctx.bl_cfg.name}, {stat.wieder_kanzlei} wieder Kanzlei, {stat.herabgestuft} von Konfidenz „hoch\" "
                  f"herabgestuft, {stat.dubletten} Dubletten-Verdacht (Telefon + Ansprechpartner).")
    if not ctx.settings.extract.kanzlei_pruefung:
        console.print("[yellow]Für neue Extraktionen extract.kanzlei_pruefung: true in settings.yaml setzen.[/yellow]")


@app.command("fixture-klassen-umstellen")
def fixture_klassen_umstellen(
    config: ConfigOpt = None,
    ordner: Annotated[Path | None, typer.Option(help="Fixture-Ordner (Standard: pfade.fixtures_echt)")] = None,
    probe: Annotated[bool, typer.Option("--probe", help="nur anzeigen, nichts schreiben")] = False,
) -> None:
    """Teamgröße-Klassen in erwartet.json auf die aktuelle Einteilung (extract.groessenklassen) umstellen.

    Mit Zahl → Klasse aus der Zahl; ohne Zahl → alte Klasse nur, wenn eindeutig umrechenbar, sonst leer
    (mit Vorschlag aus dem Seitentext zum Nachtragen von Hand). Sicherung: erwartet.json.bak."""
    from .fixtures import klassen_umstellen

    s = load_settings(config)
    ziel = ordner or s.pfad(s.pfade.fixtures_echt)
    liste = klassen_umstellen(ziel, schreiben=not probe)
    if not liste:
        console.print(f"Keine erwartet.json mit alter Größenklasse in {ziel}.")
        return
    t = Table(title=f"Größenklassen {'(Probe, nichts geschrieben)' if probe else 'umgestellt'} – {ziel}")
    for col in ("Fixture", "alt", "neu", "Hinweis", "Vorschlag aus Text"):
        t.add_column(col)
    for u in liste:
        t.add_row(u.domain, u.alt or "–", u.neu or "leer", u.hinweis, u.vorschlag or "")
    console.print(t)
    offen = [u for u in liste if u.neu is None]
    if offen:
        console.print(f"[yellow]{len(offen)} ohne eindeutige Klasse – teamgroesse_klasse in erwartet.json von Hand "
                      "setzen (Vorschlag prüfen) oder leer lassen.[/yellow]")


@app.command("fixture-eval")
def fixture_eval(
    config: ConfigOpt = None,
    modell: Annotated[str | None, typer.Option(help="Modell (Standard: llm.modell)")] = None,
    live: Annotated[bool, typer.Option("--live", help="Ollama fragen und Antworten aufzeichnen")] = False,
    nur: Annotated[str | None, typer.Option(help="nur 'beispiele' oder 'echt'")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="ohne OpenPLZ (Ort wird nicht vereinheitlicht)")] = False,
) -> None:
    """Trefferquote je Feld über die Kanzleiseiten-Fixtures, mit Laufzeit und Token je Kanzlei.

    --live zeichnet nur für echt/ auf; die fiktiven Beispiele nur mit ausdrücklichem --nur beispiele."""
    s = load_settings(config)
    ordner = {"beispiele": s.pfad(s.pfade.fixtures_beispiele), "echt": s.pfad(s.pfade.fixtures_echt)}
    if live and nur is None:
        nur = "echt"
        console.print("--live: nur echte Fixtures (fiktive Beispiele mit --nur beispiele)")
    fixtures = fixtures_finden([p for n, p in ordner.items() if nur in (None, n)])
    if not fixtures:
        _fehler("Keine Fixtures gefunden (recherche fixture-add --domain …)")
    ausschluss = Ausschluss.laden(s.pfad(s.pfade.impressum_ausschluss))
    modell = modell or s.llm.modell
    extractor = OllamaExtractor(s.llm, modell=modell) if live else None
    with Storage(s.db_pfad()) as st:
        pruefer = None if offline else PlzPruefer(s.openplz.url, cache=st).pruefe
        try:
            erg = auswerten(fixtures, ausschluss, modell, extractor, pruefer)
        except AnbieterNichtErreichbar as e:
            _fehler(str(e))
        zeige_eval(erg, len(fixtures))
        modelle = aufgezeichnete_modelle(fixtures)
        if len(modelle) > 1:
            zeige_modellvergleich([auswerten(fixtures, ausschluss, m, plz_pruefen=pruefer) for m in modelle])


@app.command("fixture-prompt")
def fixture_prompt(
    domain: Annotated[str, typer.Option("--domain", help="Fixture-Ordner, z. B. bd-partner.de")],
    config: ConfigOpt = None,
    modell: Annotated[str | None, typer.Option(help="Modell (Standard: llm.modell)")] = None,
    live: Annotated[bool, typer.Option("--live", help="Prompt an Ollama schicken und echte Token messen")] = False,
    speichern: Annotated[bool, typer.Option("--speichern", help="vollständigen Prompt unter data/ ablegen")] = False,
    namen: Annotated[bool, typer.Option("--namen", help="erkannte Personen namentlich ausgeben")] = False,
) -> None:
    """Was vom HTML beim Modell ankommt: Größen je Bereinigungsstufe, Kürzung, Token – ohne Inhalte
    (Personennamen nur mit --namen)."""
    s = load_settings(config)
    fx = fixture_finden(domain, [s.pfad(s.pfade.fixtures_echt), s.pfad(s.pfade.fixtures_beispiele)])
    if fx is None:
        _fehler(f"Fixture {domain} nicht gefunden (recherche fixture-add --domain {domain})")
    modell = modell or s.llm.modell
    d = prompt_diagnose(fx, s.llm, Ausschluss.laden(s.pfad(s.pfade.impressum_ausschluss)), modell)
    p = d.prompt
    console.print(f"[bold]{d.domain}[/bold] – Budget {p.budget} Zeichen (num_ctx {s.llm.num_ctx}, "
                  f"max_text_chars {s.llm.max_text_chars})")
    t = Table(title="Seiten: Zeichen je Bereinigungsstufe")
    for col in ("Seite", "Pfad", "HTML", "Text", "bereinigt", "im Prompt", "gekürzt", "Schlüssel", "Personen"):
        t.add_column(col, justify="left" if col in ("Seite", "Pfad") else "right")
    from urllib.parse import urlsplit

    for sp in p.seiten:
        t.add_row(sp.typ.value, urlsplit(sp.url).path or "/", f"{sp.html_zeichen / 1000:.0f} KB", str(sp.text_zeichen),
                  str(sp.bereinigt_zeichen), str(sp.im_prompt_zeichen), "ja" if sp.gekuerzt else "",
                  str(sp.schluesselstellen or ""), str(sp.personen or ""))  # fmt: skip
    t.add_row("rahmen", "(Navigation/Fußzeile)", "", "", str(p.rahmen_zeichen), str(p.rahmen_im_prompt),
              "ja" if p.rahmen_im_prompt < p.rahmen_zeichen else "", "", "")  # fmt: skip
    console.print(t)
    if d.links:
        lt = Table(title="Linkerkennung (Startseite, beste 3 je Typ)")
        for col in ("Typ", "Punkte", "Pfad", "Linktext", "geladen"):
            lt.add_column(col, justify="right" if col == "Punkte" else "left")
        for typ, liste in d.links.items():
            for punkte_, pfad, text, gewaehlt in liste:
                lt.add_row(typ.value, str(punkte_), pfad, text, "✓" if gewaehlt else "")
        console.print(lt)
    console.print("Erkannte Personen (Teamseiten): " + ", ".join(f"{k} {v}" for k, v in d.personen_je_art.items()))
    if d.versteckt:
        vt = Table(title="Teamseiten: Text außerhalb des sichtbaren Inhalts (N. = Anzahl Namen)")
        for col in ("Seite", "Text/HTML", "noscript", "Skripte", "JSON-LD", "N.Attr", "N.Skript", "N.noscr"):
            vt.add_column(col, justify="left" if col in ("Seite", "JSON-LD") else "right")
        for typ, q in d.versteckt.items():
            vt.add_row(typ.value, f"{q.anteil:.1%}", f"{q.noscript_zeichen} Z.", f"{q.skript_zeichen / 1000:.0f} KB",
                       ", ".join(q.json_ld_typen) or "–", str(q.attribut_namen), str(q.skript_namen),
                       str(q.noscript_namen))  # fmt: skip
        console.print(vt)
        for typ, hinweis in d.playwright.items():
            console.print(f"  Playwright beim Abruf ({typ}): {hinweis}")
        if any(q.anteil < s.crawl.playwright_team_min_anteil and q.html_zeichen >= s.crawl.playwright_team_min_html
               for q in d.versteckt.values()) and not d.playwright:  # fmt: skip
            console.print(f"  Teamseite mit Text unter {s.crawl.playwright_team_min_anteil:.0%} des HTML: der Crawler lädt sie "
                          f"mit Playwright nach{'' if s.crawl.playwright else ' – dafür crawl.playwright: true setzen'} "
                          f"(Fixture neu holen: fixture-add).")
    console.print(f"Impressum: {d.vertreter} Vertretungsberechtigte"
                  f"{' (' + ', '.join(d.vertreter_namen) + ')' if namen and d.vertreter_namen else ''}, "
                  f"Berufsbezeichnung: {d.berufsbezeichnung or '–'}")
    quelle = f"mit Aufzeichnung {d.aufzeichnung.modell}" if d.aufzeichnung else "nur Regeln, keine Aufzeichnung"
    herkunft = Counter(h for _, _, hs in d.berufstraeger for h in hs.split(", "))
    console.print(f"Berufsträger laut Zusammenführung ({quelle}): {len(d.berufstraeger)}"
                  + (" – Herkunft: " + ", ".join(f"{k} {v}" for k, v in herkunft.items()) if herkunft else ""))
    if namen:
        for name_, funktion, hs in d.berufstraeger:
            console.print(f"  {name_} | {funktion} | {hs}")
        console.print(f"Erster Ansprechpartner: {d.erster_ansprechpartner or '–'}")
        console.print("Impressum-Schlüsselwörter:" if d.vertretung_protokoll else "Impressum: kein Schlüsselwort "
                      "(vertretungsberechtigt, vertreten durch, Geschäftsführer, Inhaber, Partner, Gesellschafter, "
                      "Verantwortlich) gefunden")
        for zeile in d.vertretung_protokoll:
            console.print(f"  {zeile}")
        console.print(f"Alle Personen nach Zusammenführung ({len(d.personen)}): Name | Funktion | Herkunft | Berufsträger")
        for name_, funktion, hs, bt in d.personen:
            console.print(f"  {name_} | {funktion or '–'} | {hs} | {'ja' if bt else 'nein'}")
        console.print(f"Kanzleiname: LLM „{d.name_llm or '–'}“ → Ergebnis „{d.name_regel or '–'}“")
        console.print(f"Rechtsform: Regel {d.rechtsform_regel or '–'}, LLM {d.rechtsform_llm or '–'}")
        console.print(f"Telefon (Regel): {d.regel_telefon or '–'}; Kandidaten:")
        for seite, nummer, art in d.telefone:
            console.print(f"  {seite}: {nummer} ({art})")
        if d.telefon_html:
            console.print("Telefonnummern im rohen HTML (Impressum, Startseite):")
            for seite, nummer, stelle in d.telefon_html:
                console.print(f"  {seite}: {nummer} ({stelle})")
        console.print("Impressum-Kopf (ohne Navigation/Fremd-Abschnitte):")
        for zeile in d.impressum_kopf:
            console.print(f"  {zeile[:100]}")
        if d.umfeld:
            console.print("Teamseite – Zeilen ab dem Namen (⏎ = Zeilenumbruch):")
            for name_, zeilen in d.umfeld.items():
                console.print(f"  {name_}: {zeilen}")
        for zeile in d.nur_im_rahmen:
            console.print(f"  [yellow]nur in Navigation/Fußzeile, nicht gezählt:[/yellow] {zeile}")
    console.print(f"Mitarbeiterzahl (Regel): {d.mitarbeiter_beleg or '–'}")
    console.print("Regeln gefunden: " + ", ".join(f"{k} {'✓' if v else '✗'}" for k, v in d.regel_felder.items()))
    from .extract.prompt import ZEICHEN_JE_TOKEN

    geschaetzt = round(d.prompt_zeichen / ZEICHEN_JE_TOKEN)
    console.print(f"Prompt: {d.prompt_zeichen} Zeichen (System {d.system_zeichen} + Seiten {len(p.text)}), "
                  f"geschätzt {geschaetzt} Token bei {ZEICHEN_JE_TOKEN} Zeichen/Token"
                  f"{'; gekürzt: ' + p.kuerzung_text() if p.gekuerzt else ''}")
    a = d.aufzeichnung
    if a and a.prompt_tokens:
        if a.prompt_zeichen:
            console.print(f"Aufzeichnung {a.modell}: {a.prompt_tokens} Prompt-Token für {a.prompt_zeichen} Zeichen "
                          f"→ {a.zeichen_je_token:.2f} Zeichen/Token")
        else:
            console.print(f"Aufzeichnung {a.modell}: {a.prompt_tokens} Prompt-Token (alter Prompt, Zeichen nicht "
                          f"aufgezeichnet) – mit --live neu messen")
    if a:
        belegt, alle = a.belegte_felder
        console.print(f"  Antwort: {belegt}/{alle} Felder belegt, {len(a.ergebnis.personen)} Personen, "
                      f"{a.antwort_tokens} Antwort-Token, Abbruchgrund {a.abbruchgrund or 'nicht aufgezeichnet'}")
    if live:
        try:
            antwort = OllamaExtractor(s.llm, modell=modell).extract(fx.seiten)
        except (AnbieterNichtErreichbar, ExtraktionFehler) as e:
            _fehler(str(e))
        console.print(
            f"Ollama {antwort.modell}: {antwort.prompt_tokens} Prompt-Token (geschätzt {geschaetzt}), "
            f"{antwort.antwort_tokens} Antwort-Token, {antwort.laufzeit_s:.1f} s → "
            f"{antwort.zeichen_je_token:.2f} Zeichen/Token" if antwort.zeichen_je_token else "keine Token-Angabe"
        )
        console.print("  Hinweis: Ollama zählt bei wiederverwendetem Präfix (Prompt-Cache) evtl. weniger Token.")
        belegt, alle = antwort.belegte_felder
        console.print(f"  Antwort: {belegt}/{alle} Felder belegt, {len(antwort.ergebnis.personen)} Personen, "
                      f"Abbruchgrund {antwort.abbruchgrund or '–'}")
        if belegt <= 3:
            console.print("[yellow]  Fast leere Antwort – mit --speichern den Prompt ansehen.[/yellow]")
        if antwort.kontext_voll:
            console.print(f"[yellow]  Kontext fast voll ({s.llm.num_ctx}) – Ollama hat vermutlich abgeschnitten.[/yellow]")
    if speichern:
        from .extract.ollama import SYSTEM_PROMPT

        ziel = s.pfad("data") / f"prompt_{d.domain}.txt"
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_text(f"=== SYSTEM ===\n{SYSTEM_PROMPT}\n\n=== USER ===\n{p.text}\n", encoding="utf-8")
        console.print(f"Prompt gespeichert: {ziel} (enthält Seiteninhalte – nicht weitergeben, wenn echte Daten)")


def _quote(e: EvalErgebnis, feld: str) -> str:
    q = e.quote(feld)
    return "–" if q is None else f"{e.richtig.get(feld, 0)}/{e.bewertet[feld]} ({q:.0%})"


def zeige_eval(erg: EvalErgebnis, gesamt: int) -> None:
    offen = gesamt - erg.fixtures
    console.print(f"Fixtures: {erg.fixtures} ausgewertet{f', {offen} noch ohne fertige erwartet.json' if offen else ''}; "
                  f"Modell {erg.modell}")
    if erg.ohne_aufzeichnung:
        console.print(f"[yellow]ohne LLM-Aufzeichnung (LLM-Felder nicht bewertet):[/yellow] "
                      f"{', '.join(erg.ohne_aufzeichnung)} – `recherche fixture-eval --live`")
    t = Table(title="Trefferquote je Feld")
    for col in ("Feld", "richtig / bewertet", "Kernfeld"):
        t.add_column(col, justify="left" if col == "Feld" else "right")
    for feld in FELDER:
        t.add_row(feld, _quote(erg, feld), "●" if feld in KERNFELDER else "")
    console.print(t)
    laufzeit = erg.mittlere_laufzeit_s
    p, a, g = erg.mittlere_tokens
    if laufzeit is not None:
        console.print(
            f"Je Kanzlei im Mittel: {laufzeit:.1f} s, Token {g:.0f} (Prompt {p:.0f}, Antwort {a:.0f}) "
            f"über {len(erg.laufzeiten)} Antworten"
        )
    for f in erg.fehler:
        console.print(f"  ✗ {f.domain} · {f.feld}: ist {f.ist!r} / soll {f.soll!r}")


def zeige_modellvergleich(ergebnisse: list[EvalErgebnis]) -> None:
    t = Table(title="Modellvergleich (aufgezeichnete Antworten)")
    for col in ("Modell", *KERNFELDER, "Ø s/Kanzlei", "Ø Token"):
        t.add_column(col, justify="left" if col == "Modell" else "right")
    for e in ergebnisse:
        g = e.mittlere_tokens[2]
        t.add_row(e.modell, *(_quote(e, f) for f in KERNFELDER),
                  _zahl(e.mittlere_laufzeit_s, 1), _zahl(g))  # fmt: skip
    console.print(t)


@app.command()
def export(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Ziel-.xlsx")] = None,
) -> None:
    """Excel-Liste mit allen vier Blättern schreiben."""
    _export(_kontext(config, bundesland, db), out)


@app.command()
def run(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    orte: OrteOpt = None,
    adapter: Annotated[str | None, typer.Option(help="Websuche-Adapter, z. B. ddgs für lokale Tests")] = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Ziel-.xlsx")] = None,
    ja: JaOpt = False,
) -> None:
    """Gesamtlauf Stufe A: search → crawl → extract → export. Nach Abbruch einfach erneut starten."""
    ctx = _kontext(config, bundesland, db)
    if not _search(ctx, orte, adapter, _lauf_id(), ja):
        return
    _crawl(ctx)
    _extract(ctx)
    _export(ctx, out)


@app.command("search-compare")
def search_compare(
    orte: Annotated[str, typer.Option("--orte", help="Kommagetrennte Orte, z. B. Kiel,Husum")],
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    adapter: Annotated[
        str | None, typer.Option("--adapter", help="Kommagetrennte Adapter statt aller aktiven")
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="CSV mit Kanzlei je Ort und Adapter")] = None,
) -> None:
    """Aktive Websuche-Adapter vergleichen (Stufe B nur mit --adapter; schreibt keine Kandidaten, nutzt den API-Cache)."""
    ctx = _kontext(config, bundesland, db)
    with ctx.storage() as st:
        if adapter:
            adapters = [build_adapter(n.strip(), ctx.settings, cache=st) for n in adapter.split(",") if n.strip()]
        else:
            # ohne --adapter nur die Websuche: Stufe-B-Quellen (Places) kosten je Rasterzelle und laufen nur ausdrücklich
            adapters = aktive_adapter(ctx.settings, cache=st, stufe="A")
        ergebnis = compare_adapters(
            adapters, ctx.orte(orte), ctx.vorlagen(), ctx.blocklist(),
            ctx.settings.discovery.stufe_a.max_results_per_query,
        )  # fmt: skip
    zeige_vergleich(ergebnis)
    if out:
        console.print(f"CSV geschrieben: {ergebnis.als_csv(out)}")


def zeige_vergleich(erg: VergleichsErgebnis, max_namen: int = 30) -> None:
    for name, grund in erg.uebersprungen.items():
        console.print(f"[yellow]übersprungen:[/yellow] {name} – {grund}")
    if not erg.ergebnisse:
        console.print("[red]Kein Adapter einsatzbereit.[/red]")
        return
    namen = list(erg.ergebnisse)

    t = Table(title="Kanzleien je Adapter und Ort")
    t.add_column("Ort")
    for n in namen:
        t.add_column(n, justify="right")
    t.add_column("alle Adapter", justify="right")
    t.add_column("Vereinigung", justify="right")
    for ort in [*erg.orte, None]:
        t.add_row(
            ort or "[bold]Gesamt[/bold]",
            *(str(len(erg.kanzleien(n, ort))) for n in namen),
            str(len(erg.ueberschneidung(ort))),
            str(len(erg.alle(ort))),
        )
    console.print(t)

    q = Table(title="Je Adapter")
    for col in ("Adapter", "Abfragen", "Fehler", "Treffer", "Kanzleien", "nur dieser", "Anfragen", "Kosten"):
        q.add_column(col, justify="left" if col == "Adapter" else "right")
    for n, e in erg.ergebnisse.items():
        q.add_row(
            n, str(e.queries), str(e.fehler), str(e.treffer), str(len(e.kanzleien)),
            str(len(erg.exklusiv(n))), str(e.anfragen), f"{e.kosten:.2f}",
        )  # fmt: skip
    console.print(q)
    for n, e in erg.ergebnisse.items():
        if e.abbruch:
            console.print(f"[yellow]{n} vorzeitig beendet:[/yellow] {e.abbruch}")

    if len(namen) >= 2:
        m = Table(title="Überschneidung je Paar (gemeinsam / nur Zeile / nur Spalte)")
        m.add_column("")
        for n in namen:
            m.add_column(n, justify="right")
        paare = {(p.a, p.b): p for p in erg.paare()}
        for a in namen:
            zellen = []
            for b in namen:
                if a == b:
                    zellen.append(str(len(erg.kanzleien(a))))
                elif (a, b) in paare:
                    p = paare[(a, b)]
                    zellen.append(f"{len(p.beide)} / {len(p.nur_a)} / {len(p.nur_b)}")
                else:
                    p = paare[(b, a)]
                    zellen.append(f"{len(p.beide)} / {len(p.nur_b)} / {len(p.nur_a)}")
            m.add_row(a, *zellen)
        console.print(m)

    for n in namen:
        exklusiv = erg.namen(erg.exklusiv(n))
        if exklusiv:
            rest = f" … (+{len(exklusiv) - max_namen})" if len(exklusiv) > max_namen else ""
            console.print(f"  [bold]nur in {n}:[/bold] {', '.join(exklusiv[:max_namen])}{rest}")


@app.command()
def orte(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Ziel-CSV statt der Ortsliste aus settings.yaml")] = None,
    force: Annotated[bool, typer.Option("--force", help="bestehende Ortsliste überschreiben")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="nur Zusammenfassung, nichts schreiben")] = False,
    gv: Annotated[str | None, typer.Option("--gv", help="GV-ISys-Datei oder URL statt orte.gv_isys.quelle")] = None,
    ohne_gv: Annotated[bool, typer.Option("--ohne-gv", help="ohne GV-ISys (keine Einwohner, Kreis aus OpenPLZ)")] = False,
) -> None:
    """Vollständige Gemeindeliste aus OpenPLZ, Kreis und Einwohner aus dem GV-ISys (Destatis)."""
    ctx = _kontext(config, bundesland, db)
    s = ctx.settings
    land = LAND_SCHLUESSEL[ctx.bundesland]
    ziel = out or s.pfad(ctx.bl_cfg.orte_datei)
    with ctx.storage() as st:
        quelle = OpenPlzOrte(s.openplz.url, cache=st, page_size=s.openplz.page_size, pause_s=s.openplz.pause_s)
        try:
            gemeinden = gemeinden_aus_localities(quelle.localities(land))
            gvdaten = None
            if not ohne_gv:
                gv_quelle = gv or s.orte.gv_isys.quelle
                if not gv_quelle:
                    _fehler("Keine GV-ISys-Quelle: orte.gv_isys.quelle in settings.yaml setzen, --gv angeben oder --ohne-gv")
                if not gv and not gv_quelle.startswith(("http://", "https://")):
                    gv_quelle = str(s.pfad(gv_quelle))
                pfad = gv_quelle_laden(gv_quelle, s.pfad("data/gv_isys"))
                gvdaten = lade_gv(pfad, s.orte.gv_isys.spalten, land, s.orte.gv_isys.blatt)
        except OrteFehler as e:
            _fehler(str(e))
    erg = orte_erzeugen(gemeinden, gvdaten, s.orte.stadtteile)
    gem = erg.gemeinden
    mit_ew = sum(1 for o in gem if o.einwohner is not None)
    console.print(
        f"[bold]{ctx.bl_cfg.name}[/bold]: {len(gem)} Gemeinden ({mit_ew} mit Einwohnerzahl), "
        f"{len(erg.zusatz)} Zusatz-Suchorte (PLZ-Bezirke großer Städte), {quelle.anfragen} OpenPLZ-Anfragen."
    )
    for w in erg.warnungen[:20]:
        console.print(f"  [yellow]Warnung:[/yellow] {w}")
    if len(erg.warnungen) > 20:
        console.print(f"  … und {len(erg.warnungen) - 20} weitere Warnungen")
    referenz = load_referenz(s.pfad(s.pfade.referenz), ctx.bundesland)
    kreise = sorted({o.kreis for o in gem if o.kreis})
    if referenz and (fehlend := [k for k in kreise if k not in referenz.kreise]):
        console.print(f"  [yellow]Kreise nicht in referenz_kammern.yaml:[/yellow] {', '.join(fehlend)}")

    plan = ctx.vorlagen()
    t = Table(title="Suchumfang je Einwohnerstufe (Stufe A)")
    for col in ("Stufe", "Orte", "Queries", "Seiten je Query"):
        t.add_column(col, justify="left" if col == "Stufe" else "right")
    gesamt_min = gesamt_max = 0
    max_seiten = max(1, ctx.settings.discovery.stufe_a.max_results_per_query // 20)
    for stufe, (n_orte, n_queries, seiten) in stufen_verteilung(erg.orte, plan).items():
        t.add_row(stufe, str(n_orte), str(n_queries), str(seiten or max_seiten))
        gesamt_min += n_queries
        gesamt_max += n_queries * (seiten or max_seiten)
    console.print(t)
    console.print(f"Geschätzt {gesamt_min}–{gesamt_max} Such-Anfragen für den Gesamtlauf "
                  f"(weniger, wenn Seiten nicht voll sind; mehr nur bei vollen Kanzlei-Seiten).")

    if ziel.exists():
        v = vergleichen(load_orte(ziel), erg.orte)
        console.print(f"Vergleich mit {ziel.name}: bisher {v.alt}, neu {v.neu} Gemeinden; "
                      f"{len(v.hinzu)} hinzu, {len(v.entfallen)} entfallen.")
        if v.entfallen:
            console.print(f"  entfallen (Namen prüfen): {', '.join(v.entfallen[:20])}{' …' if len(v.entfallen) > 20 else ''}")
    if dry_run:
        console.print("[yellow]--dry-run: nichts geschrieben.[/yellow]")
        return
    if ziel.exists() and not force:
        _fehler(f"{ziel} existiert bereits – mit --force überschreiben oder mit --out in eine andere Datei schreiben.")
    console.print(f"Ortsliste geschrieben: [bold]{schreibe_orte(erg.orte, ziel)}[/bold]")


if __name__ == "__main__":  # pragma: no cover
    app()
