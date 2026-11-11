"""Exact target resolution against the merged head view (GAP-4 consumer).

Every PR target is resolved to an exact `(path, symbol)` row before any
inference runs:

- exact match -> proceed with that identity;
- basename match in several directories (same symbol, same basename) ->
  proceed ONLY with the exact head path; the engine fix guarantees the
  exact row wins;
- no exact row, or several exact rows (!) -> defer with reason, never
  an arbitrary pick;
- target not a function/closure/initializer (e.g. class-level change
  with no indexed unit) -> not-examinable, recorded, never a finding.

This is what acceptance #7 exercises: two symbols with identical names
in different directories resolve to the exact head identities.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def resolve_target(conn: sqlite3.Connection, path: str,
                   symbol: str) -> dict:
    exact = conn.execute(
        "SELECT id, path, symbol, start_line, end_line FROM units "
        "WHERE path=? AND symbol=?", (path, symbol)).fetchall()
    if len(exact) == 1:
        r = exact[0]
        return {"ok": True, "id": r[0], "path": r[1], "symbol": r[2],
                "start_line": r[3], "end_line": r[4]}
    if len(exact) > 1:
        return {"ok": False, "reason": "duplicate exact identity in view",
                "detail": f"{len(exact)} rows for {path}::{symbol}"}
    base = path.split("/")[-1]
    near = conn.execute(
        "SELECT id, path, symbol FROM units WHERE path LIKE ? AND symbol=?",
        (f"%{base}", symbol)).fetchall()
    if not near:
        return {"ok": False, "reason": "target not in head view",
                "detail": f"{path}::{symbol}"}
    return {"ok": False, "reason": "target path not in head view",
            "detail": f"{path}::{symbol} matches {[r[1] for r in near][:5]} "
                      f"by basename only; refusing basename fallback"}


def ambiguity_scan(conn: sqlite3.Connection) -> list[dict]:
    """View-wide check: basename/symbol pairs with >1 exact path.

    Informational for reports; resolution itself is exact-first by
    construction, so ambiguous pairs do not block processing.
    """
    rows = conn.execute(
        "SELECT symbol, path FROM units WHERE kind IN "
        "('function','closure','initializer')").fetchall()
    from collections import defaultdict
    groups: dict = defaultdict(set)
    for symbol, path in rows:
        groups[(path.split("/")[-1], symbol)].add(path)
    return [{"basename": b, "symbol": s, "paths": sorted(p)}
            for (b, s), p in groups.items() if len(p) > 1]
