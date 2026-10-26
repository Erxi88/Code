"""SQLite-Speicher: Kandidaten, Rohseiten-Cache, Kanzleien und Protokolle.

Jeder Schritt der Pipeline liest und schreibt hier seinen Fortschritt, damit ein
abgebrochener Lauf beim nächsten Aufruf dort weitermacht, wo er aufgehört hat.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from .models import (
    Ansprechpartner,
    Dublette,
    Kandidat,
    Kanzlei,
    LaufEintrag,
    NichtGefunden,
    SchrittStatus,
    Seite,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS kandidaten (
    domain          TEXT PRIMARY KEY,
    bundesland      TEXT NOT NULL,
    url             TEXT NOT NULL,
    ort             TEXT,
    suchbegriff     TEXT,
    quelle          TEXT,
    titel           TEXT,
    snippet         TEXT,
    gefunden_am     TEXT NOT NULL,
    crawl_status    TEXT NOT NULL DEFAULT 'offen',
    extract_status  TEXT NOT NULL DEFAULT 'offen',
    fehler          TEXT
);
CREATE INDEX IF NOT EXISTS ix_kandidaten_bl ON kandidaten (bundesland, crawl_status, extract_status);

CREATE TABLE IF NOT EXISTS seiten (
    url             TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    seitentyp       TEXT NOT NULL,
    http_status     INTEGER,
    text            TEXT,
    abgerufen_am    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_seiten_domain ON seiten (domain);

CREATE TABLE IF NOT EXISTS kanzleien (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    domain              TEXT NOT NULL UNIQUE,
    lauf_bundesland     TEXT NOT NULL,
    kanzleiname         TEXT,
    rechtsform          TEXT,
    strasse             TEXT,
    plz                 TEXT,
    ort                 TEXT,
    bundesland          TEXT,
    telefon             TEXT,
    email               TEXT,
    website             TEXT,
    ansprechpartner     TEXT NOT NULL DEFAULT '[]',
    teamgroesse         INTEGER,
    teamgroesse_klasse  TEXT,
    teamgroesse_basis   TEXT,
    konfidenz           TEXT NOT NULL,
    quell_urls          TEXT NOT NULL DEFAULT '[]',
    abrufdatum          TEXT,
    status              TEXT NOT NULL DEFAULT 'neu'
);
CREATE INDEX IF NOT EXISTS ix_kanzleien_bl ON kanzleien (lauf_bundesland);

CREATE TABLE IF NOT EXISTS nicht_gefunden (
    domain          TEXT PRIMARY KEY,
    bundesland      TEXT NOT NULL,
    grund           TEXT NOT NULL,
    url             TEXT,
    zeitpunkt       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dubletten (
    dublette_domain TEXT PRIMARY KEY,
    behalten_domain TEXT NOT NULL,
    bundesland      TEXT NOT NULL,
    kriterium       TEXT NOT NULL,
    kanzleiname     TEXT,
    details         TEXT,
    zeitpunkt       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lauf_protokoll (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lauf_id         TEXT NOT NULL,
    schritt         TEXT NOT NULL,
    bundesland      TEXT NOT NULL,
    suchbegriff     TEXT,
    adapter         TEXT,
    ort             TEXT,
    trefferzahl     INTEGER NOT NULL DEFAULT 0,
    neue_domains    INTEGER NOT NULL DEFAULT 0,
    laufzeit_s      REAL NOT NULL DEFAULT 0,
    fehler          TEXT,
    zeitpunkt       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lauf_query ON lauf_protokoll (bundesland, schritt, adapter, suchbegriff);
"""

_KANZLEI_FELDER = [
    "domain", "lauf_bundesland", "kanzleiname", "rechtsform", "strasse", "plz", "ort",
    "bundesland", "telefon", "email", "website", "ansprechpartner", "teamgroesse",
    "teamgroesse_klasse", "teamgroesse_basis", "konfidenz", "quell_urls", "abrufdatum",
    "status",
]  # fmt: skip


def _iso(wert: date | datetime | None) -> str | None:
    return wert.isoformat() if wert is not None else None


