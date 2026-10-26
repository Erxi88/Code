-- Schema v1 (Stand Stufe 1) für den Migrationstest
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
