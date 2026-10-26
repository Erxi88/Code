"""Kommandozeile: recherche search | crawl | extract | export | run | search-compare | orte."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import Settings, load_settings
from .export import export_excel
from .models import SchrittStatus
from .search.compare import VergleichsErgebnis, compare_adapters
from .search.pipeline import (
    load_blocklist,
    load_orte,
    load_vorlagen,
    orte_filtern,
    run_search,
)
from .search.registry import build_adapter, vergleichs_adapter
from .storage import Storage

app = typer.Typer(
    name="recherche",
    help="Recherche von Steuerberaterkanzleien: Suche, Crawling, Extraktion, Excel-Export.",
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

    def orte(self, auswahl: str | None):
        orte = load_orte(self.settings.pfad(self.bl_cfg.orte_datei))
        return orte_filtern(orte, auswahl.split(",") if auswahl else None)


def _lauf_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _kontext(config, bundesland, db) -> Kontext:
    try:
        return Kontext(config, bundesland, db)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Fehler:[/red] {e}")
        raise typer.Exit(2) from e


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------- Schritte


def _search(ctx: Kontext, orte: str | None, adapter_name: str | None, lauf_id: str) -> None:
    s = ctx.settings
    adapter = build_adapter(adapter_name or s.search.adapter, s)
    grund = adapter.nicht_konfiguriert_grund()
    if grund:
        console.print(f"[red]Such-Adapter {adapter.name} nicht einsatzbereit:[/red] {grund}")
        raise typer.Exit(2)
    ortsliste = ctx.orte(orte)
    console.print(f"Suche mit [bold]{adapter.name}[/bold] in {len(ortsliste)} Orten ({ctx.bundesland}) …")
    with ctx.storage() as st:
        stat = run_search(
            st, adapter, ortsliste,
            load_vorlagen(s.pfad(s.pfade.queries)),
            load_blocklist(s.pfad(s.pfade.blocklist)),
            ctx.bundesland, lauf_id, s.search.max_results_per_query,
            fortschritt=lambda m: console.print(f"  {m}"),
        )  # fmt: skip
    console.print(
        f"Fertig: {stat.queries} Queries, {stat.uebersprungen} schon erledigt, "
        f"{stat.fehler} Fehler, {stat.treffer} Treffer, {stat.neue_domains} neue Domains."
    )
    if stat.fehler:
        console.print("[yellow]Fehlgeschlagene Queries werden beim nächsten Aufruf wiederholt.[/yellow]")


def _crawl(ctx: Kontext) -> None:
    with ctx.storage() as st:
        offen = len(st.kandidaten(ctx.bundesland, crawl_status=SchrittStatus.OFFEN))
    console.print(f"[yellow]crawl: noch nicht implementiert[/yellow] ({offen} Kandidaten offen).")


def _extract(ctx: Kontext) -> None:
    with ctx.storage() as st:
        offen = len(
            st.kandidaten(
                ctx.bundesland, crawl_status=SchrittStatus.FERTIG, extract_status=SchrittStatus.OFFEN
            )
        )
    console.print(
        f"[yellow]extract: noch nicht implementiert[/yellow] ({offen} gecrawlte Kandidaten offen, "
        f"Modell {ctx.settings.llm.modell})."
    )


def _export(ctx: Kontext, out: Path | None) -> Path:
    pfad = out or ctx.settings.ausgabe_pfad(ctx.bundesland)
    with ctx.storage() as st:
        pfad = export_excel(st, ctx.bundesland, pfad)
        n = len(st.kanzleien(ctx.bundesland))
    console.print(f"Excel geschrieben: [bold]{pfad}[/bold] ({n} Kanzleien)")
    return pfad


# ---------------------------------------------------------------------- Befehle


@app.command()
def search(
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    orte: OrteOpt = None,
    adapter: Annotated[str | None, typer.Option(help="Adapter statt search.adapter")] = None,
) -> None:
    """Websuche je Ort; neue Domains werden Kandidaten (wiederaufnehmbar)."""
    _search(_kontext(config, bundesland, db), orte, adapter, _lauf_id())


@app.command()
def crawl(bundesland: BundeslandOpt = None, db: DbOpt = None, config: ConfigOpt = None) -> None:
    """Kandidaten-Websites laden (Startseite, Impressum, Kontakt, Team) – Platzhalter."""
    _crawl(_kontext(config, bundesland, db))


@app.command()
def extract(bundesland: BundeslandOpt = None, db: DbOpt = None, config: ConfigOpt = None) -> None:
    """Daten aus gecachten Seiten extrahieren (Regeln + LLM) – Platzhalter."""
    _extract(_kontext(config, bundesland, db))


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
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Ziel-.xlsx")] = None,
) -> None:
    """Gesamtlauf: search → crawl → extract → export. Nach Abbruch einfach erneut starten."""
    ctx = _kontext(config, bundesland, db)
    _search(ctx, orte, None, _lauf_id())
    _crawl(ctx)
    _extract(ctx)
    _export(ctx, out)


@app.command("search-compare")
def search_compare(
    orte: Annotated[str, typer.Option("--orte", help="Kommagetrennte Orte, z. B. Kiel,Husum")],
    bundesland: BundeslandOpt = None,
    db: DbOpt = None,
    config: ConfigOpt = None,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="CSV mit Domain je Ort und Adapter")
    ] = None,
) -> None:
    """Alle konfigurierten Such-Adapter vergleichen (schreibt nichts in die Datenbank)."""
    ctx = _kontext(config, bundesland, db)
    s = ctx.settings
    ergebnis = compare_adapters(
        vergleichs_adapter(s),
        ctx.orte(orte),
        load_vorlagen(s.pfad(s.pfade.queries)),
        load_blocklist(s.pfad(s.pfade.blocklist)),
        s.search.max_results_per_query,
    )
    zeige_vergleich(ergebnis)
    if out:
        console.print(f"CSV geschrieben: {ergebnis.als_csv(out)}")


def zeige_vergleich(erg: VergleichsErgebnis) -> None:
    for name, grund in erg.uebersprungen.items():
        console.print(f"[yellow]übersprungen:[/yellow] {name} – {grund}")
    if not erg.ergebnisse:
        console.print("[red]Kein Adapter einsatzbereit.[/red]")
        return
    namen = list(erg.ergebnisse)

    t = Table(title="Kanzlei-Domains je Adapter")
    t.add_column("Ort")
    for n in namen:
        t.add_column(n, justify="right")
    t.add_column("alle", justify="right")
    t.add_column("Union", justify="right")
    for ort in [*erg.orte, None]:
        t.add_row(
            ort or "[bold]Gesamt[/bold]",
            *(str(len(erg.domains(n, ort))) for n in namen),
            str(len(erg.ueberschneidung(ort))),
            str(len(erg.alle_domains(ort))),
        )
    console.print(t)

    q = Table(title="Queries je Adapter")
    for col in ("Adapter", "Queries", "Fehler", "Treffer"):
        q.add_column(col, justify="left" if col == "Adapter" else "right")
    for n, e in erg.ergebnisse.items():
        q.add_row(n, str(e.queries), str(e.fehler), str(e.treffer))
    console.print(q)

    for p in erg.paare():
        pt = Table(title=f"{p.a} vs. {p.b}")
        pt.add_column("beide", justify="right")
        pt.add_column(f"nur {p.a}", justify="right")
        pt.add_column(f"nur {p.b}", justify="right")
        pt.add_row(str(len(p.beide)), str(len(p.nur_a)), str(len(p.nur_b)))
        console.print(pt)
        for titel, menge in ((f"nur in {p.a}", p.nur_a), (f"nur in {p.b}", p.nur_b)):
            if menge:
                console.print(f"  [bold]{titel}:[/bold] {', '.join(sorted(menge))}")


@app.command()
def orte(bundesland: BundeslandOpt = None, config: ConfigOpt = None) -> None:
    """Vollständige Ortsliste aus OpenPLZ erzeugen – Platzhalter."""
    ctx = _kontext(config, bundesland, None)
    console.print(
        f"[yellow]orte: noch nicht implementiert[/yellow] – derzeit Startliste "
        f"{ctx.bl_cfg.orte_datei} mit {len(ctx.orte(None))} Orten."
    )


if __name__ == "__main__":  # pragma: no cover
    app()
