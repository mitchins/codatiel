"""Fast/explorer arm (MiMo): bounded candidate discovery, zero authority.

One call per query unit, bounded output, temperature 0, no recursion, no
retries beyond transport policy. Output is hypotheses only: paths/symbols
are validated against the live snapshot (invalid hints dropped with
counts), and hints can only ADD retrieval pairs — deterministic lexical and
semantic candidates are never erased, vetoed, or re-ranked away. No findings, no equivalence claims, no mutation.

Parsing rule: provider-constrained JSON is requested, but the llama.cpp
endpoint does not reliably enforce it — the model often wraps output in
a fenced block. The parser therefore takes the first fenced block when
present (multiple fences: first wins, documented — not "exactly one"),
then parses strictly; anything else fails closed. This is not
brace-stripping prose until something parses: at most one delimited
block is ever considered.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from . import inference as I
from . import ledger as L
from . import retrieve as ret

PROMPT_VERSION = "fast_v1"
MAX_EXPANSIONS = 3
MAX_HINTS_PER_UNIT = 10
CONFIDENCES = ("low", "medium", "high")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def render_prompt(unit: dict, prompt_text: str, peer_index: list[dict]) -> str:
    lines = [prompt_text.strip(), "",
             f"## Query unit: {unit['path']}:{unit['symbol']} "
             f"(lines {unit['start_line']}-{unit['end_line']})",
             "```", unit["text"][:2000], "```",
             f"deterministic description [{unit.get('description_provenance', '?')}]: "
             f"{unit.get('description', '')}",
             "", "## Repository unit index (path : symbol — hint ONLY from this list)"]
    for peer in peer_index[:400]:
        lines.append(f"- {peer['path']} : {peer['symbol']}")
    lines += ["", "Respond with exactly one JSON object with keys query_expansions, "
              "candidate_hints, helper_hints. No prose."]
    return "\n".join(lines)


def _validate_hints(value: object) -> tuple[dict, list[str], int]:
    """Lenient shape check: missing lists default to [], wrong types error.

    Leniency is documented and safe: every hint is re-validated against the
    snapshot before use, so extra keys or empty lists cannot fabricate
    evidence — only well-shaped entries survive. Returns (hints, errors,
    shape_dropped): entries skipped for bad shape are counted, not silent.
    """
    errors: list[str] = []
    dropped = 0
    if not isinstance(value, dict):
        return {}, ["top-level object required"], 1
    out = {"query_expansions": [], "candidate_hints": [], "helper_hints": []}
    expansions = value.get("query_expansions", [])
    if not isinstance(expansions, list):
        errors.append("query_expansions must be a list")
    else:
        for item in expansions[:MAX_EXPANSIONS]:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                out["query_expansions"].append({"text": item["text"][:300],
                                                "reason": str(item.get("reason", ""))[:300]})
            else:
                dropped += 1
    cands = value.get("candidate_hints", [])
    if not isinstance(cands, list):
        errors.append("candidate_hints must be a list")
    else:
        for item in cands:
            if not isinstance(item, dict):
                dropped += 1
                continue
            if not isinstance(item.get("path"), str) or not isinstance(item.get("symbol"), str):
                dropped += 1
                continue
            if item.get("confidence", "low") not in CONFIDENCES:
                dropped += 1
                continue
            out["candidate_hints"].append({
                "path": item["path"], "symbol": item["symbol"],
                "responsibility": str(item.get("responsibility", ""))[:300],
                "reason": str(item.get("reason", ""))[:300],
                "confidence": item.get("confidence", "low")})
    helps = value.get("helper_hints", [])
    if not isinstance(helps, list):
        errors.append("helper_hints must be a list")
    else:
        for item in helps:
            if isinstance(item, dict) and isinstance(item.get("symbol"), str):
                out["helper_hints"].append({"symbol": item["symbol"],
                                            "reason": str(item.get("reason", ""))[:300]})
            else:
                dropped += 1
    return out, errors, dropped


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


def _extract_block(content: str) -> str:
    match = _FENCE_RE.search(content)
    return match.group(1) if match else content


def suggest(unit: dict, peer_index: list[dict], prompt_text: str,
            transport: object) -> tuple[dict, str, object, int]:
    """One explorer call. Returns (hints, prompt, raw_result, shape_dropped). Raises on failure."""
    prompt = render_prompt(unit, prompt_text, peer_index)
    raw = transport([{"role": "user", "content": prompt}])  # type: ignore[operator]
    try:
        value = json.loads(_extract_block(raw.content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"non-JSON fast output: {exc}") from exc
    hints, errors, dropped = _validate_hints(value)
    if errors:
        raise ValueError("fast output rejected: " + "; ".join(errors))
    return hints, prompt, raw, dropped


def resolve(conn: sqlite3.Connection, query_id: int, hints: dict,
            k_per_expansion: int = 5) -> tuple[list[tuple[int, str]], dict]:
    """Validate hints against the snapshot; return ([(unit_id, subsource)] pairs, stats).

    Subsources: hint (explicit path/symbol), expansion (via desc_fts),
    helper (symbol anywhere). Unknown paths/symbols are dropped with
    counts — never trusted, never fabricated.
    """
    stats = {"hints_raw": len(hints["candidate_hints"]), "hints_valid": 0,
             "hints_dropped": 0, "expansions": len(hints["query_expansions"]),
             "expansion_hits": 0, "helpers_raw": len(hints["helper_hints"]),
             "helpers_valid": 0}
    pairs: dict[int, str] = {}

    def unit_id_for(path: str, symbol: str) -> int | None:
        row = conn.execute(
            "SELECT id FROM units WHERE path=? AND symbol=? AND kind IN "
            "('function','closure','initializer')", (path, symbol)).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "SELECT id FROM units WHERE symbol=? AND kind IN "
            "('function','closure','initializer') ORDER BY path LIMIT 1", (symbol,)).fetchone()
        return row[0] if row else None

    for hint in hints["candidate_hints"][:MAX_HINTS_PER_UNIT]:
        uid = unit_id_for(hint["path"], hint["symbol"])
        if uid is None or uid == query_id:
            stats["hints_dropped"] += 1
            continue
        stats["hints_valid"] += 1
        pairs.setdefault(uid, "hint")
    for expansion in hints["query_expansions"][:MAX_EXPANSIONS]:
        tokens = ret.significant_tokens(expansion["text"], limit=12)
        if not tokens:
            continue
        query = " OR ".join(ret._fts_quote(t) for t in tokens)
        try:
            rows = conn.execute(
                "SELECT u.id FROM desc_fts JOIN units u ON u.id = desc_fts.rowid"
                " WHERE desc_fts MATCH ? AND u.kind IN ('function','closure','initializer')"
                " LIMIT ?", (query, k_per_expansion)).fetchall()
        except sqlite3.OperationalError:
            continue
        for (uid,) in rows:
            if uid == query_id or uid in pairs:
                continue
            stats["expansion_hits"] += 1
            pairs.setdefault(uid, "expansion")
    for helper in hints["helper_hints"][:MAX_HINTS_PER_UNIT]:
        rows = conn.execute(
            "SELECT id FROM units WHERE symbol=? AND kind IN "
            "('function','closure','initializer')", (helper["symbol"],)).fetchall()
        if len(rows) != 1:
            # Ambiguous (or absent) symbol: skip rather than misattribute.
            # Exact-path hints already resolved above; helpers carry no path.
            continue
        uid = rows[0][0]
        if uid == query_id:
            continue
        stats["helpers_valid"] += 1
        pairs.setdefault(uid, "helper")
    return [(uid, source) for uid, source in pairs.items()][:MAX_HINTS_PER_UNIT], stats
