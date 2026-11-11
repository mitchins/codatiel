"""SQLite store: files, units, and lexical FTS index. Stdlib sqlite3 only."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
  language TEXT NOT NULL, parse_status TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS units(
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
  sha256 TEXT NOT NULL, language TEXT NOT NULL, symbol TEXT NOT NULL,
  kind TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
  text TEXT NOT NULL, fingerprint TEXT NOT NULL,
  flags TEXT NOT NULL DEFAULT '[]', truncated_regions INTEGER NOT NULL DEFAULT 0,
  refs TEXT NOT NULL DEFAULT '[]'
);
CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(symbol, text);
CREATE TABLE IF NOT EXISTS descriptions(
  unit_id INTEGER PRIMARY KEY, text TEXT NOT NULL, provenance TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS desc_fts USING fts5(text);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    return conn


def add_file(conn: sqlite3.Connection, path: str, sha: str, nbytes: int, lang: str, status: str = "ok") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO files(path, sha256, bytes, language, parse_status) VALUES(?,?,?,?,?)",
        (path, sha, nbytes, lang, status),
    )


def add_unit(
    conn: sqlite3.Connection,
    path: str,
    sha: str,
    lang: str,
    symbol: str,
    kind: str,
    start: int,
    end: int,
    text: str,
    fingerprint: str,
    flags: str = "[]",
    truncated_regions: bool = False,
    refs: str = "[]",
) -> int:
    cur = conn.execute(
        "INSERT INTO units(path, sha256, language, symbol, kind, start_line, end_line, text, fingerprint,"
        " flags, truncated_regions, refs)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (path, sha, lang, symbol, kind, start, end, text, fingerprint, flags, int(truncated_regions), refs),
    )
    unit_id = cur.lastrowid
    assert unit_id is not None
    conn.execute("INSERT INTO units_fts(rowid, symbol, text) VALUES(?,?,?)", (unit_id, symbol, text))
    return unit_id


def add_description(conn: sqlite3.Connection, unit_id: int, text: str, provenance: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO descriptions(unit_id, text, provenance) VALUES(?,?,?)",
        (unit_id, text, provenance),
    )
    conn.execute("INSERT INTO desc_fts(rowid, text) VALUES(?,?)", (unit_id, text))
