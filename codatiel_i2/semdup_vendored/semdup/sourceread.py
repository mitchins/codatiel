"""Bounded source access: pure functions over (root, conn).

read_symbol: exact unit body by path::symbol (or bare symbol when
unique). read_context: bounded surroundings (caps 40/40).
references: bounded caller list from the recorded call graph.
Every call records its exact range into the provided ledger list;
whole-file reads require explicit justification flag.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import spans as SP

CONTEXT_CAP = 40


def _resolve(conn: sqlite3.Connection, symbol_id: str) -> tuple[str, str, int, int]:
    p, _, sym = symbol_id.partition("::")
    sym = sym.strip()
    row = None
    if p and sym:
        row = conn.execute("SELECT path, symbol, start_line, end_line FROM units "
                           "WHERE path=? AND symbol=?", (p, sym)).fetchone()
        if row is None:
            row = conn.execute("SELECT path, symbol, start_line, end_line FROM units "
                               "WHERE path LIKE ? AND symbol=?",
                               (f"%{p.split('/')[-1]}", sym)).fetchone()
    if row is None and sym:
        rows = conn.execute("SELECT path, symbol, start_line, end_line FROM units "
                            "WHERE symbol=?", (sym,)).fetchall()
        if len(rows) == 1:
            row = rows[0]
    if row is None:
        raise LookupError(f"symbol not in index: {symbol_id[:120]}")
    return row[0], row[1], int(row[2]), int(row[3])


def read_symbol(root: Path, conn: sqlite3.Connection, symbol_id: str,
                ledger: list | None = None) -> dict:
    """Exact unit body. Returns {id, path, start, end, text}."""
    path, symbol, start, end = _resolve(conn, symbol_id)
    full = SP.safe_path(root, path)
    lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1)
                     if start <= i <= end)
    rec = {"id": f"{path}::{symbol}", "path": path, "start": start,
           "end": end, "text": body,
           "chars": len(body), "unique_lines": max(0, end - start + 1)}
    if ledger is not None:
        ledger.append({"tool": "read_symbol", "id": rec["id"],
                       "start": start, "end": end})
    return rec


def read_context(root: Path, conn: sqlite3.Connection, symbol_id: str,
                 before: int = 10, after: int = 10,
                 ledger: list | None = None) -> dict:
    """Bounded surroundings. Caps enforced (40/40)."""
    before, after = max(0, min(int(before), CONTEXT_CAP)), max(0, min(int(after), CONTEXT_CAP))
    path, symbol, start, end = _resolve(conn, symbol_id)
    full = SP.safe_path(root, path)
    lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
    lo, hi = max(1, start - before), min(len(lines), end + after)
    body = "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1) if lo <= i <= hi)
    rec = {"id": f"{path}::{symbol}", "path": path, "start": lo, "end": hi,
           "unit_start": start, "unit_end": end, "text": body,
           "chars": len(body), "unique_lines": max(0, hi - lo + 1)}
    if ledger is not None:
        ledger.append({"tool": "read_context", "id": rec["id"],
                       "start": lo, "end": hi})
    return rec


def read_file_range(root: Path, relpath: str, start: int, end: int,
                    justified: bool = False,
                    ledger: list | None = None) -> dict:
    """Whole-file/range reads: only when explicitly justified."""
    if not justified and not (start and end):
        raise ValueError("unjustified whole-file read refused: use read_symbol")
    full = SP.safe_path(root, relpath)
    lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
    lo, hi = (1, len(lines)) if not (start and end) else (start, end)
    body = "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1) if lo <= i <= hi)
    rec = {"id": relpath, "path": relpath, "start": lo, "end": hi,
           "text": body, "chars": len(body),
           "unique_lines": max(0, hi - lo + 1), "justified": justified}
    if ledger is not None:
        ledger.append({"tool": "read_source", "id": relpath,
                       "start": lo, "end": hi})
    return rec


def references(conn: sqlite3.Connection, symbol: str, limit: int = 30) -> dict:
    """Bounded caller list from the recorded call graph."""
    bare = symbol.split("#")[-1].split("@")[0].split("::")[-1]
    callers: set[str] = set()
    for (cpath, csym, refs) in conn.execute("SELECT path, symbol, refs FROM units"):
        try:
            names = [r if isinstance(r, str) else r.get("name", "")
                     for r in json.loads(refs or "[]")]
        except (ValueError, TypeError):
            continue
        if bare in names:
            callers.add(f"{cpath}::{csym}")
    out = sorted(callers)[:limit]
    return {"symbol": symbol, "callers": out, "truncated": len(callers) > limit}


def ledger_lines(records: list[dict]) -> dict:
    """Unique/repeated line accounting over ledger records."""
    seen: dict[str, set] = {}
    repeated = 0
    for r in records:
        key = r.get("id", "?")
        rng = set(range(int(r.get("start", 1)), int(r.get("end", 0)) + 1))
        prev = seen.setdefault(key, set())
        repeated += len(rng & prev)
        prev.update(rng)
    return {"n_reads": len(records),
            "unique_source_lines": sum(len(v) for v in seen.values()),
            "repeated_source_lines": repeated}