class Storage:
    """Dünne Schicht über sqlite3. Als Kontextmanager verwendbar."""

    def __init__(self, pfad: str | Path):
        self.pfad = Path(pfad)
        if str(pfad) != ":memory:":
            self.pfad.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(pfad))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrieren()

    def _migrieren(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Datenbank hat Schemaversion {version}, dieses Programm kennt nur {SCHEMA_VERSION}"
            )
        with self.conn:
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ Kandidaten

    def add_kandidat(self, k: Kandidat) -> bool:
        """Legt einen Kandidaten an. False, wenn die Domain schon bekannt ist."""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO kandidaten
                   (domain, bundesland, url, ort, suchbegriff, quelle, titel, snippet,
                    gefunden_am, crawl_status, extract_status, fehler)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    k.domain, k.bundesland, k.url, k.ort, k.suchbegriff, k.quelle, k.titel,
                    k.snippet, _iso(k.gefunden_am), k.crawl_status.value,
                    k.extract_status.value, k.fehler,
                ),
            )  # fmt: skip
        return cur.rowcount == 1

    def get_kandidat(self, domain: str) -> Kandidat | None:
        row = self.conn.execute("SELECT * FROM kandidaten WHERE domain = ?", (domain,)).fetchone()
        return Kandidat(**dict(row)) if row else None

    def kandidaten(
        self,
        bundesland: str,
        crawl_status: SchrittStatus | None = None,
        extract_status: SchrittStatus | None = None,
    ) -> list[Kandidat]:
        sql = "SELECT * FROM kandidaten WHERE bundesland = ?"
        params: list = [bundesland]
        if crawl_status is not None:
            sql += " AND crawl_status = ?"
            params.append(crawl_status.value)
        if extract_status is not None:
            sql += " AND extract_status = ?"
            params.append(extract_status.value)
        sql += " ORDER BY domain"
        return [Kandidat(**dict(r)) for r in self.conn.execute(sql, params)]

    def set_crawl_status(self, domain: str, status: SchrittStatus, fehler: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE kandidaten SET crawl_status = ?, fehler = ? WHERE domain = ?",
                (status.value, fehler, domain),
            )

    def set_extract_status(
        self, domain: str, status: SchrittStatus, fehler: str | None = None
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE kandidaten SET extract_status = ?, fehler = ? WHERE domain = ?",
                (status.value, fehler, domain),
            )

    # ------------------------------------------------------------------ Seiten-Cache

    def hat_seite(self, url: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seiten WHERE url = ?", (url,)).fetchone() is not None

    def save_seite(self, s: Seite) -> bool:
        """Speichert eine Rohseite. Bereits gecachte URLs werden nie überschrieben."""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO seiten (url, domain, seitentyp, http_status, text, abgerufen_am)
                   VALUES (?,?,?,?,?,?)""",
                (s.url, s.domain, s.seitentyp.value, s.http_status, s.text, _iso(s.abgerufen_am)),
            )
        return cur.rowcount == 1

    def get_seite(self, url: str) -> Seite | None:
        row = self.conn.execute("SELECT * FROM seiten WHERE url = ?", (url,)).fetchone()
        return Seite(**dict(row)) if row else None

    def seiten(self, domain: str) -> list[Seite]:
        rows = self.conn.execute("SELECT * FROM seiten WHERE domain = ? ORDER BY url", (domain,))
        return [Seite(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ Kanzleien

    def upsert_kanzlei(self, k: Kanzlei) -> int:
        """Legt eine Kanzlei an oder aktualisiert sie (Schlüssel: Domain). Gibt die ID zurück."""
        werte = self._kanzlei_werte(k)
        spalten = ", ".join(_KANZLEI_FELDER)
        platzhalter = ", ".join("?" for _ in _KANZLEI_FELDER)
        update = ", ".join(f"{f} = excluded.{f}" for f in _KANZLEI_FELDER if f != "domain")
        with self.conn:
            self.conn.execute(
                f"INSERT INTO kanzleien ({spalten}) VALUES ({platzhalter}) "
                f"ON CONFLICT(domain) DO UPDATE SET {update}",
                werte,
            )
        return self.conn.execute(
            "SELECT id FROM kanzleien WHERE domain = ?", (k.domain,)
        ).fetchone()[0]

    @staticmethod
    def _kanzlei_werte(k: Kanzlei) -> list:
        d = k.model_dump(mode="json", exclude={"id"})
        d["ansprechpartner"] = json.dumps(d["ansprechpartner"], ensure_ascii=False)
        d["quell_urls"] = json.dumps(d["quell_urls"], ensure_ascii=False)
        return [d[f] for f in _KANZLEI_FELDER]

    @staticmethod
    def _kanzlei_aus_row(row: sqlite3.Row) -> Kanzlei:
        d = dict(row)
        d["ansprechpartner"] = [Ansprechpartner(**a) for a in json.loads(d["ansprechpartner"])]
        d["quell_urls"] = json.loads(d["quell_urls"])
        return Kanzlei(**d)

    def get_kanzlei(self, domain: str) -> Kanzlei | None:
        row = self.conn.execute("SELECT * FROM kanzleien WHERE domain = ?", (domain,)).fetchone()
        return self._kanzlei_aus_row(row) if row else None

    def kanzleien(self, bundesland: str) -> list[Kanzlei]:
        rows = self.conn.execute(
            "SELECT * FROM kanzleien WHERE lauf_bundesland = ? ORDER BY plz, kanzleiname, domain",
            (bundesland,),
        )
        return [self._kanzlei_aus_row(r) for r in rows]

    def delete_kanzlei(self, domain: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM kanzleien WHERE domain = ?", (domain,))

    # ------------------------------------------------------------------ Nicht gefunden

    def add_nicht_gefunden(self, n: NichtGefunden) -> None:
        """Ein Eintrag je Domain; ein erneuter Fehler ersetzt den alten Grund."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO nicht_gefunden (domain, bundesland, grund, url, zeitpunkt) "
                "VALUES (?,?,?,?,?)",
                (n.domain, n.bundesland, n.grund, n.url, _iso(n.zeitpunkt)),
            )

    def nicht_gefunden(self, bundesland: str) -> list[NichtGefunden]:
        rows = self.conn.execute(
            "SELECT * FROM nicht_gefunden WHERE bundesland = ? ORDER BY domain", (bundesland,)
        )
        return [NichtGefunden(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ Dubletten

    def add_dublette(self, d: Dublette) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO dubletten
                   (dublette_domain, behalten_domain, bundesland, kriterium, kanzleiname, details, zeitpunkt)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    d.dublette_domain, d.behalten_domain, d.bundesland, d.kriterium,
                    d.kanzleiname, d.details, _iso(d.zeitpunkt),
                ),
            )  # fmt: skip

    def dubletten(self, bundesland: str) -> list[Dublette]:
        rows = self.conn.execute(
            "SELECT * FROM dubletten WHERE bundesland = ? ORDER BY behalten_domain, dublette_domain",
            (bundesland,),
        )
        return [Dublette(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ Lauf-Protokoll

    def log_lauf(self, e: LaufEintrag) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO lauf_protokoll
                   (lauf_id, schritt, bundesland, suchbegriff, adapter, ort, trefferzahl,
                    neue_domains, laufzeit_s, fehler, zeitpunkt)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    e.lauf_id, e.schritt, e.bundesland, e.suchbegriff, e.adapter, e.ort,
                    e.trefferzahl, e.neue_domains, e.laufzeit_s, e.fehler, _iso(e.zeitpunkt),
                ),
            )  # fmt: skip

    def lauf_protokoll(self, bundesland: str) -> list[LaufEintrag]:
        rows = self.conn.execute(
            "SELECT * FROM lauf_protokoll WHERE bundesland = ? ORDER BY id", (bundesland,)
        )
        return [LaufEintrag(**{k: v for k, v in dict(r).items() if k != "id"}) for r in rows]

    def query_erledigt(self, bundesland: str, adapter: str, suchbegriff: str) -> bool:
        """True, wenn diese Query mit diesem Adapter bereits erfolgreich lief (Wiederaufnahme)."""
        row = self.conn.execute(
            """SELECT 1 FROM lauf_protokoll
               WHERE bundesland = ? AND schritt = 'search' AND adapter = ? AND suchbegriff = ?
                 AND fehler IS NULL LIMIT 1""",
            (bundesland, adapter, suchbegriff),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ Hilfen

    def import_kanzleien(self, kanzleien: Iterable[Kanzlei]) -> int:
        n = 0
        for k in kanzleien:
            self.upsert_kanzlei(k)
            n += 1
        return n
