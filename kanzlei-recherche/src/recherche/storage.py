"""SQLite-Speicher: Kandidaten, Rohseiten-Cache, Kanzleien und Protokolle.

Jeder Schritt der Pipeline liest und schreibt hier seinen Fortschritt, damit ein
abgebrochener Lauf beim nächsten Aufruf dort weitermacht, wo er aufgehört hat.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from .groesse import neu_einordnen
from .models import (
    AUTO_STATUS,
    Ansprechpartner,
    Dublette,
    Fund,
    Kandidat,
    Kanzlei,
    LaufEintrag,
    NichtGefunden,
    SchrittStatus,
    Seite,
    Seitentyp,
)
from .normalize import normalize_telefon

SCHEMA_VERSION = 4

# Schema v2 eingefroren – Grundlage der Migration v1 → v2
_SCHEMA_V2 = """
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

def _schema_v3() -> str:
    s = _SCHEMA_V2
    s = s.replace(
        """    text            TEXT,
    abgerufen_am    TEXT NOT NULL
);""",
        """    text            TEXT,
    abgerufen_am    TEXT NOT NULL,
    html_gz         BLOB,
    final_url       TEXT
);""",
    )
    a, e = s.index("CREATE TABLE IF NOT EXISTS kanzleien ("), s.index("CREATE TABLE IF NOT EXISTS nicht_gefunden (")
    s = s[:a] + _KANZLEIEN_V3 + s[e:]
    a, e = s.index("CREATE TABLE IF NOT EXISTS nicht_gefunden ("), s.index("CREATE TABLE IF NOT EXISTS dubletten (")
    return s[:a] + _NICHT_GEFUNDEN_V3 + _KANDIDAT_SEITEN + s[e:]


_KANZLEIEN_V3 = """CREATE TABLE IF NOT EXISTS kanzleien (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kandidat_id         INTEGER UNIQUE,
    domain              TEXT UNIQUE,
    lauf_bundesland     TEXT NOT NULL,
    kanzleiname         TEXT,
    rechtsform          TEXT,
    strasse             TEXT,
    plz                 TEXT,
    ort                 TEXT,
    bundesland          TEXT,
    plz_verifiziert     INTEGER,
    telefon             TEXT,
    email               TEXT,
    website             TEXT,
    website_vorhanden   INTEGER NOT NULL DEFAULT 1,
    ansprechpartner     TEXT NOT NULL DEFAULT '[]',
    berufstraeger_anzahl INTEGER,
    teamgroesse         INTEGER,
    teamgroesse_klasse  TEXT,
    teamgroesse_basis   TEXT,
    konfidenz           TEXT NOT NULL,
    abweichung          TEXT,
    quell_urls          TEXT NOT NULL DEFAULT '[]',
    abrufdatum          TEXT,
    status              TEXT NOT NULL DEFAULT 'neu'
);
CREATE INDEX IF NOT EXISTS ix_kanzleien_bl ON kanzleien (lauf_bundesland);

"""

_NICHT_GEFUNDEN_V3 = """CREATE TABLE IF NOT EXISTS nicht_gefunden (
    domain          TEXT PRIMARY KEY,
    bundesland      TEXT NOT NULL,
    grund           TEXT NOT NULL,
    url             TEXT,
    zeitpunkt       TEXT NOT NULL,
    kandidat_id     INTEGER,
    name            TEXT,
    strasse         TEXT,
    plz             TEXT,
    ort             TEXT,
    telefon         TEXT,
    website         TEXT
);

"""

_KANDIDAT_SEITEN = """CREATE TABLE IF NOT EXISTS kandidat_seiten (
    kandidat_id     INTEGER NOT NULL REFERENCES kandidaten (id) ON DELETE CASCADE,
    seitentyp       TEXT NOT NULL,
    url             TEXT NOT NULL,
    PRIMARY KEY (kandidat_id, seitentyp)
);

"""

# Schema v4: Adresszusatz („Haus 8 Süd", „2. OG") bei den Kanzleien
SCHEMA = _schema_v3().replace(
    "    strasse             TEXT,\n    plz                 TEXT,",
    "    strasse             TEXT,\n    adresszusatz        TEXT,\n    plz                 TEXT,",
)
assert "adresszusatz" in SCHEMA

