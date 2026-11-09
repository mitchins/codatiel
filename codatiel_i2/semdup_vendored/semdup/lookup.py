"""Lookup: intent -> candidates + suitability limits (report-only).

Searches indexed units (descriptions with generic expansion + code FTS)
for an existing implementation matching a stated intent. A
no-suitable-result outcome is valid and carries the search coverage that
justifies it — never an instruction to reuse the nearest neighbor.
Suitability limits are fixed generic checks, not model judgments.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import expand as lex
from . import retrieve as ret

GENERIC_LIMITS = [
    "verify behavior on your inputs, including edges and errors",
    "check mutation, effects, and ordering against your call site",
    "check error contract and cancellation/isolation context",
    "confirm the owning module is an allowed dependency direction",
]


def lookup(db_path: Path, intent: str, k: int = 10) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        tokens = ret.significant_tokens(intent, limit=12)
        expanded = lex.expand(tokens)
        seen: dict[int, dict] = {}
        errors: list[str] = []
        searched = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        for table, channel in (("desc_fts", "semantic"), ("units_fts", "lexical")):
            if not expanded:
                continue
            query = " OR ".join(ret._fts_quote(t) for t in expanded)
            try:
                if table == "desc_fts":
                    rows = conn.execute(
                        "SELECT u.id, u.path, u.symbol, u.kind, u.start_line, u.end_line"
                        " FROM desc_fts JOIN units u ON u.id = desc_fts.rowid"
                        " WHERE desc_fts MATCH ? AND u.kind IN ('function','closure','initializer')"
                        " LIMIT ?", (query, k)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT u.id, u.path, u.symbol, u.kind, u.start_line, u.end_line"
                        " FROM units_fts JOIN units u ON u.id = units_fts.rowid"
                        " WHERE units_fts MATCH ? AND u.kind IN ('function','closure','initializer')"
                        " LIMIT ?", (query, k)).fetchall()
            except sqlite3.OperationalError as exc:
                errors.append(f"{channel}: {exc}")
                continue
            for uid, path, symbol, kind, sl, el in rows:
                slot = seen.setdefault(uid, {"path": path, "symbol": symbol, "kind": kind,
                                             "start_line": sl, "end_line": el, "channels": []})
                if channel not in slot["channels"]:
                    slot["channels"].append(channel)
        candidates = sorted(seen.values(), key=lambda c: (len(c["channels"]), c["symbol"]), reverse=True)[:k]
        coverage = {"units_searched": searched, "channels": ["semantic", "lexical"],
                    "intent_tokens": len(tokens), "errors": errors}
        if not candidates:
            return {"candidates": [], "suitability_limits": GENERIC_LIMITS,
                    "coverage": coverage, "outcome": "no-suitable-result"}
        for cand in candidates:
            cand["suitability_limits"] = GENERIC_LIMITS
        return {"candidates": candidates, "suitability_limits": GENERIC_LIMITS,
                "coverage": coverage, "outcome": "candidates"}
    finally:
        conn.close()
