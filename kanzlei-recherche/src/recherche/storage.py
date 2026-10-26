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
    Fund,
    Kandidat,
    Kanzlei,
    LaufEintrag,
    NichtGefunden,
    SchrittStatus,
    Seite,
)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS kandidaten (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bundesland      TEXT NOT NULL,
    domain          TEXT UNIQUE,
    website         TEXT,
    name            TEXT,
    strasse         TEXT,
    plz             TEXT,
    ort             TEXT,
    telefon         TEXT,
    suchort         TEXT,
    quelle          TEXT,
    gefunden_am     TEXT NOT NULL,
    crawl_status    TEXT NOT NULL DEFAULT 'offen',
    extract_status  TEXT NOT NULL DEFAULT 'offen',
    fehler          TEXT
);
CREATE INDEX IF NOT EXISTS ix_kandidaten_bl ON kandidaten (bundesland, crawl_status, extract_status);

CREATE TABLE IF NOT EXISTS funde (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kandidat_id     INTEGER NOT NULL REFERENCES kandidaten (id) ON DELETE CASCADE,
    bundesland      TEXT NOT NULL,
    quelle          TEXT NOT NULL,
    quell_id        TEXT NOT NULL,
    lauf_id         TEXT,
    suchbegriff     TEXT,
    suchort         TEXT,
    name            TEXT,
    strasse         TEXT,
    plz             TEXT,
    ort             TEXT,
    telefon         TEXT,
    website         TEXT,
    snippet         TEXT,
    roh_url         TEXT,
    gefunden_am     TEXT NOT NULL,
    UNIQUE (bundesland, quelle, quell_id)
);
CREATE INDEX IF NOT EXISTS ix_funde_kandidat ON funde (kandidat_id);

CREATE TABLE IF NOT EXISTS api_cache (
    adapter         TEXT NOT NULL,
    schluessel      TEXT NOT NULL,
    antwort         TEXT NOT NULL,
    zeitpunkt       TEXT NOT NULL,
    PRIMARY KEY (adapter, schluessel)
);

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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bundesland      TEXT NOT NULL,
    behalten        TEXT NOT NULL,
    dublette        TEXT NOT NULL,
    kriterium       TEXT NOT NULL,
    art             TEXT NOT NULL,
    kanzleiname     TEXT,
    details         TEXT,
    zeitpunkt       TEXT NOT NULL,
    UNIQUE (bundesland, behalten, dublette, kriterium)
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
    neue_kandidaten INTEGER NOT NULL DEFAULT 0,
    anfragen        INTEGER NOT NULL DEFAULT 0,
    kosten          REAL NOT NULL DEFAULT 0,
    laufzeit_s      REAL NOT NULL DEFAULT 0,
    fehler          TEXT,
    zeitpunkt       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lauf_query ON lauf_protokoll (bundesland, schritt, adapter, suchbegriff);
"""

# Schema v1 → v2: Kandidaten mit eigener ID (Domain optional), Tabelle funde,
# api_cache, Dubletten mit Art, Kostenlog im Lauf-Protokoll.
MIGRATION_V1_V2 = """
ALTER TABLE kandidaten RENAME TO kandidaten_v1;
ALTER TABLE dubletten RENAME TO dubletten_v1;
DROP INDEX IF EXISTS ix_kandidaten_bl;
ALTER TABLE lauf_protokoll RENAME COLUMN neue_domains TO neue_kandidaten;
ALTER TABLE lauf_protokoll ADD COLUMN anfragen INTEGER NOT NULL DEFAULT 0;
ALTER TABLE lauf_protokoll ADD COLUMN kosten REAL NOT NULL DEFAULT 0;
""" + SCHEMA + """
INSERT INTO kandidaten (bundesland, domain, website, name, suchort, quelle, gefunden_am,
                        crawl_status, extract_status, fehler)
    SELECT bundesland, domain, url, titel, ort, quelle, gefunden_am, crawl_status,
           extract_status, fehler
    FROM kandidaten_v1;
INSERT INTO funde (kandidat_id, bundesland, quelle, quell_id, suchbegriff, suchort, name,
                   website, snippet, roh_url, gefunden_am)
    SELECT k.id, v.bundesland, COALESCE(v.quelle, 'unbekannt'), v.url, v.suchbegriff, v.ort,
           v.titel, v.url, v.snippet, v.url, v.gefunden_am
    FROM kandidaten_v1 v JOIN kandidaten k ON k.domain = v.domain;