# Schema v1 → v2: Kandidaten mit eigener ID (Domain optional), Tabelle funde,
# api_cache, Dubletten mit Art, Kostenlog im Lauf-Protokoll.
MIGRATION_V1_V2 = """
ALTER TABLE kandidaten RENAME TO kandidaten_v1;
ALTER TABLE dubletten RENAME TO dubletten_v1;
DROP INDEX IF EXISTS ix_kandidaten_bl;
ALTER TABLE lauf_protokoll RENAME COLUMN neue_domains TO neue_kandidaten;
ALTER TABLE lauf_protokoll ADD COLUMN anfragen INTEGER NOT NULL DEFAULT 0;
ALTER TABLE lauf_protokoll ADD COLUMN kosten REAL NOT NULL DEFAULT 0;
""" + _SCHEMA_V2 + """
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

# Schema v2 → v3: Rohes HTML im Seiten-Cache, Seiten je Kandidat, Kanzleien je Kandidat
# (Domain optional) mit Abweichung/Berufsträgern, Discovery-Daten bei „Nicht gefunden".
_KANZLEI_SPALTEN_V2 = (
    "domain, lauf_bundesland, kanzleiname, rechtsform, strasse, plz, ort, bundesland, telefon, email, "
    "website, ansprechpartner, teamgroesse, teamgroesse_klasse, teamgroesse_basis, konfidenz, "
    "quell_urls, abrufdatum, status"
)
MIGRATION_V2_V3 = (
    """
ALTER TABLE seiten ADD COLUMN html_gz BLOB;
ALTER TABLE seiten ADD COLUMN final_url TEXT;
ALTER TABLE nicht_gefunden ADD COLUMN kandidat_id INTEGER;
ALTER TABLE nicht_gefunden ADD COLUMN name TEXT;
ALTER TABLE nicht_gefunden ADD COLUMN strasse TEXT;
ALTER TABLE nicht_gefunden ADD COLUMN plz TEXT;
ALTER TABLE nicht_gefunden ADD COLUMN ort TEXT;
ALTER TABLE nicht_gefunden ADD COLUMN telefon TEXT;
ALTER TABLE nicht_gefunden ADD COLUMN website TEXT;
ALTER TABLE kanzleien RENAME TO kanzleien_v2;
DROP INDEX IF EXISTS ix_kanzleien_bl;
"""
    + _KANZLEIEN_V3
    + _KANDIDAT_SEITEN
    + f"""
INSERT INTO kanzleien ({_KANZLEI_SPALTEN_V2})
    SELECT {_KANZLEI_SPALTEN_V2} FROM kanzleien_v2;
