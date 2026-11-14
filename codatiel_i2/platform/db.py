"""Connection helper for the platform database.

Same host, same local file as the v1 event store (additive tables), but
with the platform pragmas: WAL, foreign keys, synchronous=FULL. Write
transactions stay short and network I/O never happens inside one; the
busy timeout is explicit per caller because ingress must fail fast
while the worker may wait.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_S = 30.0
INGRESS_BUSY_TIMEOUT_S = 1.5


def connect(path: str | Path, *,
             busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), isolation_level=None,
                         timeout=busy_timeout_s)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA synchronous=FULL;")
    db.execute("PRAGMA foreign_keys=ON;")
    return db


@contextmanager
def tx(db: sqlite3.Connection):
    """One BEGIN IMMEDIATE / COMMIT / ROLLBACK unit of work."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise
