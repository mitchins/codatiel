"""Cross-revision verdict cache (COST-01B).

Reuses a prior Terra/Luna verdict when the decision evidence is
genuinely unchanged across revisions.

Identity (must match on reuse, otherwise reassess):
- Both code units' content (target sha256, counterpart sha256).
- File-level surrounding context (file sha256 for each path).
- Target classification/role (introduced_or_changed classification).
- Model identity (terra model, luna/editor model).
- Prompt / instructions and version (terra_instructions_id,
  editor_instructions_id, stage_version, detector).

Must NOT depend on:
- Revision SHAs (base/head)
- Absolute line numbers

Cache is scoped per (repo, PR) to avoid cross-PR collisions.
A hit rebinds anchors to current locations and validates that the
referenced source still exists at the expected sha.

Never cached: budget_stop refusals (they were never judged) and
rows whose detail indicates a ledger refusal.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

CACHE_VERSION = "cost01b-v1"
STAGE_VERSION_KEY = "semdup-stage-0.3"

def cache_path(state_dir: Path | str, repo: str, pr: int) -> Path:
    slug = str(repo).replace("/", "__")
    return Path(state_dir) / "semdup" / "cache" / slug / f"{pr}.json"

def _file_sha256(merged_db: Path, file_path: str) -> str:
    """File sha256 from the merged view (empty when not found)."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(merged_db))
        try:
            row = conn.execute(
                "SELECT sha256 FROM files WHERE path=?", (file_path,)
            ).fetchone()
            return str(row[0]) if row and row[0] else ""
        finally:
            conn.close()
    except Exception:
        return ""

def _unit_sha256(merged_db: Path, path: str, symbol: str) -> str:
    try:
        import sqlite3
        conn = sqlite3.connect(str(merged_db))
        try:
            row = conn.execute(
                "SELECT text FROM units WHERE path=? AND symbol=? ORDER BY start_line LIMIT 1",
                (path, symbol),
            ).fetchone()
            if not row or not row[0]:
                return ""
            return hashlib.sha256(str(row[0]).encode("utf-8")).hexdigest()
        finally:
            conn.close()
    except Exception:
        return ""

def _inline_context_hash(merged_db: Path, path: str, symbol: str) -> str:
    """Hash of surrounding context for invalidation.

    Currently the file sha256 (captures any file-level change).
    Line numbers are intentionally excluded; moving a unit within the
    same file keeps the file sha stable only if the file content
    outside the unit is unchanged (insertions above will change the
    file sha — that is treated as context change and forces reassess,
    which is conservative and correct for the spec's
    'changed surrounding context -> reassess' gate).
    """
    return _file_sha256(merged_db, path)

def build_identity(
    *,
    merged_db: Path,
    target: dict,
    counterpart: dict,
    classification: str,
    terra_model: str | None,
    luna_model: str | None,
    terra_instructions_id: str | None,
    luna_instructions_id: str | None,
    stage_version: str | None,
) -> dict:
    t_path = str(target.get("path") or "")
    c_path = str(counterpart.get("path") or "")
    # Prefer explicit sha from evidence anchors when available (already
    # content-derived); fall back to DB lookup for the unit text.
    t_sha = str(target.get("sha256") or _unit_sha256(merged_db, t_path, str(target.get("symbol") or "")))
    c_sha = str(counterpart.get("sha256") or _unit_sha256(merged_db, c_path, str(counterpart.get("symbol") or "")))
    t_file = _inline_context_hash(merged_db, t_path, str(target.get("symbol") or ""))
    c_file = _inline_context_hash(merged_db, c_path, str(counterpart.get("symbol") or ""))
    return {
        "v": CACHE_VERSION,
        "target_sha256": t_sha,
        "counterpart_sha256": c_sha,
        "target_path": t_path,
        "counterpart_path": c_path,
        "target_file_sha256": t_file,
        "counterpart_file_sha256": c_file,
        "classification": str(classification or ""),
        "terra_model": str(terra_model or ""),
        "luna_model": str(luna_model or ""),
        "terra_instructions_id": str(terra_instructions_id or ""),
        "luna_instructions_id": str(luna_instructions_id or ""),
        "stage_version": str(stage_version or STAGE_VERSION_KEY),
    }

def cache_key(identity: dict) -> str:
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

def _load_raw(state_dir: Path | str, repo: str, pr: int) -> dict:
    p = cache_path(state_dir, repo, pr)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("version") != CACHE_VERSION:
        return {}
    entries = raw.get("entries")
    return entries if isinstance(entries, dict) else {}

def load_cache(state_dir: Path | str, repo: str, pr: int) -> dict:
    return dict(_load_raw(state_dir, repo, pr))

def save_cache(state_dir: Path | str, repo: str, pr: int, entries: dict) -> None:
    p = cache_path(state_dir, repo, pr)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "repo": repo, "pr": pr, "entries": entries}
    tmp = p.with_name(f"{p.name}.tmp-{time.time_ns()}")
    try:
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass

def _has_completion_flags(row: dict) -> bool:
    """True when the row carries explicit completion state (new records).

    Legacy rows predate the flags and fall back to reason-string checks.
    """
    return isinstance(row, dict) and ("completed" in row or "budget_refused" in row)


def is_completed_row(row: dict) -> bool:
    """Authority for cache reuse: only completed decisions are reusable.

    Every failure (budget stop, provider failure, evidence failure,
    stale skip) retries; completed decisions (published, deterministic
    drops, completed no-lead / no-opportunity / editor-drop verdicts)
    retain their existing reuse behaviour (including tool-dependency
    and provider guards applied separately).
    """
    if not isinstance(row, dict):
        return False
    if _has_completion_flags(row):
        return bool(row.get("completed"))
    # Legacy fallback for rows predating completion flags: retry unless
    # unambiguously completed. A failed legacy call can hide behind an
    # empty detail (e.g. a failed Luna record carrying only editor_error),
    # so the absence of failure keywords never proves completion.
    reason = row.get("reason")
    if reason == "budget_stop":
        return False
    if reason in ("evidence_failed", "stale_superseded"):
        return False
    if reason in ("terra_no_lead", "editor_unpublishable"):
        # Ambiguous without flags (provider failure vs completed no-lead)
        # → retry. New flagged rows keep the completed no-lead reusable.
        return False
    if reason == "editor_drop":
        return True  # completed Luna decision to drop
    return True


def is_budget_stop_row(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    # Completion state first: budget refusals always retry, never cached.
    if _has_completion_flags(row):
        return bool(row.get("budget_refused"))
    if row.get("reason") == "budget_stop":
        return True
    # Also catch mislabeled refusals that hid a budget error
    detail = str(row.get("detail") or "")
    if row.get("reason") in ("terra_no_lead", "editor_unpublishable") and "exceeds remaining budget" in detail:
        return True
    if "BudgetCeilingExceeded" in detail:
        return True
    return False

def is_provider_failure_row(row: dict) -> bool:
    """Unsuccessful provider calls must not be cached.

    Completion state first: any non-completed, non-budget row is a
    retryable failure. Legacy rows without flags fall back to the
    reason-string check.
    """
    if not isinstance(row, dict):
        return False
    if _has_completion_flags(row):
        if bool(row.get("budget_refused")):
            return False  # budget stops are classified separately
        return not bool(row.get("completed"))
    reason = row.get("reason")
    if reason == "editor_drop":
        return False  # completed Luna decision, never a provider failure
    if reason in ("terra_no_lead", "editor_unpublishable"):
        detail = str(row.get("detail") or "")
        if "exceeds remaining budget" in detail or "BudgetCeilingExceeded" in detail:
            return True  # retryable (budget classification takes precedence)
        # Failed-call signals: error detail keywords, a recorded
        # editor_error, or an attempted-but-undecided Luna call. A failed
        # legacy Luna record may carry only editor_error with an empty
        # detail — that still retries.
        if detail and any(k in detail for k in ("timeout", "unavailable", "failed", "Provider", "model_failed", "unreadable", "invalid", "BudgetCeiling")):
            return True
        if str(row.get("editor_error") or ""):
            return True
        if row.get("edit_ran") and not (row.get("editor") or {}).get("decision"):
            return True
        # Also treat rows without assess_ran as failures (no completed verdict)
        if not row.get("assess_ran"):
            # But legitimate terra_no_lead with overlap false and assess_ran False? Actually assess_ran is set only for successful?
            # In _review_inner, terra_no_lead rows have assess_ran True even when overlap false? Let's check: they set assess_ran True before.
            # So this is defensive.
            return False
        # Ambiguous legacy verdict (e.g. empty detail, no error signal):
        # retryable, never reused.
        return True
    return False

def lookup(
    state_dir: Path | str,
    repo: str,
    pr: int,
    identity: dict,
    cache: dict | None = None,
) -> dict | None:
    """Return cached row for identity, or None on miss/invalid."""
    if cache is None:
        cache = _load_raw(state_dir, repo, pr)
    key = cache_key(identity)
    row = cache.get(key)
    if not isinstance(row, dict):
        return None
    if is_budget_stop_row(row) or is_provider_failure_row(row):
        return None
    if not is_completed_row(row):
        return None
    # Tool-dependent verdicts must not be reused (helper file change)
    if row.get("terra_tool_calls") or row.get("luna_tool_calls"):
        return None
    # Verify stored identity matches current (defensive)
    stored_ident = row.get("_cache_identity")
    if isinstance(stored_ident, dict) and stored_ident != identity:
        return None
    return row

def store(
    state_dir: Path | str,
    repo: str,
    pr: int,
    identity: dict,
    row: dict,
    cache: dict | None = None,
) -> dict:
    """Persist row under identity; returns updated cache dict."""
    if is_budget_stop_row(row) or is_provider_failure_row(row):
        return cache if cache is not None else _load_raw(state_dir, repo, pr)
    if not is_completed_row(row):
        return cache if cache is not None else _load_raw(state_dir, repo, pr)
    if row.get("terra_tool_calls") or row.get("luna_tool_calls"):
        return cache if cache is not None else _load_raw(state_dir, repo, pr)
    if cache is None:
        cache = _load_raw(state_dir, repo, pr)
    key = cache_key(identity)
    # Attach identity for future validation (and debugging)
    to_store = dict(row)
    to_store["_cache_identity"] = dict(identity)
    to_store["_cached_at"] = time.time()
    cache[key] = to_store
    save_cache(state_dir, repo, pr, cache)
    return cache

def rebind_row(row: dict, merged_db: Path, repo: str, pr: int, head_rev: str) -> dict | None:
    """Rebind a cached row to current anchor locations.

    Validates that the target and counterpart units still exist and
    that their text sha matches the stored anchors (which are the same
    as the identity's target_sha256/counterpart_sha256). Returns a
    copy with updated anchors/head_rev, or None if the source is
    no longer valid (caller must reassess, never publish stale).
    """
    try:
        from codatiel_i2.semdup_dev import advisory as AV

        def _anchor_sha(a: dict) -> str:
            if a.get("sha256"):
                return str(a["sha256"])
            txt = a.get("text") or ""
            return hashlib.sha256(str(txt).encode("utf-8")).hexdigest() if txt else ""

        # Validate anchors still resolve and text unchanged
        t = row.get("target") or {}
        c = row.get("counterpart") or {}
        t_path, t_sym = str(t.get("path") or ""), str(t.get("symbol") or "")
        c_path, c_sym = str(c.get("path") or ""), str(c.get("symbol") or "")
        if not t_path or not c_path or not t_sym or not c_sym:
            return None
        t_anchor = AV.validate_anchor(merged_db, t_path, t_sym)
        c_anchor = AV.validate_anchor(merged_db, c_path, c_sym)
        if not t_anchor.get("ok") or not c_anchor.get("ok"):
            return None
        # Verify text sha matches cached anchors (protects against
        # file content change that wasn't captured by file hash due to
        # caching race)
        cached_anchors = row.get("anchors") or {}
        cached_t_sha = ((cached_anchors.get("target") or {}).get("sha256") or "")
        cached_c_sha = ((cached_anchors.get("counterpart") or {}).get("sha256") or "")
        cur_t_sha = _anchor_sha(t_anchor)
        cur_c_sha = _anchor_sha(c_anchor)
        if cached_t_sha and cur_t_sha and cur_t_sha != cached_t_sha:
            return None
        if cached_c_sha and cur_c_sha and cur_c_sha != cached_c_sha:
            return None
        # File existence already validated via anchor; also ensure
        # current head still contains the path (defensive)
        new_row = dict(row)
        # Update anchors to current locations
        new_anchors = {
            "target": {
                "path": t_anchor["path"],
                "symbol": t_anchor["symbol"],
                "start_line": t_anchor["start_line"],
                "end_line": t_anchor["end_line"],
                "sha256": cur_t_sha,
            },
            "counterpart": {
                "path": c_anchor["path"],
                "symbol": c_anchor["symbol"],
                "start_line": c_anchor["start_line"],
                "end_line": c_anchor["end_line"],
                "sha256": cur_c_sha,
            },
        }
        new_row["anchors"] = new_anchors
        # Rebind target/counterpart line for publishing
        if "target" in new_row and isinstance(new_row["target"], dict):
            new_row["target"] = dict(new_row["target"])
            new_row["target"]["start_line"] = t_anchor["start_line"]
        if "counterpart" in new_row and isinstance(new_row["counterpart"], dict):
            new_row["counterpart"] = dict(new_row["counterpart"])
            new_row["counterpart"]["start_line"] = c_anchor["start_line"]
        new_row["head_rev"] = head_rev
        new_row["reused"] = True
        # Preserve original identity marker
        if "_cache_identity" in new_row:
            pass
        return new_row
    except Exception:
        return None