DROP TABLE kanzleien_v2;
"""
)

# Version → Migrationsskript auf Version + 1
MIGRATION_V3_V4 = "ALTER TABLE kanzleien ADD COLUMN adresszusatz TEXT;"

MIGRATIONEN = {1: MIGRATION_V1_V2, 2: MIGRATION_V2_V3, 3: MIGRATION_V3_V4}

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
    "kandidat_id", "domain", "lauf_bundesland", "kanzleiname", "rechtsform", "strasse", "adresszusatz", "plz",
    "ort", "bundesland", "plz_verifiziert", "telefon", "email", "website", "website_vorhanden",
    "ansprechpartner", "berufstraeger_anzahl", "teamgroesse", "teamgroesse_klasse",
    "teamgroesse_basis", "konfidenz", "abweichung", "quell_urls", "abrufdatum", "status",
]  # fmt: skip
_NG_FELDER = [
    "domain", "bundesland", "grund", "url", "zeitpunkt", "kandidat_id", "name", "strasse", "plz",
    "ort", "telefon", "website",
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
        if version == 0:
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()
            return
        while version < SCHEMA_VERSION:
            # executescript committet selbst; jede Migration läuft in einer expliziten Transaktion
            try:
                self.conn.executescript(
                    f"BEGIN;\n{MIGRATIONEN[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
                )
            except sqlite3.Error:
                self.conn.rollback()
                raise
            version += 1

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

    def fehler_zuruecksetzen(self, bundesland: str, schritt: str, ausser: str | None = None) -> int:
        """Setzt Kandidaten mit Status „fehler" im Schritt crawl/extract wieder auf „offen" –
        außer denen, deren Fehler `ausser` enthält (z. B. tote Domains)."""
        spalte = {"crawl": "crawl_status", "extract": "extract_status"}[schritt]
        bedingung, werte = "", [bundesland]
        if ausser:
            bedingung, werte = " AND (fehler IS NULL OR instr(fehler, ?) = 0)", [bundesland, ausser]
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE kandidaten SET {spalte} = 'offen', fehler = NULL WHERE bundesland = ? AND {spalte} = 'fehler'"
                + bedingung, werte,
            )
        return cur.rowcount

    def extract_alle_offen(self, bundesland: str) -> int:
        """extract --alle: alle bereits extrahierten (oder fehlgeschlagenen) Kandidaten wieder auf „offen".
        Die Kanzlei-Zeilen bleiben bis zur Neu-Extraktion stehen; gepflegte Status bleiben erhalten."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE kandidaten SET extract_status = 'offen', fehler = NULL "
                "WHERE bundesland = ? AND extract_status != 'offen'", (bundesland,))
        return cur.rowcount

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

    def funde_kandidat(self, kid: int) -> list[Fund]:
        rows = self.conn.execute("SELECT * FROM funde WHERE kandidat_id = ? ORDER BY id", (kid,))
        return [Fund(**dict(r)) for r in rows]

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

    def save_seite(self, s: Seite, ersetzen: bool = False) -> bool:
        """Speichert eine Seite. Bereits gecachte URLs werden nie überschrieben – außer mit `ersetzen`
        (Playwright-Ergebnis für eine gecachte 403- oder Leerseite bei crawl --wiederholen)."""
        html_gz = gzip.compress(s.html.encode("utf-8")) if s.html is not None else None
        with self.conn:
            cur = self.conn.execute(
                f"""INSERT OR {'REPLACE' if ersetzen else 'IGNORE'} INTO seiten
                   (url, domain, seitentyp, http_status, text, abgerufen_am, html_gz, final_url)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (s.url, s.domain, s.seitentyp.value, s.http_status, s.text, _iso(s.abgerufen_am),
                 html_gz, s.final_url),
            )  # fmt: skip
        return cur.rowcount == 1

    @staticmethod
    def _seite_aus_row(row: sqlite3.Row) -> Seite:
        d = dict(row)
        roh = d.pop("html_gz", None)
        d["html"] = gzip.decompress(roh).decode("utf-8") if roh else None
        return Seite(**d)

    def get_seite(self, url: str) -> Seite | None:
        row = self.conn.execute("SELECT * FROM seiten WHERE url = ?", (url,)).fetchone()
        return self._seite_aus_row(row) if row else None

    def seiten(self, domain: str) -> list[Seite]:
        rows = self.conn.execute("SELECT * FROM seiten WHERE domain = ? ORDER BY url", (domain,))
        return [self._seite_aus_row(r) for r in rows]

    def setze_kandidat_seite(self, kid: int, typ: Seitentyp, url: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO kandidat_seiten (kandidat_id, seitentyp, url) VALUES (?,?,?)",
                (kid, typ.value, url),
            )

    def kandidat_seiten(self, kid: int) -> dict[Seitentyp, Seite]:
        """Gecachte Seiten eines Kandidaten je Typ (Start, Impressum, Kontakt, Team)."""
        rows = self.conn.execute(
            """SELECT ks.seitentyp AS typ, s.* FROM kandidat_seiten ks JOIN seiten s ON s.url = ks.url
               WHERE ks.kandidat_id = ?""",
            (kid,),
        )
        ergebnis = {}
        for r in rows:
            d = dict(r)
            typ = Seitentyp(d.pop("typ"))
            ergebnis[typ] = self._seite_aus_row(d)
        return ergebnis

    # ------------------------------------------------------------------ Kanzleien

    def upsert_kanzlei(self, k: Kanzlei) -> int:
        """Legt eine Kanzlei an oder aktualisiert sie (Schlüssel: Kandidat, sonst Domain).

        Der Status (Excel-Dropdown) einer bestehenden Kanzlei bleibt erhalten, wenn er von Hand
        gepflegt ist – eine erneute Extraktion setzt also keine gepflegten Status zurück. Nur die
        automatischen Werte „neu", „keine Kanzlei" und „außerhalb Bundesland" werden ersetzt.
        """
        row = None
        if k.id is not None:  # geladene Zeile (z. B. kanzlei-pruefung) – auch ohne Kandidat und Domain
            row = self.conn.execute("SELECT id, status FROM kanzleien WHERE id = ?", (k.id,)).fetchone()
        if row is None and k.kandidat_id is not None:
            row = self.conn.execute("SELECT id, status FROM kanzleien WHERE kandidat_id = ?", (k.kandidat_id,)).fetchone()
        if row is None and k.domain:
            row = self.conn.execute("SELECT id, status FROM kanzleien WHERE domain = ?", (k.domain,)).fetchone()
        werte = dict(zip(_KANZLEI_FELDER, self._kanzlei_werte(k), strict=True))
        with self.conn:
            if row is None:
                cur = self.conn.execute(
                    f"INSERT INTO kanzleien ({', '.join(_KANZLEI_FELDER)}) "
                    f"VALUES ({', '.join('?' for _ in _KANZLEI_FELDER)})",
                    list(werte.values()),
                )
                return cur.lastrowid
            if row["status"] not in AUTO_STATUS and k.status in AUTO_STATUS:
                werte["status"] = row["status"]
            self.conn.execute(
                f"UPDATE kanzleien SET {', '.join(f'{f} = ?' for f in werte)} WHERE id = ?",
                [*werte.values(), row["id"]],
            )
            return row["id"]

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
        # Telefon nach der aktuellen Normalisierung (ältere Zeilen: „+494513888100" → „+49 451 3888100")
        if d.get("telefon"):
            d["telefon"] = normalize_telefon(d["telefon"]) or d["telefon"]
        # Größenklasse nach der aktuellen Einteilung (settings.yaml) – auch für ältere Zeilen
        d["teamgroesse_klasse"], hinweis = neu_einordnen(d.get("teamgroesse"), d.get("teamgroesse_klasse"),
                                                         d.get("teamgroesse_basis"))
        if hinweis:
            d["abweichung"] = "; ".join(x for x in (d.get("abweichung"), hinweis) if x)
        return Kanzlei(**d)

    def get_kanzlei(self, domain: str) -> Kanzlei | None:
        row = self.conn.execute("SELECT * FROM kanzleien WHERE domain = ?", (domain,)).fetchone()
        return self._kanzlei_aus_row(row) if row else None

    def kanzlei_fuer_kandidat(self, kid: int) -> Kanzlei | None:
        row = self.conn.execute("SELECT * FROM kanzleien WHERE kandidat_id = ?", (kid,)).fetchone()
        return self._kanzlei_aus_row(row) if row else None

    def kanzleien(self, bundesland: str) -> list[Kanzlei]:
        rows = self.conn.execute(
            "SELECT * FROM kanzleien WHERE lauf_bundesland = ? ORDER BY plz, kanzleiname, domain",
            (bundesland,),
        )
        return [self._kanzlei_aus_row(r) for r in rows]

    def alle_kanzleien(self) -> list[Kanzlei]:
        """Alle Zeilen aller Läufe – fürs Lieferformat je PLZ-Bundesland."""
        rows = self.conn.execute("SELECT * FROM kanzleien ORDER BY plz, kanzleiname, domain")
        return [self._kanzlei_aus_row(r) for r in rows]

    def laeufe(self) -> list[str]:
        """Bundesland-Kürzel aller Läufe mit Kandidaten oder Kanzleien."""
        rows = self.conn.execute("SELECT bundesland FROM kandidaten UNION SELECT lauf_bundesland FROM kanzleien")
        return sorted({r[0] for r in rows if r[0]})

    def delete_kanzlei(self, domain: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM kanzleien WHERE domain = ?", (domain,))

    # ------------------------------------------------------------------ Nicht gefunden

    def add_nicht_gefunden(self, n: NichtGefunden) -> None:
        """Ein Eintrag je Domain; ein erneuter Fehler ersetzt den alten Grund."""
        d = n.model_dump(mode="json")
        with self.conn:
            self.conn.execute(
                f"INSERT OR REPLACE INTO nicht_gefunden ({', '.join(_NG_FELDER)}) "
                f"VALUES ({', '.join('?' for _ in _NG_FELDER)})",
                [d[f] for f in _NG_FELDER],
            )

    def entferne_nicht_gefunden(self, domain: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM nicht_gefunden WHERE domain = ?", (domain,))

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

    def loesche_dubletten(self, bundesland: str, kriterium: str) -> int:
        with self.conn:
            return self.conn.execute(
                "DELETE FROM dubletten WHERE bundesland = ? AND kriterium = ?", (bundesland, kriterium)).rowcount

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

    def query_treffer(self, bundesland: str, adapter: str, suchbegriff: str, schritt: str = "search") -> int:
        """Trefferzahl einer erledigten Query (größte, falls mehrfach) – für die Stadtteil-Nachsuche."""
        row = self.conn.execute(
            """SELECT MAX(trefferzahl) FROM lauf_protokoll
               WHERE bundesland = ? AND schritt = ? AND adapter = ? AND suchbegriff = ? AND fehler IS NULL""",
            (bundesland, schritt, adapter, suchbegriff),
        ).fetchone()
        return (row[0] or 0) if row else 0

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
