"""Kommandozeile: recherche search | fill | coverage | crawl | extract | export | run |
search-compare | orte | fixture-add | fixture-eval."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import Settings, load_settings
from .coverage import CoverageErgebnis, als_excel, berechne, load_referenz
from .crawl import run_crawl
from .export import export_excel
from .extract.base import AnbieterNichtErreichbar
from .extract.lauf import run_extract
from .extract.ollama import OllamaExtractor
from .extract.rules import Ausschluss
from .fixtures import (
    FELDER,
    KERNFELDER,
    EvalErgebnis,
    aufgezeichnete_modelle,
    auswerten,
    fixture_holen_sync,
    fixtures_finden,
)
from .models import LaufEintrag, SchrittStatus
from .normalize import PlzPruefer
from .search.base import OrtsAdapter, QueryAdapter, SearchAdapter
from .search.compare import VergleichsErgebnis, compare_adapters
from .search.pipeline import (
    Ort,
    SuchStatistik,
    load_blocklist,
    load_orte,
    load_vorlagen,
    orte_filtern,
    run_search,
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

    def orte(self, auswahl: str | None) -> list[Ort]:
        orte = load_orte(self.settings.pfad(self.bl_cfg.orte_datei))
        return orte_filtern(orte, auswahl.split(",") if auswahl else None)

    def vorlagen(self) -> list[str]:
        return load_vorlagen(self.settings.pfad(self.settings.pfade.queries))

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


def _lauf(ctx: Kontext, st: Storage, adapter: SearchAdapter, orte: list[Ort], lauf_id: str) -> SuchStatistik:
    stufe = "Websuche (Stufe A)" if isinstance(adapter, QueryAdapter) else "Lückenfüller (Stufe B)"
    console.print(f"{stufe} mit [bold]{adapter.name}[/bold] in {len(orte)} Orten ({ctx.bundesland}) …")
    stat = run_search(
        st, adapter, orte, ctx.vorlagen(), ctx.blocklist(), ctx.bundesland, lauf_id,
        ctx.settings.discovery.stufe_a.max_results_per_query,
        fortschritt=lambda m: console.print(f"  {m}"),
    )  # fmt: skip
    console.print(
        f"Fertig: {stat.queries} Abfragen, {stat.uebersprungen} schon erledigt, {stat.fehler} Fehler, "
        f"{stat.treffer} Treffer, {stat.neue_kandidaten} neue Kandidaten."
    )
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


def _search(ctx: Kontext, orte: str | None, adapter_name: str | None, lauf_id: str) -> None:
    with ctx.storage() as st:
        name = adapter_name or ctx.settings.discovery.stufe_a.adapter
        adapter = build_adapter(name, ctx.settings, cache=st)
        if not isinstance(adapter, QueryAdapter):
            _fehler(f"{name} ist ein Lückenfüller (Stufe B) – bitte `recherche fill --quelle {name}` verwenden")
        _adapter_pruefen(adapter, explizit=adapter_name is not None)
        _lauf(ctx, st, adapter, ctx.orte(orte), lauf_id)


def _crawl(ctx: Kontext, limit: int | None = None, wiederholen: bool = False) -> None:
    with ctx.storage() as st:
        if wiederholen:
            console.print(f"{st.fehler_zuruecksetzen(ctx.bundesland, 'crawl')} fehlgeschlagene Kandidaten wieder offen.")
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
        stat = run_crawl(st, cfg, ctx.bundesland, ctx.blocklist(), limit=limit,
                         fortschritt=lambda m: console.print(f"  {m}"))  # fmt: skip
        st.log_lauf(
            LaufEintrag(lauf_id=_lauf_id(), schritt="crawl", bundesland=ctx.bundesland,
                        suchbegriff=f"{stat.fertig} gelesen, {stat.nicht_gefunden} nicht gefunden",
                        trefferzahl=stat.fertig, anfragen=stat.anfragen,
                        laufzeit_s=round(time.monotonic() - start, 2))
        )  # fmt: skip
    console.print(f"Fertig: {stat.fertig} Websites gelesen, {stat.nicht_gefunden} nicht gefunden, {stat.anfragen} Anfragen.")
    for grund, n in sorted(stat.gruende.items(), key=lambda x: -x[1]):
        console.print(f"  {n:4d} × {grund}")


def _extractor(ctx: Kontext, modell: str | None) -> OllamaExtractor:
    return OllamaExtractor(ctx.settings.llm, modell=modell)


def _extract(
    ctx: Kontext, ohne_llm: bool = False, modell: str | None = None, limit: int | None = None,
    wiederholen: bool = False,
) -> None:
    s = ctx.settings
    with ctx.storage() as st:
        if wiederholen:
            console.print(f"{st.fehler_zuruecksetzen(ctx.bundesland, 'extract')} fehlgeschlagene Kandidaten wieder offen.")
        extractor = None if ohne_llm else _extractor(ctx, modell)
        pruefer = PlzPruefer(s.openplz.url, cache=st)
        ausschluss = Ausschluss.laden(s.pfad(s.pfade.impressum_ausschluss))
        console.print(f"Extraktion mit {'nur Regeln' if ohne_llm else 'Regeln + ' + extractor.modell} …")
        stat = run_extract(st, ctx.bundesland, extractor, pruefer, ausschluss, _lauf_id(), limit=limit,
                           fortschritt=lambda m: console.print(f"  {m}"))  # fmt: skip
    console.print(
        f"Fertig: {stat.fertig} Kanzleien ({stat.ohne_website} ohne Website), {stat.fehler} Fehler, "
        f"{stat.llm_aufrufe} LLM-Aufrufe, {pruefer.anfragen} OpenPLZ-Anfragen."
    )
    if stat.llm_aufrufe:
        console.print(f"  im Mittel {stat.mittlere_laufzeit_s:.1f} s und {stat.tokens / stat.llm_aufrufe:.0f} Token je Kanzlei")
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
    return berechne(st, ctx.bundesland, ctx.orte(None), referenz)


# ---------------------------------------------------------------------- Befehle


@app.command()
def search(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    orte: OrteOpt = None,
    adapter: Annotated[str | None, typer.Option(help="Websuche-Adapter statt discovery.stufe_a.adapter")] = None,
) -> None:
    """Stufe A: Websuche je Ort mit allen Query-Vorlagen (wiederaufnehmbar)."""
    _search(_kontext(config, bundesland, db), orte, adapter, _lauf_id())


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
            auswahl = orte_filtern(ctx.orte(None), namen)
        else:
            auswahl = ctx.orte(orte if orte else None)
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
) -> None:
    """Daten aus den gecachten Seiten extrahieren (Regeln + LLM) und mit der Discovery zusammenführen."""
    _extract(_kontext(config, bundesland, db), ohne_llm, modell, limit, wiederholen)


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
    console.print("Jetzt erwartet.json von Hand ausfüllen und _status auf 'fertig' setzen.")


@app.command("fixture-eval")
def fixture_eval(
    config: ConfigOpt = None,
    modell: Annotated[str | None, typer.Option(help="Modell (Standard: llm.modell)")] = None,
    live: Annotated[bool, typer.Option("--live", help="Ollama fragen und Antworten aufzeichnen")] = False,
    nur: Annotated[str | None, typer.Option(help="nur 'beispiele' oder 'echt'")] = None,
) -> None:
    """Trefferquote je Feld über die Kanzleiseiten-Fixtures, mit Laufzeit und Token je Kanzlei."""
    s = load_settings(config)
    ordner = {"beispiele": s.pfad(s.pfade.fixtures_beispiele), "echt": s.pfad(s.pfade.fixtures_echt)}
    fixtures = fixtures_finden([p for n, p in ordner.items() if nur in (None, n)])
    if not fixtures:
        _fehler("Keine Fixtures gefunden (recherche fixture-add --domain …)")
    ausschluss = Ausschluss.laden(s.pfad(s.pfade.impressum_ausschluss))
    modell = modell or s.llm.modell
    extractor = OllamaExtractor(s.llm, modell=modell) if live else None
    try:
        erg = auswerten(fixtures, ausschluss, modell, extractor)
    except AnbieterNichtErreichbar as e:
        _fehler(str(e))
    zeige_eval(erg, len(fixtures))
    modelle = aufgezeichnete_modelle(fixtures)
    if len(modelle) > 1:
        zeige_modellvergleich([auswerten(fixtures, ausschluss, m) for m in modelle])


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
) -> None:
    """Gesamtlauf Stufe A: search → crawl → extract → export. Nach Abbruch einfach erneut starten."""
    ctx = _kontext(config, bundesland, db)
    _search(ctx, orte, adapter, _lauf_id())
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
    """Alle aktiven Adapter vergleichen (schreibt keine Kandidaten, nutzt aber den API-Cache)."""
    ctx = _kontext(config, bundesland, db)
    with ctx.storage() as st:
        if adapter:
            adapters = [build_adapter(n.strip(), ctx.settings, cache=st) for n in adapter.split(",") if n.strip()]
        else:
            adapters = aktive_adapter(ctx.settings, cache=st)
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
def orte(bundesland: BundeslandOpt = None, config: ConfigOpt = None) -> None:
    """Vollständige Ortsliste (OpenPLZ) mit Einwohnern (GV-ISys) erzeugen – Platzhalter."""
    ctx = _kontext(config, bundesland, None)
    liste = ctx.orte(None)
    mit_ew = sum(1 for o in liste if o.einwohner)
    console.print(
        f"[yellow]orte: noch nicht implementiert[/yellow] – derzeit Startliste "
        f"{ctx.bl_cfg.orte_datei} mit {len(liste)} Orten ({mit_ew} mit Einwohnerzahl)."
    )


if __name__ == "__main__":  # pragma: no cover
    app()
