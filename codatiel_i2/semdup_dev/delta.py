"""PR delta classification on (base index, merged head view) unit maps.

Reuses the pure deterministic classifier `check.classify` (four-way
units + move detection + reexamine set). It does NOT use
`check.run_check` / `check.classify_pairs` (which rebuild both revision
indexes from scratch and drive the older audit inference pipeline) and
never `audit.*`.

Classification vocabulary (engine terms, preserved):
added / removed / changed / unchanged / moved / reexamine.
Only added + changed members can introduce or worsen duplication;
moved units whose content is unchanged elsewhere are not new code;
pre-existing and removed families stay counts, never findings.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codatiel_i2.semdup_vendored.semdup import check as CHK

# check.unit_map filters to these kinds; kept in sync deliberately.
UNIT_KINDS = ("function", "closure", "initializer")


def unit_map(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        out = {}
        for r in conn.execute(
                "SELECT path, symbol, kind, start_line, end_line, text, fingerprint,"
                " flags, refs FROM units WHERE kind IN ('function','closure','initializer')"):
            path, symbol, kind, sl, el, text, fp, flags, refs = r
            out[(path, symbol)] = {"path": path, "symbol": symbol, "kind": kind,
                                   "start_line": sl, "end_line": el, "text": text,
                                   "fingerprint": fp,
                                   "flags": json.loads(flags), "refs": json.loads(refs)}
        return out
    finally:
        conn.close()


def classify_delta(base_db: Path, head_db: Path) -> dict:
    """Classify (path, symbol) units between base index and head view."""
    base, head = unit_map(base_db), unit_map(head_db)
    units = CHK.classify(base, head)
    touched = units["added"] | units["changed"] | units["moved"] | units["reexamine"]
    return {"units": {k: sorted(v) for k, v in units.items()},
            "touched": sorted(touched),
            "introduced_or_changed": sorted((units["added"] | units["changed"]) - units["moved"])}


def delta_fingerprint(base_rev: str, head_rev: str, from_head: str,
                      touched: list) -> str:
    """Content fingerprint for the actually processed delta (dedup key)."""
    import hashlib
    body = json.dumps({"base": base_rev, "head": head_rev,
                       "from": from_head, "touched": [list(t) for t in touched]},
                      sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:16]