INSERT INTO dubletten (bundesland, behalten, dublette, kriterium, art, kanzleiname, details, zeitpunkt)
    SELECT bundesland, behalten_domain, dublette_domain, kriterium, 'zusammengeführt',
           kanzleiname, details, zeitpunkt
    FROM dubletten_v1;
DROP TABLE kandidaten_v1;
DROP TABLE dubletten_v1;
"""

_KANDIDAT_FELDER = [
    "bundesland", "domain", "website", "name", "strasse", "plz", "ort", "telefon",
    "suchort", "quelle", "gefunden_am", "crawl_status", "extract_status", "fehler",
]  # fmt: skip
# Felder, die beim Zusammenführen ergänzt (nie überschrieben) werden
ERGAENZBARE_FELDER = ("domain", "website", "name", "strasse", "plz", "ort", "telefon")

_FUND_FELDER = [
    "kandidat_id", "bundesland", "quelle", "quell_id", "lauf_id", "suchbegriff", "suchort",
    "name", "strasse", "plz", "ort", "telefon", "website", "snippet", "roh_url", "gefunden_am",
]  # fmt: skip

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
        if version == 1:
            # executescript committet selbst; Migration läuft in einer expliziten Transaktion
            try:
                self.conn.executescript("BEGIN;\n" + MIGRATION_V1_V2 + "\nPRAGMA user_version = 2;\nCOMMIT;")
            except sqlite3.Error:
                self.conn.rollback()
                raise
            return
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ Kandidaten

    def add_kandidat(self, k: Kandidat) -> int:
        """Legt einen Kandidaten an und gibt seine ID zurück."""
        d = k.model_dump(mode="json", exclude={"id"})
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO kandidaten ({', '.join(_KANDIDAT_FELDER)}) "
                f"VALUES ({', '.join('?' for _ in _KANDIDAT_FELDER)})",
                [d[f] for f in _KANDIDAT_FELDER],
            )
        return cur.lastrowid

    def ergaenze_kandidat(self, kid: int, **felder: str | None) -> Kandidat:
        """Füllt nur leere Felder eines Kandidaten; vorhandene Werte bleiben unverändert."""
        for feld, wert in felder.items():
            if feld not in ERGAENZBARE_FELDER or not wert:
                continue
            try:
                with self.conn:
                    self.conn.execute(
                        f"UPDATE kandidaten SET {feld} = ? WHERE id = ? AND ({feld} IS NULL OR {feld} = '')",
                        (wert, kid),
                    )
            except sqlite3.IntegrityError:
                pass  # Domain gehört schon einem anderen Kandidaten
        return self.get_kandidat(kid)

    def get_kandidat(self, kid: int) -> Kandidat | None:
        row = self.conn.execute("SELECT * FROM kandidaten WHERE id = ?", (kid,)).fetchone()
        return Kandidat(**dict(row)) if row else None

    def kandidat_nach_domain(self, domain: str) -> Kandidat | None:
        row = self.conn.execute("SELECT * FROM kandidaten WHERE domain = ?", (domain,)).fetchone()
        return Kandidat(**dict(row)) if row else None

    def kandidaten(
        self,
        bundesland: str,
        crawl_status: SchrittStatus | None = None,
        extract_status: SchrittStatus | None = None,
        nur_mit_website: bool = False,
    ) -> list[Kandidat]:
        sql = "SELECT * FROM kandidaten WHERE bundesland = ?"
        params: list = [bundesland]
        if crawl_status is not None:
            sql += " AND crawl_status = ?"
            params.append(crawl_status.value)
        if extract_status is not None:
            sql += " AND extract_status = ?"
            params.append(extract_status.value)
        if nur_mit_website:
            sql += " AND website IS NOT NULL"
        sql += " ORDER BY id"
        return [Kandidat(**dict(r)) for r in self.conn.execute(sql, params)]

    def set_crawl_status(self, kid: int, status: SchrittStatus, fehler: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE kandidaten SET crawl_status = ?, fehler = ? WHERE id = ?",
                (status.value, fehler, kid),
            )

    def set_extract_status(self, kid: int, status: SchrittStatus, fehler: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE kandidaten SET extract_status = ?, fehler = ? WHERE id = ?",
                (status.value, fehler, kid),
            )

    # ------------------------------------------------------------------ Funde

    def add_fund(self, f: Fund) -> bool:
        """Speichert einen Fund. False, wenn diese Quelle ihn schon geliefert hat."""
        d = f.model_dump(mode="json", exclude={"id"})
        with self.conn:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO funde ({', '.join(_FUND_FELDER)}) "
                f"VALUES ({', '.join('?' for _ in _FUND_FELDER)})",
                [d[k] for k in _FUND_FELDER],
            )
        return cur.rowcount == 1

    def hat_fund(self, bundesland: str, quelle: str, quell_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM funde WHERE bundesland = ? AND quelle = ? AND quell_id = ?",
            (bundesland, quelle, quell_id),
        ).fetchone()
        return row is not None

    def funde(self, bundesland: str, quelle: str | None = None) -> list[Fund]:
        sql = "SELECT * FROM funde WHERE bundesland = ?"
        params: list = [bundesland]
        if quelle:
            sql += " AND quelle = ?"
            params.append(quelle)
        return [Fund(**dict(r)) for r in self.conn.execute(sql + " ORDER BY id", params)]

    # ------------------------------------------------------------------ API-Cache

    def cache_get(self, adapter: str, schluessel: str) -> dict | None:
        row = self.conn.execute(
            "SELECT antwort FROM api_cache WHERE adapter = ? AND schluessel = ?", (adapter, schluessel)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, adapter: str, schluessel: str, antwort: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO api_cache (adapter, schluessel, antwort, zeitpunkt) VALUES (?,?,?,?)",
                (adapter, schluessel, json.dumps(antwort, ensure_ascii=False), _iso(datetime.now())),
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
                   (bundesland, behalten, dublette, kriterium, art, kanzleiname, details, zeitpunkt)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    d.bundesland, d.behalten, d.dublette, d.kriterium, d.art.value,
                    d.kanzleiname, d.details, _iso(d.zeitpunkt),
                ),
            )  # fmt: skip

    def dubletten(self, bundesland: str) -> list[Dublette]:
        rows = self.conn.execute(
            "SELECT * FROM dubletten WHERE bundesland = ? ORDER BY id", (bundesland,)
        )
        return [Dublette(**{k: v for k, v in dict(r).items() if k != "id"}) for r in rows]

    # ------------------------------------------------------------------ Lauf-Protokoll

    def log_lauf(self, e: LaufEintrag) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO lauf_protokoll
                   (lauf_id, schritt, bundesland, suchbegriff, adapter, ort, trefferzahl,
                    neue_kandidaten, anfragen, kosten, laufzeit_s, fehler, zeitpunkt)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    e.lauf_id, e.schritt, e.bundesland, e.suchbegriff, e.adapter, e.ort,
                    e.trefferzahl, e.neue_kandidaten, e.anfragen, e.kosten, e.laufzeit_s,
                    e.fehler, _iso(e.zeitpunkt),
                ),
            )  # fmt: skip

    def lauf_protokoll(self, bundesland: str) -> list[LaufEintrag]:
        rows = self.conn.execute(
            "SELECT * FROM lauf_protokoll WHERE bundesland = ? ORDER BY id", (bundesland,)
        )
        return [LaufEintrag(**{k: v for k, v in dict(r).items() if k != "id"}) for r in rows]

    def query_erledigt(
        self, bundesland: str, adapter: str, suchbegriff: str, schritt: str = "search"
    ) -> bool:
        """True, wenn diese Query mit diesem Adapter bereits erfolgreich lief (Wiederaufnahme)."""
        row = self.conn.execute(
            """SELECT 1 FROM lauf_protokoll
               WHERE bundesland = ? AND schritt = ? AND adapter = ? AND suchbegriff = ?
                 AND fehler IS NULL LIMIT 1""",
            (bundesland, schritt, adapter, suchbegriff),
        ).fetchone()
        return row is not None

    def anfragen_summe(self, bundesland: str, lauf_id: str | None = None) -> dict[str, tuple[int, float]]:
        """Kostenlog: (Anfragen, Kosten) je Adapter, optional für einen Lauf."""
        sql = """SELECT adapter, SUM(anfragen), SUM(kosten) FROM lauf_protokoll
                 WHERE bundesland = ? AND adapter IS NOT NULL"""
        params: list = [bundesland]
        if lauf_id:
            sql += " AND lauf_id = ?"
            params.append(lauf_id)
        rows = self.conn.execute(sql + " GROUP BY adapter ORDER BY adapter", params)
        return {r[0]: (r[1] or 0, round(r[2] or 0.0, 4)) for r in rows}

    # ------------------------------------------------------------------ Hilfen

    def import_kanzleien(self, kanzleien: Iterable[Kanzlei]) -> int:
        n = 0
        for k in kanzleien:
            self.upsert_kanzlei(k)
            n += 1
        return n
