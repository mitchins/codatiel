"""Index store (SQLite open/ensure)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from codatiel_i2.index.schema import SCHEMA_SQL


def open_index(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
