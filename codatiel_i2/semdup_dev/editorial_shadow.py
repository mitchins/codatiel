"""Parallel editorial shadow (SEMDUP-07 Track A, DEV-only, report-only).

Runs the SEMDUP-06 Terra-propose / Luna-edit pipeline alongside the
existing DEV shadow implementation (M11 investigate -> findings ->
advisories) on the same delta, and persists its outputs separately
(`editorial.json` next to `findings.json` / `advisories.json`).

Rules:
- Prospective-only: invoked only from the worker's normal delta flow,
  which enforces delivery dedup, last-processed-head checkpoints,
  delta fingerprints, and the grandfather path for pre-existing PRs.
  This module performs no backfill and keeps no independent cursor.
- Never affects the engine/advisory path: every failure (backends,
  evidence, models, validation, budget) degrades to a recorded
  editorial outcome. This module never raises into the worker.
- Bounded: at most `max_pairs` candidate pairs per delta and a
  per-run spend ceiling; estimates are checked before each model call.
- No GitHub comments, no notifications, no state outside the run dir
  except the spend sink inside it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

EDITORIAL_VERSION = "semdup-editorial-shadow-0.1"
DEFAULT_MAX_PAIRS = 4
DEFAULT_CEILING_USD = 0.20
ELIGIBLE_RECORD_STATUS = ("finding", "investigate")

# Shared SEMDUP budget allocation (ACTIVATE-02 Phase 3 experiment).
# Flag-gated, default off: SEMDUP_SHARED_BUDGET=1 enables. Frozen params
# (shared_budget_spec.md) — no tuning without a new receipt.
SHARED_BUDGET_ENV = "SEMDUP_SHARED_BUDGET"
SHARED_R_MIN_USD = 0.01
SHARED_DEFAULT_MEAN_EDIT_USD = 0.002
SHARED_RESERVE_CAP_FRAC = 0.5
SHARED_MIN_EDITS_FOR_MEAN = 3

# SCOPE: the shared allocation applies to review_candidate_pairs (the
# integrated semdup stage path). The worker-path run_editorial below
# keeps the fixed 50/50 split unconditionally — wiring it would change
# the DEV shadow worker's behavior, which is out of scope for this
# experiment. Operators: the flag has no effect on run_editorial runs.


def shared_budget_enabled() -> bool:
    """Independent flag; anything but exactly "1" means fixed 50/50."""
    import os as _os
    return (_os.environ.get(SHARED_BUDGET_ENV) or "").strip() == "1"


def editorial_reserve_usd(cap: float, outstanding: int,
                            mean_edit_usd: float | None,
                            r_min: float = SHARED_R_MIN_USD) -> float:
    """Luna editorial reserve for outstanding Terra leads.

    R = min(C/2, max(R_MIN, outstanding × mean)). Zero outstanding means
    the floor only (F1: never reserve a phantom edit). Capped at half so
    Terra always keeps at least its old half.
    """
    try:
        mean = float(mean_edit_usd) if mean_edit_usd is not None else None
    except (TypeError, ValueError):
        mean = None
    if mean is None or not (mean > 0):
        mean = SHARED_DEFAULT_MEAN_EDIT_USD
    try:
        out = max(0, int(outstanding))
    except (TypeError, ValueError):
        out = 0
    return min(max(cap, 0.0) * SHARED_RESERVE_CAP_FRAC,
               max(r_min, out * mean))


def shared_remaining_usd(cap: float, spent_terra, spent_luna,
                           bounded_terra=None, bounded_luna=None):
    """Single remaining pool; None when anything is unaccountable.

    Subtracts confirmed spend AND bounded reservations (unknown-usage
    entries live in the bounded reserve — see SpendLedger). A None
    bounded reserve means unbounded unknown: fail closed. (F4)
    """
    if (spent_terra is None or spent_luna is None
            or bounded_terra is None or bounded_luna is None):
        return None
    try:
        cap_f = float(cap)
        if not (cap_f == cap_f):  # NaN cap fails closed
            return None
        return max(0.0, cap_f - float(spent_terra) - float(spent_luna)
                   - float(bounded_terra) - float(bounded_luna))
    except (TypeError, ValueError):
        return None


def terra_admitted(est_t, est_l, remaining, reserve) -> tuple:
    """Admission verdict for one Terra examination: (admit, reason_code).

    admit iff the pair's potential finding is completable within the
    shared remainder AND the editorial reserve survives. Each known side
    is enforced independently (a known over-budget estimate refuses even
    when the other side is unknown); conversion failure refuses.
    reason_code is one of "ok", "shared-remainder-exhausted",
    "editorial-reserve".
    """
    import math as _math

    def _isnan(v):
        try:
            return _math.isnan(float(v))
        except (TypeError, ValueError):
            return False

    try:
        rem = float(remaining)
    except (TypeError, ValueError):
        return False, "shared-remainder-exhausted"
    if _isnan(rem):  # NaN never admits
        return False, "shared-remainder-exhausted"
    try:
        _r = float(reserve)
    except (TypeError, ValueError):
        return False, "editorial-reserve"
    if _isnan(_r):  # NaN reserve fails closed (before max() swallows it)
        return False, "editorial-reserve"
    rsv = max(0.0, _r)
    try:
        te = float(est_t) if est_t is not None else None
    except (TypeError, ValueError):
        return False, "shared-remainder-exhausted"
    if te is not None and _isnan(te):  # NaN estimate never admits
        return False, "shared-remainder-exhausted"
    try:
        le = float(est_l) if est_l is not None else None
    except (TypeError, ValueError):
        return False, "shared-remainder-exhausted"
    if le is not None and _isnan(le):
        return False, "shared-remainder-exhausted"
    # Completability first: a pair that cannot fit is exhausted,
    # regardless of the reserve. Reserve second: a pair that fits but
    # breaks the reserve is a reserve refusal.
    if te is not None and te > rem:
        return False, "shared-remainder-exhausted"
    if le is not None and (te or 0.0) + le > rem:
        return False, "shared-remainder-exhausted"
    if rem < rsv:
        return False, "editorial-reserve"
    if te is not None and rem - te < rsv:
        return False, "editorial-reserve"
    return True, "ok"


def new_shared_state() -> dict:
    """Per-run shared-budget state (outstanding leads, edit stats)."""
    return {"outstanding": 0, "luna_edits": 0, "luna_spent": 0.0}


def _shared_release(shared, luna_ledger=None, sl_before=None,
                    decided: bool = False) -> None:
    """Release one outstanding lead at pair finalization (F3).

    Held/failed/dropped leads are terminal for this run — no retry pass
    exists — so counting them forever would ratchet the reserve up to
    its cap and re-strand Terra. The R_MIN floor still protects future
    leads. Mean-edit stats update only on decided edits.
    """
    if not isinstance(shared, dict):
        return
    try:
        shared["outstanding"] = max(
            0, int(shared.get("outstanding", 1)) - 1)
        if decided and luna_ledger is not None and sl_before is not None:
            sl_after = _spent(luna_ledger)
            if sl_after is not None and sl_after >= sl_before:
                shared["luna_spent"] = float(
                    shared.get("luna_spent", 0.0)) + (sl_after - sl_before)
                shared["luna_edits"] = int(
                    shared.get("luna_edits", 0)) + 1
    except (TypeError, ValueError):
        pass


def shared_mean_edit_usd(state: dict) -> float:
    """Live mean Luna cost per completed edit; default until observed."""
    try:
        n = int(state.get("luna_edits") or 0)
        s = float(state.get("luna_spent") or 0.0)
    except (TypeError, ValueError):
        return SHARED_DEFAULT_MEAN_EDIT_USD
    if n < SHARED_MIN_EDITS_FOR_MEAN or s <= 0:
        return SHARED_DEFAULT_MEAN_EDIT_USD
    return s / n


def _is_oracle(path: str) -> bool:
    return path == "oracles" or path.startswith("oracles/")


def _is_fragment(symbol: str) -> bool:
    return "#region" in (symbol or "")


def unordered_mirror_key(target: dict, counterpart: dict) -> str:
    """Unordered identity of a unit pair (direction-insensitive).

    Same format as the post-hoc mirror consolidation: the sorted
    ``path::symbol`` pair joined by ``~~``. The single definition
    lives here; stage._dedup_mirrors reuses it.
    """
    a = f"{(target or {}).get('path')}::{(target or {}).get('symbol')}"
    b = f"{(counterpart or {}).get('path')}::{(counterpart or {}).get('symbol')}"
    return "~~".join(sorted((a, b)))


def mirror_skip_decision(target: dict, counterpart: dict,
                         published_keys) -> bool:
    """True when the opposite direction already has an established
    published verdict: the unordered pair key is present in the
    published set (earlier in this run, or a same-revision
    continuation seed). Never true on rejection alone — only
    published verdicts are recorded in the set."""
    try:
        return unordered_mirror_key(target, counterpart) in published_keys
    except Exception:
        return False


def mirror_preskip_row(pair: dict, *, head_rev: str, base_rev: str,
                       kept: str = "") -> dict:
    """Suppressed row for a pre-examination mirror skip ($0, no verdict).

    Not a judgment: completed=False so it is never cached or continued
    (the kept twin's own record re-establishes the skip on re-drive),
    and assess never ran so it never counts as examined.
    """
    return {
        "target": pair.get("target"), "counterpart": pair.get("counterpart"),
        "record_status": pair.get("record_status"),
        "classification": pair.get("classification"),
        "pair_id": pair.get("pair_id"), "pair_rank": pair.get("pair_rank"),
        "head_rev": head_rev, "base_rev": base_rev,
        "path": "suppressed", "reason": "mirror_of_published",
        "detail": ("same unordered unit pair already published"
                   + (f" ({kept})" if kept else "") +
                   "; opposite direction skipped before examination"),
        "assess_ran": False, "completed": False, "budget_refused": False,
        "terra_completed": False, "luna_completed": None,
    }


def budget_status(row: dict) -> str:
    """Classify a budget refusal into an auditable state (SIGNAL-01 B4).

    Distinguishes: refused before the first provider request (preflight
    gate, $0 possible), refused after examination started but before
    any billed response was recorded (spend unknown, never assumed
    $0), interrupted after one or more billed responses (partial
    spend confirmed), and Terra-held/Luna-refused. Non-budget rows
    return "".
    """
    try:
        row = row or {}
        if row.get("reason") != "budget_stop":
            return ""
        terra_ids = row.get("terra_response_ids") or []
        luna_ids = row.get("luna_response_ids") or []
        if not row.get("assess_ran"):
            return "refused_before_first_request"
        # Partial billed spend outranks stage labels: a Luna refusal
        # after billed Luna turns is an interruption, not a clean hold.
        if terra_ids or luna_ids:
            return "interrupted_after_partial_spend"
        if row.get("terra_completed"):
            return "terra_held_luna_refused"
        return "refused_before_first_billed_response"
    except Exception:
        return ""


def _ledger_order(ledger: Any) -> list:
    """Snapshot of recorded response ids (duck-typed; [] when unknown)."""
    try:
        order = getattr(ledger, "order", None)
        return list(order) if order is not None else []
    except Exception:
        return []


def _num_wall(value: Any) -> float:
    """Wall seconds as float; non-numeric transcripts degrade to 0.0."""
    try:
        return round(float(value or 0.0), 1)
    except (TypeError, ValueError):
        return 0.0


def _ledger_delta_ids(ledger: Any, before: list) -> list:
    """Response ids recorded during one examination stage (serial loop)."""
    try:
        after = _ledger_order(ledger)
        seen = set(before or [])
        return [rid for rid in after if rid not in seen]
    except Exception:
        return []


def _tool_summary(tool_calls: Any) -> list:
    """Compact per-call record: name + observed summary chars.

    Summary chars are an exact character count of the logged tool
    result, NOT provider tokens — never presented as token usage.
    """
    out = []
    try:
        for entry in tool_calls or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("tool") or entry.get("name") or ""
            summary = entry.get("summary")
            try:
                chars = len(str(summary)) if summary is not None else 0
            except Exception:
                chars = 0
            out.append({"name": str(name), "summary_chars": chars})
    except Exception:
        pass
    return out


def _stage_usage(ledger: Any, response_ids: list) -> dict:
    """Exact provider-reported usage for one stage (never estimated).

    Sums only provider-reported token fields over the stage's response
    ids. ``usage_known`` counts entries with provider payloads;
    ``unknown`` counts recorded responses without them (never priced,
    never zero-filled). ``priced_usd`` uses live ledger rates or None.
    """
    result: dict[str, Any] = {
        "responses": len(response_ids or []), "requests": 0,
        "input_tokens": 0, "cached_input_tokens": 0,
        "cache_write_input_tokens": 0, "output_tokens": 0,
        "reasoning_output_tokens": 0, "usage_known": 0,
        "unknown": 0, "priced_usd": None,
    }
    try:
        entries = getattr(ledger, "entries", None)
        if not isinstance(entries, dict):
            result["unknown"] = len(response_ids or [])
            return result
        for rid in response_ids or []:
            entry = entries.get(str(rid))
            if not isinstance(entry, dict) or not entry.get("usage_known"):
                result["unknown"] += 1
                continue
            result["usage_known"] += 1
            try:
                result["requests"] += int(entry.get("requests") or 0)
                for key in ("input_tokens", "cached_input_tokens",
                            "cache_write_input_tokens", "output_tokens",
                            "reasoning_output_tokens"):
                    value = entry.get(key)
                    if isinstance(value, (int, float)) and value:
                        result[key] += int(value)
            except Exception:
                continue
        try:
            priced = ledger.price_entries(response_ids or [])
            result["priced_usd"] = priced.get("confirmed_usd")
            if priced.get("unknown"):
                result["unknown"] = max(
                    result["unknown"], int(priced["unknown"]))
        except Exception:
            pass
    except Exception:
        pass
    return result


def _price_row(row: dict, terra_ledger: Any,
               luna_ledger: Any) -> float | None:
    """Confirmed spend for one pair's recorded responses (or None).

    Sums per-side confirmed prices; any side with recorded-but-unknown
    spend taints the total to None (unknown, never $0). A side with no
    recorded responses contributes $0. Shared by the row field and the
    telemetry builder so the two can never disagree.
    """
    try:
        row = row or {}
        terra_ids = list(row.get("terra_response_ids") or [])
        luna_ids = list(row.get("luna_response_ids") or [])
        if not terra_ids and not luna_ids:
            # No recorded responses: unknown, never $0 (the only
            # certain-$0 case — refused before the first request — is
            # classified by budget_status, not priced here).
            return None
        total = 0.0
        for ledger, ids in ((terra_ledger, terra_ids),
                            (luna_ledger, luna_ids)):
            if not ids:
                continue
            try:
                priced = ledger.price_entries(ids)
            except Exception:
                return None
            if not isinstance(priced, dict):
                return None
            if priced.get("unknown"):
                return None
            confirmed = priced.get("confirmed_usd")
            if not isinstance(confirmed, (int, float)):
                return None
            total += confirmed
        return round(total, 6)
    except Exception:
        return None


def _pair_telemetry_row(row: dict, *, repo: str, pr: Any,
                        terra_ledger: Any, luna_ledger: Any,
                        models: Any = None) -> dict:
    """Assemble one per-pair economics record (SIGNAL-01 B2).

    Joins row fields with provider-reported ledger usage. Every token
    count is provider-reported; prompt/source sizes are exact character
    counts labeled as such; priced USD comes from live ledger rates.
    """
    row = row or {}
    terra_ids = list(row.get("terra_response_ids") or [])
    luna_ids = list(row.get("luna_response_ids") or [])
    terra_usage = _stage_usage(terra_ledger, terra_ids)
    luna_usage = _stage_usage(luna_ledger, luna_ids)
    priced = _price_row(row, terra_ledger, luna_ledger)
    try:
        models = models or {}
        terra_model = (models.get("terra") or {}).get("model")
        terra_iid = (models.get("terra") or {}).get("instructions_id")
        luna_model = (models.get("luna") or {}).get("model")
        luna_iid = (models.get("luna") or {}).get("instructions_id")
    except Exception:
        terra_model = terra_iid = luna_model = luna_iid = None
    record = {
        "pair_id": row.get("pair_id"), "pair_rank": row.get("pair_rank"),
        "repo": repo, "pr": pr,
        "base_rev": row.get("base_rev"), "head_rev": row.get("head_rev"),
        "target": {"path": (row.get("target") or {}).get("path"),
                   "symbol": (row.get("target") or {}).get("symbol"),
                   "chars": row.get("target_chars")},
        "counterpart": {"path": (row.get("counterpart") or {}).get("path"),
                        "symbol": (row.get("counterpart") or {}).get("symbol"),
                        "chars": row.get("counterpart_chars")},
        "terra_prompt_chars": row.get("terra_prompt_chars"),
        "luna_prompt_chars": row.get("luna_prompt_chars"),
        "terra": {"model": terra_model, "instructions_id": terra_iid,
                  "responses": terra_usage["responses"],
                  "requests": terra_usage["requests"],
                  "usage": {k: terra_usage[k] for k in
                            ("input_tokens", "cached_input_tokens",
                             "cache_write_input_tokens", "output_tokens",
                             "reasoning_output_tokens")},
                  "usage_known": terra_usage["usage_known"],
                  "unknown": terra_usage["unknown"],
                  "priced_usd": terra_usage["priced_usd"],
                  "response_ids": terra_ids,
                  "tool_calls": row.get("terra_tools") or [],
                  "wall_s": row.get("terra_wall_s")},
        "luna": {"model": luna_model, "instructions_id": luna_iid,
                 "responses": luna_usage["responses"],
                 "requests": luna_usage["requests"],
                 "usage": {k: luna_usage[k] for k in
                           ("input_tokens", "cached_input_tokens",
                            "cache_write_input_tokens", "output_tokens",
                            "reasoning_output_tokens")},
                 "usage_known": luna_usage["usage_known"],
                 "unknown": luna_usage["unknown"],
                 "priced_usd": luna_usage["priced_usd"],
                 "response_ids": luna_ids,
                 "tool_calls": row.get("luna_tools") or [],
                 "wall_s": row.get("luna_wall_s")},
        "pair_priced_usd": priced,
        "outcome": {"path": row.get("path"), "reason": row.get("reason"),
                    "decision": row.get("decision"),
                    **({"budget_reason": row.get("budget_reason")}
                       if row.get("budget_reason") is not None else {})},
        "classification": ((row.get("editor") or {}).get("classification")
                           if isinstance(row.get("editor"), dict) else None),
        "budget_status": budget_status(row),
        "wall_s": row.get("wall_s"),
    }
    return record


def _telemetry_for_interrupted(
        pair: dict, *, repo: str, pr: Any, base_rev: str, head_rev: str,
        terra_ledger: Any, luna_ledger: Any, models: Any,
        terra_before: list, luna_before: list) -> dict:
    """Attribution record for a pair whose examination raised.

    Spend responses recorded before the interruption are in the
    ledgers; this maps them to the pair before the exception
    propagates (behavior unchanged: the caller re-raises).
    """
    try:
        row = {
            "target": (pair or {}).get("target"),
            "counterpart": (pair or {}).get("counterpart"),
            "pair_id": (pair or {}).get("pair_id"),
            "pair_rank": (pair or {}).get("pair_rank"),
            "base_rev": base_rev, "head_rev": head_rev,
            "terra_response_ids": _ledger_delta_ids(
                terra_ledger, terra_before),
            "luna_response_ids": _ledger_delta_ids(
                luna_ledger, luna_before),
            "path": "interrupted", "reason": "exception",
        }
        return _pair_telemetry_row(
            row, repo=repo, pr=pr, terra_ledger=terra_ledger,
            luna_ledger=luna_ledger, models=models)
    except Exception:
        return {}


def _append_pair_telemetry(run_dir: Any, record: dict) -> None:
    """Append one pair record to semdup_pairs.jsonl (crash-durable).

    Best-effort per-pair append, mirroring the spend sinks: an
    interrupted examination retains every completed pair's record.
    Never raises.
    """
    try:
        import json as _json
        path = Path(run_dir) / "semdup_pairs.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_json.dumps(record, sort_keys=True,
                                     default=str) + "\n")
            handle.flush()
            try:
                import os as _os
                _os.fsync(handle.fileno())
            except Exception:
                pass
    except Exception:
        pass


def _write_rate_snapshot(run_dir: Any, *, terra_res: dict,
                         luna_res: dict, terra_model: str,
                         luna_model: str) -> None:
    """Persist the pricing identity for future offline repricing (B3).

    Immutable snapshot of the rate cards + statuses + model ids in
    effect for this review. Never rewrites history; one file per run.
    """
    try:
        import json as _json
        snapshot = {
            "recorded": "review_start",
            "terra": {"model": terra_model,
                      "provider": (terra_res or {}).get("provider"),
                      "rate_usd_per_mtok": dict(
                          (terra_res or {}).get("rate_usd_per_mtok") or {}),
                      "rate_status": (terra_res or {}).get("rate_status")},
            "luna": {"model": luna_model,
                     "provider": (luna_res or {}).get("provider"),
                     "rate_usd_per_mtok": dict(
                         (luna_res or {}).get("rate_usd_per_mtok") or {}),
                     "rate_status": (luna_res or {}).get("rate_status")},
        }
        (Path(run_dir) / "rate_snapshot.json").write_text(
            _json.dumps(snapshot, indent=1, sort_keys=True, default=str),
            encoding="utf-8")
    except Exception:
        pass


def summarize_budget_refusals(rows: list) -> dict:
    """Counts + confirmed spend per budget state over judged rows."""
    summary: dict[str, Any] = {"total": 0, "by_status": {},
                               "confirmed_usd": 0.0,
                               "confirmed_unknown": False}
    try:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            status = budget_status(row)
            if not status:
                continue
            summary["total"] += 1
            entry = summary["by_status"].setdefault(
                status, {"count": 0, "confirmed_usd": 0.0,
                         "unknown": False})
            entry["count"] += 1
            # refused_before_first_request attempted no provider call:
            # confirmed $0. Every other state prices recorded responses
            # (None = unknown, never $0).
            if status == "refused_before_first_request":
                priced: Any = 0.0
            else:
                priced = (row.get("pair_priced_usd")
                          if isinstance(row.get("pair_priced_usd"),
                                       (int, float)) else None)
            if priced is None:
                entry["unknown"] = True
                summary["confirmed_unknown"] = True
            else:
                entry["confirmed_usd"] = round(
                    entry["confirmed_usd"] + priced, 6)
                summary["confirmed_usd"] = round(
                    summary["confirmed_usd"] + priced, 6)
    except Exception:
        pass
    return summary


def _mirror_preskip(pair: dict, *, head_rev: str, base_rev: str,
                    published_keys: dict, outcome: dict,
                    on_pair=None) -> tuple[bool, str]:
    """Mirror pre-skip for one pair; records the row when it fires.

    Shared by run_editorial and review_candidate_pairs so the
    first-published-wins rule cannot drift between the two entry
    points. Returns (skipped, mirror_key).
    """
    mkey = unordered_mirror_key(pair.get("target"),
                                pair.get("counterpart"))
    if not mirror_skip_decision(pair.get("target"),
                                pair.get("counterpart"), published_keys):
        return False, mkey
    row = mirror_preskip_row(pair, head_rev=head_rev, base_rev=base_rev,
                             kept=published_keys[mkey])
    outcome["pairs"].append(row)
    outcome["suppressed"].append(_suppressed_row(row))
    outcome["mirror_skipped"] = int(outcome.get("mirror_skipped") or 0) + 1
    if on_pair is not None:
        try:
            on_pair(row)
        except Exception:  # noqa: BLE001 - hook never breaks judging
            pass
    return True, mkey


def _record_judged(outcome: dict, row: dict, mkey: str,
                   published_keys: dict, on_pair=None) -> None:
    """Record one judged pair: verdict row, published twin tracking,
    optional on_pair hook. Shared by both judging loops."""
    outcome["pairs"].append(row)
    if row["path"] == "published":
        t, c = row.get("target") or {}, row.get("counterpart") or {}
        published_keys[mkey] = (
            f"{t.get('path')}::{t.get('symbol')} -> "
            f"{c.get('path')}::{c.get('symbol')}")
        outcome["published"].append(_public_row(row))
    else:
        outcome["suppressed"].append(_suppressed_row(row))
    if on_pair is not None:
        try:
            on_pair(row)
        except Exception:  # noqa: BLE001 - hook never breaks judging
            pass


def _split_unit(ref: str) -> tuple[str, str] | None:
    if "::" not in ref:
        return None
    path, _, symbol = ref.partition("::")
    path, symbol = path.strip(), symbol.strip()
    if not path or not symbol:
        return None
    return path, symbol


# Bump SCAFFOLD_RULE whenever the predicate changes in ANY way that
# could alter a verdict (tokens, masking, regions, path gate): cached
# and continued scaffold_skip rows are validated against it, and the
# committed replay fixture pins the token set, so a behavior change
# without a version bump fails tests instead of silently reusing
# stale verdicts.
SCAFFOLD_RULE = "scaffold-01"
# Matched against the whitespace-collapsed (not fully stripped)
# line, so verdict keywords keep word boundaries where they matter
# ("throw " never matches "throws"; "should " never matches
# "should_retry"). Stems ("assert", "fail") and dotted forms
# deliberately also match longer verdict calls ("assertEqual",
# "assert(", "t.Fatal", "self.fail(", "t.Errorf("): over-matching
# only ever fails open (safe). Bare call shapes ("expect(") would
# collide with neutral identifiers, so only dotted/suffixed forms
# and the Go/testify families are listed.
SCAFFOLD_ASSERT_TOKENS = (
    "assert", "xctunwrap", "xctexpectfailure", "xctskip", "confirmation",
    "expect_exception", "#expect", "#require", "xctassert", "xctfail",
    "recordfailure", "issue.record", "pytest.raises", "pytest.fail",
    "self.fail", "require(", "require.", "require_", "expect(",
    "expect_", "verify(", "wait(", "fatal", "precondition", "raise ",
    "throw ", "throw(", "check(", "t.error", "t.fatal", "errorf",
    "fail(", "panic(", "panic!", "should ", "t.throws",
)

_SCAFFOLD_TRIVIAL = frozenset({
    "{", "}", "(", ")", ",", ";",
    "});", "})", ");", "},", "],", "}}", "{{",
    "}]);", "]),", "}]", "]);", "}),",
})

# Closer-only skeletons (any mix of brackets, commas, semicolons and
# quotes, ignoring whitespace) carry no setup meaning on their own.
_SCAFFOLD_CLOSER_CHARS = frozenset("{}()[],;\"'")


def _scaffold_mask_collapsed(line: str) -> str:
    """Skeleton form: string literals masked, whitespace collapsed.

    Assertion tokens are matched against this form so keywords keep
    word boundaries."""
    import re as _re
    try:
        masked = _re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
        masked = _re.sub(r"'(?:[^'\\]|\\.)*'", "''", masked)
        return _re.sub(r"\s+", " ", masked).strip()
    except Exception:
        return ""


def _scaffold_trivial(masked: str) -> bool:
    if not masked or masked in _SCAFFOLD_TRIVIAL:
        return True
    # Any closer-only skeleton (e.g. "}),", "} ]);") is structural
    # punctuation, not a setup statement — regardless of spacing.
    nospace = "".join(masked.split())
    if nospace and set(nospace) <= _SCAFFOLD_CLOSER_CHARS:
        return True
    if len(masked) < 3 and not masked[0].isalnum():
        return True
    if set(masked) <= {'"', "'"}:
        # A line that is only (masked) quote characters, e.g. a bare
        # docstring delimiter skeleton shared by any two documented
        # units: not evidence of shared setup.
        return True
    if masked.startswith("//"):
        return True
    if masked.startswith("#") and not masked.startswith(
            ("#expect", "#require")):
        return True
    return False


_SCAFFOLD_TEST_DIRS = ("tests", "test", "__tests__")


def _scaffold_is_test_path(path: str) -> bool:
    """Positive test-only identification, self-contained and strict.

    A path counts as test-only when it carries an explicit test
    marker: a tests/test/__tests__ directory, a test file name
    (test_*, *_test.py, *.spec.*, *.test.*), or a *-Tests directory
    (e.g. PresentationTests/). A bare fixtures/ or e2e/ attribution
    is NOT enough on its own: production fixture and end-to-end
    support code lives under such directories. The *-Tests suffix
    check is case-sensitive on purpose: a lowercase plural like
    "Contests/" is a plausible product domain, while capital-T
    *Tests directories are the test-target convention.
    """
    try:
        parts = str(path or "").split("/")
        if not parts or not parts[-1]:
            return False
        lowered = [p.lower() for p in parts]
        if any(d in _SCAFFOLD_TEST_DIRS for d in lowered[:-1]):
            return True
        name = lowered[-1]
        if (name.startswith("test_")
                or name.endswith(("_test.py", ".spec.ts", ".spec.js",
                                  ".test.ts", ".test.js"))):
            return True
        return any(d.endswith(("_tests", "-tests"))
                   or d.endswith(" Tests") or d.endswith("Tests")
                   for d in parts[:-1])
    except Exception:
        return False


def _scaffold_key(collapsed: str) -> str:
    """Sharing key: collapsed skeleton with spacing around bracket,
    comma, semicolon, colon and assignment characters removed.

    Purely stylistic spacing (``defer {`` vs ``defer{``,
    ``foo(a, b)`` vs ``foo(a,b)``, ``x = 1`` vs ``x=1``) never splits
    a shared setup line, while inner spacing elsewhere is preserved
    so distinct token sequences (``a and b`` vs ``aandb``) never merge.
    """
    import re as _re
    try:
        return _re.sub(r"\s*([{}()\[\],;:=])\s*", r"\1", collapsed)
    except Exception:
        return collapsed


def _scaffold_has_assert(collapsed: str) -> bool:
    """True when a collapsed skeleton line carries a verdict token.

    Checks the collapsed form (word boundaries intact) and the
    sharing-key form (catches spaced call shapes like ``check (x)``
    that normalize to ``check(x)``). Either hit marks the line.
    Over-matching only ever fails open.
    """
    try:
        low = collapsed.lower()
        if any(tok in low for tok in SCAFFOLD_ASSERT_TOKENS):
            return True
        key = _scaffold_key(collapsed).lower()
        return any(tok in key for tok in SCAFFOLD_ASSERT_TOKENS
                   if not tok.endswith(" "))
    except Exception:
        return False


def _scaffold_asserted(lines: list) -> list:
    """Boolean mask: verdict lines plus their bracket continuations.

    An assertion spans its full bracketed extent: continuation lines
    (e.g. a multi-line call's arguments and closing bracket) belong
    to the verdict even when the token sits only on the opening
    line, so shared continuations can never read as neutral setup.
    Bracket depth is tracked on masked text (string contents are
    already masked away, so only real delimiters count). Nested
    verdicts nest via a stack (an inner verdict never closes the
    outer one early). Unbalanced closers mark the remainder asserted
    rather than silently ending spans: malformed input fails safe.
    """
    try:
        cols = [c for _, c in lines]
        mask = [False] * len(cols)
        depth = 0
        stack: list = []
        for i, col in enumerate(cols):
            if _scaffold_has_assert(col):
                stack.append(depth)
            if stack:
                mask[i] = True
            depth += (col.count("{") + col.count("(") + col.count("[")
                      - col.count("}") - col.count(")") - col.count("]"))
            if depth < 0:
                # More closers than openers: the text is not
                # well-formed code; treat everything from here as
                # verdict rather than risk mislabeling it as setup.
                return [m or j >= i for j, m in enumerate(mask)]
            while stack and depth <= stack[-1]:
                stack.pop()
        return mask
    except Exception:
        return [True] * len(lines)


def scaffold_preskip_decision(target: dict,
                              counterpart: dict) -> dict:
    """Decide pre-examination scaffolding suppression (deterministic).

    ``target``/``counterpart`` carry ``path``, ``text`` (unit text from
    the evidence package) and optionally ``context`` (a list of the
    file-context window lines sent with the evidence). Returns
    {"skip", "shared", "detail"}. Skips only when: both units are
    test-only; at least one recognized assertion token appears in
    either unit or its sent context (pairs with no verifiable verdict
    anywhere nearby fail open); the skeleton-shared lines are
    non-empty and none of them is (or continues) a verdict, where a
    verdict spans its full bracketed extent so multi-line assertion
    bodies count; and every shared line lies in the setup region
    (before the first verdict) or teardown region (after the last
    verdict) of BOTH units. Fails open (no skip) on any ambiguity,
    including unparseable input — never raises.
    """
    no = {"skip": False, "shared": 0, "detail": ""}
    try:
        t = target or {}
        c = counterpart or {}
        if not (_scaffold_is_test_path(t.get("path"))
                and _scaffold_is_test_path(c.get("path"))):
            return dict(no, detail="not both test-only")

        def normalized(text):
            # (sharing key, collapsed line) pairs; sharing compares
            # brace-adjacent-normalized skeletons while assertions and
            # regions are judged on the collapsed form with word
            # boundaries intact.
            out = []
            for raw in str(text or "").splitlines():
                col = _scaffold_mask_collapsed(raw)
                if not col or _scaffold_trivial(col):
                    continue
                out.append((_scaffold_key(col), col))
            return out

        tnorm = normalized(t.get("text"))
        cnorm = normalized(c.get("text"))
        if not tnorm or not cnorm:
            return dict(no, detail="empty unit")

        def _context_assert_lines(raw_lines):
            # Context participates in the union gate only through
            # substantive lines: trivial closers, lone brackets and
            # comment-only lines are not verdicts, even nearby.
            out = []
            for raw in raw_lines or []:
                if not isinstance(raw, str):
                    continue
                col = _scaffold_mask_collapsed(raw)
                if not col or _scaffold_trivial(col):
                    continue
                out.append(col)
            return out

        tctx = _context_assert_lines(t.get("context"))
        cctx = _context_assert_lines(c.get("context"))
        if not any(_scaffold_has_assert(col)
                   for col in ([c for _, c in tnorm + cnorm] + tctx + cctx)):
            # No recognizable verdict in either unit or its sent file
            # context: without a verifiable assertion nearby the pair
            # could be unasserted logic rather than scaffolding around
            # verified behavior.
            return dict(no, detail="no recognized assertions")
        tkeys = [k for k, _ in tnorm]
        ckeys = [k for k, _ in cnorm]
        shared = sorted(set(tkeys) & set(ckeys))
        if not shared:
            return dict(no, detail="no shared skeleton lines")

        # Verdict spans first: a token anywhere marks its whole
        # bracketed extent, so multi-line assertion bodies and their
        # closing lines count as asserted even when only the opening
        # line carries the token.
        tmask = _scaffold_asserted(tnorm)
        cmask = _scaffold_asserted(cnorm)

        def key_asserted(lines, asserted, key):
            for i, (k, _) in enumerate(lines):
                if k == key and asserted[i]:
                    return True
            return False

        bad = [k for k in shared
               if key_asserted(tnorm, tmask, k)
               or key_asserted(cnorm, cmask, k)]
        if bad:
            return dict(no, detail=f"shared assertions ({len(bad)})")

        def in_setup(lines, asserted, key):
            idx = [i for i, (k, _) in enumerate(lines) if k == key]
            first = next((i for i, a in enumerate(asserted) if a), None)
            if first is None:
                setup, teardown = set(range(len(lines))), set()
            else:
                last = max(i for i, a in enumerate(asserted) if a)
                setup, teardown = set(range(first)), set(range(last + 1, len(lines)))
            ok = setup | teardown
            return bool(idx) and all(i in ok for i in idx)

        if any(not in_setup(tnorm, tmask, k) for k in shared):
            return dict(no, detail="shared outside setup/teardown (target)")
        if any(not in_setup(cnorm, cmask, k) for k in shared):
            return dict(no, detail="shared outside setup/teardown")
        return {"skip": True, "shared": len(shared),
                "detail": (f"shared test setup ({len(shared)} lines, "
                           "no assertions); skipped before examination")}
    except Exception:
        return dict(no, detail="unreadable input")


def scaffold_cacheable(row: dict) -> bool:
    """Whether a scaffold verdict may enter the cross-revision cache.

    Never: scaffold skips persist only in same-head continuation
    (version-gated). The deterministic rule is expected to evolve
    (assertion-token list), and a cached skip must not outlive the
    rule version that produced it.
    """
    try:
        return (row or {}).get("reason") != "scaffold_setup"
    except Exception:
        return False


def scaffold_cache_usable(cached: dict) -> bool:
    """Whether a cached row may be reused as a scaffold verdict.

    Defense in depth for the read path: a cached scaffold_setup row
    is reusable only when it carries the current rule version.
    Anything else (legacy/foreign rows, version drift) must be
    re-judged, never trusted.
    """
    try:
        if not isinstance(cached, dict):
            return False
        if cached.get("reason") != "scaffold_setup":
            return True
        return cached.get("_scaffold_rule") == SCAFFOLD_RULE
    except Exception:
        return False


def candidate_pairs(records: list, max_pairs: int) -> tuple[list, list]:
    """Collect (target, counterpart) candidates from finding records.

    Deterministic order: record order, then counterpart order. Oracle
    and region-fragment references are excluded with a recorded reason.
    Returns (pairs, skipped) where skipped records the reason per unit.
    """
    pairs: list = []
    skipped: list = []
    for record in records or []:
        if record.get("status") not in ELIGIBLE_RECORD_STATUS:
            continue
        target = record.get("target") or {}
        tpath, tsym = target.get("path"), target.get("symbol")
        if not tpath or not tsym:
            skipped.append({"unit": f"{tpath}::{tsym}",
                            "reason": "target unresolvable"})
            continue
        for ref in (record.get("counterparts") or []):
            if len(pairs) >= max_pairs:
                break
            split = _split_unit(str(ref))
            if split is None:
                skipped.append({"unit": str(ref),
                                "reason": "counterpart unparseable"})
                continue
            cpath, csym = split
            if _is_oracle(cpath) or _is_fragment(csym):
                skipped.append({"unit": str(ref),
                                "reason": "oracle/fragment excluded"})
                continue
            if cpath == tpath and csym == tsym:
                skipped.append({"unit": str(ref),
                                "reason": "self-pair excluded"})
                continue
            pairs.append({"target": {"path": tpath, "symbol": tsym},
                          "counterpart": {"path": cpath, "symbol": csym},
                          "record_status": record.get("status"),
                          "classification": record.get("classification")})
        if len(pairs) >= max_pairs:
            break
    return pairs, skipped


def _is_budget_error(err: Any) -> bool:
    """Ledger/spending-guard refusal, never a model verdict."""
    text = str(err or "")
    return ("BudgetCeilingExceeded" in text
            or "exceeds remaining budget" in text)


def _preflight_usd(pairs: list, merged_db: Path, terra_res: dict,
                   luna_res: dict) -> float | None:
    """Upper-bound cost to judge every pair through both models.

    Sums actual-size Terra + Luna estimates over the pair list using
    the deterministic representative texts (same rule as retrieval).
    Luna only runs on Terra leads, so the true cost is at or below
    this number — a conservative planning bound, never a promise.
    None when rates or texts are unavailable.
    """
    try:
        from codatiel_i2.semdup_dev import oai_dupe as _OD
        from codatiel_i2.semdup_dev import editor as _ED
        conn = sqlite3.connect(str(merged_db))
    except Exception:  # noqa: BLE001 - no preflight without the view
        return None
    try:
        total = 0.0
        for p in pairs:
            if not isinstance(p, dict):
                continue
            sizes = []
            for u in (p.get("target") or {}, p.get("counterpart") or {}):
                if not isinstance(u, dict):
                    continue
                r = conn.execute(
                    "SELECT length(text) FROM units WHERE path=? AND "
                    "symbol=? ORDER BY start_line LIMIT 1",
                    (u.get("path"), u.get("symbol"))).fetchone()
                if not r or not r[0]:
                    return None
                sizes.append(int(r[0]))
            if len(sizes) != 2:
                return None
            body = sum(sizes)
            te = _OD.estimate_call_usd(
                len(_OD.TERRA_INSTRUCTIONS) + body,
                terra_res.get("rate_usd_per_mtok") or {})
            le = _OD.estimate_call_usd(
                len(_ED.EDITOR_INSTRUCTIONS) + body,
                luna_res.get("rate_usd_per_mtok") or {})
            if te is None or le is None:
                return None
            total += te + le
        return round(total, 6)
    except Exception:  # noqa: BLE001 - estimates are best-effort
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _spent(ledger) -> float | None:
    try:
        return ledger.confirmed_usd()
    except Exception:  # noqa: BLE001 - spend unknown => stop spending
        return None


def _input_chars(render: Callable | None, *args: Any) -> int | None:
    """Actual model input size for the estimate, never a fixed proxy.

    A fixed character proxy blinds the preflight gate to real evidence
    size (a 50 KB unit costs ~15x the old 12 K proxy), letting calls
    pass the gate only to die at the ledger — mislabelled as model
    verdicts. Measure the rendered input; fall back to the proxy only
    if rendering itself fails.
    """
    try:
        if render is None:
            return None
        return len(render(*args))
    except Exception:  # noqa: BLE001 - fall back to the proxy
        return None


def run_editorial(*, cfg, repo_path: Path, merged_db: Path,
                  base_rev: str, head_rev: str, records: list,
                  run_dir: Path, head_tree: Path | None,
                  base_tree: Path | None, max_pairs: int | None = None,
                  ceiling_usd: float | None = None,
                  assess_fn: Callable | None = None,
                  edit_fn: Callable | None = None,
                  pr: Any = None) -> dict:
    """Run the editorial shadow pass for one delta. Never raises."""
    t0 = time.monotonic()
    outcome: dict[str, Any] = {
        "enabled": True, "ok": False, "version": EDITORIAL_VERSION,
        "head_rev": head_rev, "base_rev": base_rev,
        "pairs": [], "published": [], "suppressed": [],
        "skipped": [], "spend_usd": 0.0, "wall_s": 0.0,
        "mirror_skipped": 0, "mirror_saved_usd_est": None,
        "mirror_saved_wall_s_est": None, "scaffold_skipped": 0,
        "scaffold_saved_usd_est": None,
        "scaffold_saved_wall_s_est": None,
    }
    try:
        return _run(cfg=cfg, repo_path=repo_path, merged_db=merged_db,
                    base_rev=base_rev, head_rev=head_rev, records=records,
                    run_dir=run_dir, head_tree=head_tree,
                    base_tree=base_tree, max_pairs=max_pairs,
                    ceiling_usd=ceiling_usd, assess_fn=assess_fn,
                    edit_fn=edit_fn, outcome=outcome, t0=t0, pr=pr)
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        outcome["ok"] = False
        outcome["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome


def _run(*, cfg, repo_path, merged_db, base_rev, head_rev, records,
         run_dir, head_tree, base_tree, max_pairs, ceiling_usd,
         assess_fn, edit_fn, outcome, t0, pr=None) -> dict:
    from codatiel_i2 import providers as P
    from codatiel_i2 import runtime_sdk as R
    from codatiel_i2.core.spend import SpendLedger
    from codatiel_i2.semdup_dev import editor as ED
    from codatiel_i2.semdup_dev import oai_dupe as OD

    max_pairs = (cfg.editorial_max_pairs if max_pairs is None
                 else max_pairs)
    ceiling_usd = (cfg.editorial_ceiling_usd if ceiling_usd is None
                   else ceiling_usd)
    pairs, skipped = candidate_pairs(records, max_pairs)
    outcome["skipped"] = skipped
    if head_tree is None or base_tree is None:
        outcome["ok"] = True
        outcome["stage"] = "no_head_tree"
        outcome["skipped"] = skipped + [
            {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                     f"{p['counterpart']['path']}::{p['counterpart']['symbol']}",
             "reason": "head/base tree unavailable; candidates recorded, "
                       "no inference attempted"} for p in pairs]
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    try:
        terra_res = P.resolve_backend("terra")
        luna_res = P.resolve_backend("luna")
    except Exception as exc:  # noqa: BLE001 - no credentials => no spend
        outcome["ok"] = True
        outcome["stage"] = "backends_unavailable"
        outcome["skipped"] = skipped + [
            {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                     f"{p['counterpart']['path']}::{p['counterpart']['symbol']}",
             "reason": f"model backend unavailable: {exc}; no inference "
                       "attempted"} for p in pairs]
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    terra_model_id, terra_settings = P.build_model("terra")
    luna_model_id, luna_settings = P.build_model("luna")
    half = max(ceiling_usd / 2.0, 0.0)
    terra_ledger = SpendLedger(rate=terra_res["rate_usd_per_mtok"],
                              rate_status=terra_res["rate_status"],
                              ceiling_usd=ceiling_usd)
    luna_ledger = SpendLedger(rate=luna_res["rate_usd_per_mtok"],
                             rate_status=luna_res["rate_status"],
                             ceiling_usd=ceiling_usd)
    terra_ledger.attach_sink(run_dir / "spend_editorial_terra.jsonl")
    luna_ledger.attach_sink(run_dir / "spend_editorial_luna.jsonl")
    outcome["models"] = {
        "terra": {"model": terra_res.get("model"),
                  "provider": terra_res.get("provider"),
                  "instructions_id": OD.instructions_id(
                      "terra", OD.TERRA_INSTRUCTIONS)},
        "luna": {"model": luna_res.get("model"),
                 "provider": luna_res.get("provider"),
                 "instructions_id": ED.editor_instructions_id()},
    }
    assess = assess_fn or OD.assess_pair
    edit = edit_fn or ED.edit_pair
    # SIGNAL-01 B1/B3: stable pair ids + pricing snapshot (best-effort).
    try:
        from codatiel_i2.semdup_dev import pairing as _PA
        _PA.stamp_pairs(pairs, repo=(cfg.repos[0] if getattr(
            cfg, "repos", None) else "dev"), pr=pr, base_rev=base_rev,
            head_rev=head_rev)
    except Exception:
        pass
    _write_rate_snapshot(run_dir, terra_res=terra_res, luna_res=luna_res,
                         terra_model=str(terra_model_id),
                         luna_model=str(luna_model_id))
    # MIRROR-01: first-published-wins within this run. A reverse pair is
    # skipped before any paid call only after its twin published.
    published_keys: dict[str, str] = {}
    for pair in pairs:
        skipped, mkey = _mirror_preskip(
            pair, head_rev=head_rev, base_rev=base_rev,
            published_keys=published_keys, outcome=outcome)
        if skipped:
            continue
        # Ledger marks for interruption attribution (serial loop:
        # responses recorded between the marks belong to this pair).
        terra_before = _ledger_order(terra_ledger)
        luna_before = _ledger_order(luna_ledger)
        try:
            row = _review_pair(
                pair, cfg=cfg, repo_path=repo_path, merged_db=merged_db,
                base_rev=base_rev, head_rev=head_rev, head_tree=head_tree,
                base_tree=base_tree, terra_res=terra_res, luna_res=luna_res,
                terra_model_id=terra_model_id, terra_settings=terra_settings,
                luna_model_id=luna_model_id, luna_settings=luna_settings,
                terra_ledger=terra_ledger, luna_ledger=luna_ledger,
                ceiling_usd=ceiling_usd, half=half, assess=assess, edit=edit,
                OD=OD, ED=ED, R=R)
        except Exception:
            # Interruption (D8): retain partial attribution, then
            # propagate unchanged — failure semantics are preserved.
            _append_pair_telemetry(
                run_dir, _telemetry_for_interrupted(
                    pair, repo=(cfg.repos[0] if getattr(
                        cfg, "repos", None) else "dev"), pr=pr,
                    base_rev=base_rev, head_rev=head_rev,
                    terra_ledger=terra_ledger, luna_ledger=luna_ledger,
                    models=outcome.get("models"),
                    terra_before=terra_before, luna_before=luna_before))
            raise
        try:
            _append_pair_telemetry(
                run_dir, _pair_telemetry_row(
                    row, repo=(cfg.repos[0] if getattr(
                        cfg, "repos", None) else "dev"), pr=pr,
                    terra_ledger=terra_ledger, luna_ledger=luna_ledger,
                    models=outcome.get("models")))
        except Exception:
            pass
        _record_judged(outcome, row, mkey, published_keys)
    spent_t, spent_l = _spent(terra_ledger), _spent(luna_ledger)
    outcome["spend_terra_usd"] = spent_t
    outcome["spend_luna_usd"] = spent_l
    outcome["spend_terra_unknown"] = spent_t is None
    outcome["spend_luna_unknown"] = spent_l is None
    if spent_t is None or spent_l is None:
        outcome["spend_usd"] = (None if (spent_t is None and spent_l is None)
                                else round((spent_t or 0.0) + (spent_l or 0.0), 6))
    else:
        outcome["spend_usd"] = round(spent_t + spent_l, 6)
    outcome["spend_unknown"] = spent_t is None or spent_l is None
    _record_preskip_savings(outcome, spent_t, spent_l, t0)
    outcome["budget_refusals"] = summarize_budget_refusals(
        outcome.get("pairs"))
    outcome["ok"] = True
    outcome["wall_s"] = round(time.monotonic() - t0, 1)
    return outcome


def _record_preskip_savings(outcome: dict, spent_t, spent_l, t0) -> None:
    """Record estimated savings for both pre-examination skip kinds."""
    # Scaffold skips land inside _review_inner (no loop counter), so
    # count suppressed scaffold_setup rows at finalize time.
    outcome["scaffold_skipped"] = sum(
        1 for r in outcome.get("pairs", [])
        if isinstance(r, dict) and r.get("reason") == "scaffold_setup")
    for prefix in ("mirror", "scaffold"):
        _record_skip_savings(
            outcome, spent_t, spent_l, t0, f"{prefix}_skipped",
            f"{prefix}_saved_usd_est", f"{prefix}_saved_wall_s_est")


def _record_skip_savings(outcome: dict, spent_t, spent_l, t0, count_key: str,
                         usd_key: str, wall_key: str) -> None:
    """Estimated dollars/latency saved by pre-examination skips.

    Estimates only (labeled _est): skipped examinations are valued at
    the run's mean examined-pair cost and latency. The mean includes
    cheap Terra-only rejections, so the dollar figure is a
    conservative floor (a skipped examination that would have run both
    models costs more than the mean). USD is None when the spend is
    unknown or no examination baseline exists; latency needs only the
    wall clock.
    """
    mirror_n = int(outcome.get(count_key) or 0)
    outcome[count_key] = mirror_n
    outcome[usd_key] = None
    outcome[wall_key] = None
    if not mirror_n:
        outcome[usd_key] = 0.0
        outcome[wall_key] = 0.0
        return
    examined = sum(1 for r in outcome.get("pairs", [])
                   if isinstance(r, dict) and r.get("assess_ran"))
    if not examined:
        return
    if spent_t is not None and spent_l is not None:
        outcome[usd_key] = round(
            mirror_n * (spent_t + spent_l) / examined, 6)
    wall = round(time.monotonic() - t0, 1)
    outcome[wall_key] = round(mirror_n * wall / examined, 1)


def _review_pair(pair, *, cfg, repo_path, merged_db, base_rev, head_rev,
                 head_tree, base_tree, terra_res, luna_res,
                 terra_model_id, terra_settings, luna_model_id,
                 luna_settings, terra_ledger, luna_ledger, ceiling_usd,
                 half, assess, edit, OD, ED, R, shared=None) -> dict:
    pt0 = time.monotonic()
    row = _review_inner(
        pair, cfg=cfg, repo_path=repo_path, merged_db=merged_db,
        base_rev=base_rev, head_rev=head_rev, head_tree=head_tree,
        base_tree=base_tree, terra_res=terra_res, luna_res=luna_res,
        terra_model_id=terra_model_id, terra_settings=terra_settings,
        luna_model_id=luna_model_id, luna_settings=luna_settings,
        terra_ledger=terra_ledger, luna_ledger=luna_ledger,
        ceiling_usd=ceiling_usd, half=half, assess=assess, edit=edit,
        OD=OD, ED=ED, R=R, shared=shared)
    row["wall_s"] = round(time.monotonic() - pt0, 1)
    # Confirmed spend for this pair's recorded responses (None =
    # unknown, never $0). Single point: telemetry and the run summary
    # share it via _price_row and can never disagree.
    try:
        row["pair_priced_usd"] = _price_row(
            row, terra_ledger, luna_ledger)
    except Exception:
        row["pair_priced_usd"] = None
    return row


def _review_inner(pair, *, cfg, repo_path, merged_db, base_rev, head_rev,
                  head_tree, base_tree, terra_res, luna_res,
                  terra_model_id, terra_settings, luna_model_id,
                  luna_settings, terra_ledger, luna_ledger, ceiling_usd,
                  half, assess, edit, OD, ED, R, shared=None) -> dict:
    row: dict[str, Any] = {
        "target": pair["target"], "counterpart": pair["counterpart"],
        "record_status": pair.get("record_status"),
        "classification": pair.get("classification"),
        # SIGNAL-01 correlation label (never a cache/continuation key).
        "pair_id": pair.get("pair_id"), "pair_rank": pair.get("pair_rank"),
        "head_rev": head_rev, "base_rev": base_rev,
        "terra_response_ids": [], "luna_response_ids": [],
    }
    ev = ED.build_evidence_package(
        merged_db, repo_path, head_rev,
        pair["target"], pair["counterpart"])
    if not ev["ok"]:
        row.update({"path": "suppressed",
                    "reason": "evidence_failed",
                    "detail": f"{ev.get('code')}: {ev.get('detail', '')}",
                    # Completion state (COST-01B): evidence failure is
                    # inability, never a verdict — always retried.
                    "completed": False, "budget_refused": False,
                    "terra_completed": False, "luna_completed": None})
        return row
    row["anchors"] = {
        "target": {k: ev["target"][k] for k in
                   ("path", "symbol", "start_line", "end_line", "sha256")},
        "counterpart": {k: ev["counterpart"][k] for k in
                        ("path", "symbol", "start_line", "end_line",
                         "sha256")},
    }
    # SIGNAL-01 B2 evidence-size measures (exact chars, never estimates
    # presented as provider tokens).
    row["target_chars"] = len(ev["target"].get("text") or "")
    row["counterpart_chars"] = len(ev["counterpart"].get("text") or "")
    pre = ED.wrapper_prefilter(ev["target"]["text"],
                               ev["counterpart"]["symbol"],
                               ev["target"]["symbol"])
    if pre["wrapper"]:
        row.update({"path": "suppressed", "reason": "deterministic_drop",
                    "detail": pre["detail"],
                    # Deterministic prefilter verdict: completed without
                    # model calls, safely reusable across revisions.
                    "completed": True, "budget_refused": False,
                    "terra_completed": None, "luna_completed": None})
        return row
    # SCAFFOLD-01: deterministic test-scaffolding suppression. Runs on
    # pre-inference evidence only (unit texts + paths); completed like
    # other deterministic verdicts, so retries and revisions reuse it
    # without re-examination. Never fires on shared assertions, test
    # logic, oracles, or non-test code (fail open).
    # File-context windows travel with the evidence; their text
    # counts toward the union-assert gate (verifiable behavior nearby)
    # but never toward the shared set (only unit texts are compared).
    _ctx_lines: list = []
    try:
        for _f in (ev.get("files") or {}).values():
            if isinstance(_f, dict):
                for _line in (_f.get("window_lines") or []):
                    if isinstance(_line, str):
                        _ctx_lines.append(_line)
    except Exception:
        _ctx_lines = []
    sc = scaffold_preskip_decision(
        {"path": pair["target"].get("path"),
         "text": ev["target"]["text"], "context": _ctx_lines},
        {"path": pair["counterpart"].get("path"),
         "text": ev["counterpart"]["text"], "context": _ctx_lines})
    if sc["skip"]:
        row.update({"path": "suppressed", "reason": "scaffold_setup",
                    "detail": sc["detail"],
                    "completed": True, "budget_refused": False,
                    "terra_completed": False, "luna_completed": None,
                    # Rule version travels with the verdict so a future
                    # rule change never silently reuses a stale skip.
                    "_scaffold_rule": SCAFFOLD_RULE})
        return row
    terra_ctx = R.ReviewContext(
        repo=cfg.repos[0] if cfg.repos else "dev", base_sha=base_rev,
        head_sha=head_rev, base_dir=base_tree, head_dir=head_tree,
        diff_repo=repo_path, tool_budget=OD.TERRA_TOOL_BUDGET)
    # SIGNAL-01 B2: exact rendered prompt size (chars), reused for
    # the preflight estimate so the gate and the telemetry agree.
    row["terra_prompt_chars"] = _input_chars(
        OD.render_terra_input, ev, head_rev,
        pair.get("classification") or "introduced")
    est = OD.estimate_call_usd(
        row["terra_prompt_chars"]
        or (len(OD.TERRA_INSTRUCTIONS) + 12000),
        terra_res["rate_usd_per_mtok"])
    s = _spent(terra_ledger)
    if shared is None:
        if s is None or (est is not None and s + est > half):
            row.update({"path": "suppressed", "reason": "budget_stop",
                        "detail": ("terra estimate exceeds its half of the "
                                   f"whole-PR ceiling ${ceiling_usd:.2f}; "
                                   "unexamined, not rejected"),
                        "completed": False, "budget_refused": True,
                        "terra_completed": False, "luna_completed": None})
            return row
    else:
        # Shared allocation: admit iff the finding-in-progress is
        # completable within the shared remainder AND the editorial
        # reserve survives. Refusal reasons distinguish the reserve
        # from true remainder exhaustion (never lumped together).
        sl = _spent(luna_ledger)
        try:
            b_t = terra_ledger.bounded_reserve_usd()
        except Exception:
            b_t = None
        try:
            b_l = luna_ledger.bounded_reserve_usd()
        except Exception:
            b_l = None
        rem = shared_remaining_usd(ceiling_usd, s, sl, b_t, b_l)
        try:
            rsv = editorial_reserve_usd(
                ceiling_usd, shared.get("outstanding", 0),
                shared_mean_edit_usd(shared))
        except Exception:
            # Reserve uncomputable: fall back to the fixed-split half
            # (F8). Never looser than status quo, never a raise.
            rsv = max(float(ceiling_usd), 0.0) / 2.0
        # Luna estimate for a potential lead, bounded from the actual
        # evidence sizes (F6: no fixed character proxy).
        try:
            _ev_chars = len(ev["target"].get("text") or "") + len(
                ev["counterpart"].get("text") or "")
        except Exception:
            _ev_chars = 12000
        est_l_pot = OD.estimate_call_usd(
            len(ED.EDITOR_INSTRUCTIONS) + _ev_chars,
            luna_res.get("rate_usd_per_mtok") or {})
        admit, why = terra_admitted(est, est_l_pot, rem, rsv)
        if not admit:
            row["budget_reason"] = why
            row.update({"path": "suppressed", "reason": "budget_stop",
                        "detail": (
                            "shared remainder exhausted "
                            f"(remaining ${rem if rem is not None else '?'}); "
                            "unexamined, not rejected"
                            if why == "shared-remainder-exhausted" else
                            "editorial reserve protected "
                            f"(reserve ${rsv:.4f}, "
                            f"{shared.get('outstanding', 0)} lead(s) held); "
                            "unexamined, not rejected"),
                        "completed": False, "budget_refused": True,
                        "terra_completed": False, "luna_completed": None})
            return row
    terra_order_before = _ledger_order(terra_ledger)
    ta = assess(
        evidence={"target": ev["target"],
                  "counterpart": ev["counterpart"],
                  "pair_sha256": "editorial-shadow"},
        head_rev=head_rev,
        target_kind=pair.get("classification") or "introduced",
        tools=OD.scoped_tools(terra_ctx), model=terra_model_id,
        model_settings=terra_settings, ledger=terra_ledger, ctx=terra_ctx)
    row["assess_ran"] = True
    row["terra_response_ids"] = _ledger_delta_ids(
        terra_ledger, terra_order_before)
    row["terra_wall_s"] = _num_wall(
        (ta.get("transcript") or {}).get("wall_s"))
    row["terra_tool_calls"] = len(
        ((ta.get("transcript") or {}).get("tool_calls", [])))
    row["terra_tools"] = _tool_summary(
        (ta.get("transcript") or {}).get("tool_calls", []))
    if not ta.get("ok") or not (ta.get("terra") or {}).get("overlap"):
        err = str(ta.get("error", "") or "")
        if _is_budget_error(err):
            # Ledger refusal is inability, never a decline: record the
            # guard stop explicitly so budget exhaustion cannot hide
            # behind a model-rejection label.
            row.update({"path": "suppressed", "reason": "budget_stop",
                        "detail": ("Terra call refused by the spending "
                                   f"guard ({err[:160]}); unexamined, "
                                   "not rejected"),
                        "completed": False, "budget_refused": True,
                        "terra_completed": False, "luna_completed": None})
            return row
        terra_ok = bool(ta.get("ok"))
        row.update({"path": "suppressed", "reason": "terra_no_lead",
                    "detail": err,
                    "terra_note": str(
                        (ta.get("transcript") or {}).get("final_text",
                                                       "")
                        or "")[:600],
                    # Completion state: a successful Terra call that finds
                    # no overlap is a completed no-lead verdict (reusable);
                    # a failed call is inability (always retried).
                    "terra_completed": terra_ok, "luna_completed": None,
                    "budget_refused": False, "completed": terra_ok})
        return row
    terra = ta["terra"]
    row["terra"] = {k: terra.get(k) for k in
                    ("overlap", "relationship", "opportunity",
                     "shared_component", "differences")}
    if terra.get("opportunity", "none") == "none":
        row.update({"path": "suppressed",
                    "reason": "terra_no_opportunity",
                    "terra_note": str(terra.get("shared_component", "")
                                      or "")[:300],
                    "terra_completed": True, "luna_completed": None,
                    "budget_refused": False, "completed": True})
        return row
    proposed = ED.propose_comment(terra, ev)
    row["proposed_comment"] = proposed
    if shared is not None:
        # A lead now awaits edit; the reserve covers it.
        try:
            shared["outstanding"] = int(shared.get("outstanding", 0)) + 1
        except (TypeError, ValueError):
            shared["outstanding"] = 1
    luna_ctx = R.ReviewContext(
        repo=cfg.repos[0] if cfg.repos else "dev", base_sha=base_rev,
        head_sha=head_rev, base_dir=base_tree, head_dir=head_tree,
        diff_repo=repo_path, tool_budget=8)
    row["luna_prompt_chars"] = _input_chars(
        OD.render_luna_input, ev, terra)
    est_l = OD.estimate_call_usd(
        row["luna_prompt_chars"]
        or (len(ED.EDITOR_INSTRUCTIONS) + 12000),
        luna_res["rate_usd_per_mtok"])
    sl = _spent(luna_ledger)
    if shared is None:
        if sl is None or (est_l is not None and sl + est_l > half):
            row.update({"path": "suppressed", "reason": "budget_stop",
                        "detail": ("luna estimate exceeds its half of the "
                                   f"whole-PR ceiling ${ceiling_usd:.2f}; "
                                   "Terra proposal held, never published "
                                   "without Luna review"),
                        "terra_completed": True, "luna_completed": None,
                        "budget_refused": True, "completed": False})
            return row
    else:
        # Shared allocation: Luna completes outstanding work iff it fits
        # the shared remainder (reserve already accounts for this lead).
        sl_before = sl
        try:
            _b_t = terra_ledger.bounded_reserve_usd()
        except Exception:
            _b_t = None
        try:
            _b_l = luna_ledger.bounded_reserve_usd()
        except Exception:
            _b_l = None
        rem_l = shared_remaining_usd(
            ceiling_usd, _spent(terra_ledger), sl, _b_t, _b_l)
        if rem_l is None or (est_l is not None and est_l > rem_l):
            if shared is not None:
                _shared_release(shared)  # held = terminal this run (F3)
                row["budget_reason"] = "shared-remainder-exhausted"
            row.update({"path": "suppressed", "reason": "budget_stop",
                        "detail": ("luna estimate exceeds the shared "
                                   f"remainder ${rem_l}; Terra proposal "
                                   "held, never published without Luna "
                                   "review"),
                        "terra_completed": True, "luna_completed": None,
                        "budget_refused": True, "completed": False})
            return row
    lt0 = time.monotonic()
    luna_order_before = _ledger_order(luna_ledger)
    luna = edit(
        package=ev, proposed_comment=proposed,
        tools=R.build_i2_tools(luna_ctx), model=luna_model_id,
        model_settings=luna_settings, ledger=luna_ledger, ctx=luna_ctx)
    row["edit_ran"] = True
    if shared is not None:
        # Single release point per pair that reaches edit (no double
        # counting): every path below returns, so exactly one release
        # happens here. (sl_before is always bound: the shared Luna
        # preflight that precedes any edit sets it.) The Luna-preflight
        # refusal above releases separately — edit never runs there.
        _decided = ((luna.get("editor") or {}).get("decision")
                    in ("KEEP", "REVISE", "DROP"))
        _shared_release(shared, luna_ledger, sl_before, decided=_decided)
    row["luna_wall_s"] = round(time.monotonic() - lt0, 1)
    row["luna_response_ids"] = _ledger_delta_ids(
        luna_ledger, luna_order_before)
    row["luna_tool_calls"] = len(
        ((luna.get("transcript") or {}).get("tool_calls", [])))
    row["luna_tools"] = _tool_summary(
        (luna.get("transcript") or {}).get("tool_calls", []))
    row["editor"] = luna.get("editor")
    row["editor_error"] = luna.get("error", "")
    if not luna.get("ok"):
        if _is_budget_error(luna.get("error", "")):
            row.update({"path": "suppressed", "reason": "budget_stop",
                        "detail": ("Luna call refused by the spending "
                                   "guard; Terra proposal held, never "
                                   "published without Luna review"),
                        "terra_completed": True, "luna_completed": False,
                        "budget_refused": True, "completed": False})
            return row
        row.update({"path": "suppressed",
                    "reason": "editor_unpublishable",
                    "detail": str(luna.get("error", "") or "")[:600],
                    "terra_completed": True, "luna_completed": False,
                    "budget_refused": False, "completed": False})
        return row
    decision = (luna.get("editor") or {}).get("decision")
    if decision not in ("KEEP", "REVISE"):
        row.update({"path": "suppressed", "reason": "editor_drop",
                    "detail": ((luna.get("editor") or {}).get("reason", "")
                               or "")[:600],
                    "terra_completed": True, "luna_completed": True,
                    "budget_refused": False, "completed": True})
        return row
    row.update({"path": "published", "decision": decision,
                "revised_comment": (luna.get("editor") or {}).get(
                    "revised_comment", ""),
                "reason": ((luna.get("editor") or {}).get("reason", "")
                           or "")[:800],
                "terra_completed": True, "luna_completed": True,
                "budget_refused": False, "completed": True})
    return row


def _public_row(row: dict) -> dict:
    editor = row.get("editor") if isinstance(row.get("editor"), dict) \
        else {}
    # SIGNAL-01 A3: missing/malformed metadata falls back to unknown
    # here too (injected editors, legacy rows) — never invalidates.
    try:
        from codatiel_i2.semdup_dev.classification import (
            normalize_classification as _normalize)
        classification = _normalize(editor.get("classification"))
    except Exception:
        classification = None
    return {
        "target": row["target"], "counterpart": row["counterpart"],
        "head_rev": row.get("head_rev"),
        "pair_id": row.get("pair_id"), "pair_rank": row.get("pair_rank"),
        "decision": row.get("decision"),
        "revised_comment": row.get("revised_comment", ""),
        "reason": row.get("reason", ""),
        "classification": classification,
        "anchors": row.get("anchors", {}),
        "inference": {
            "terra_wall_s": row.get("terra_wall_s", 0.0),
            "terra_tool_calls": row.get("terra_tool_calls", 0),
            "luna_wall_s": row.get("luna_wall_s", 0.0),
            "luna_tool_calls": row.get("luna_tool_calls", 0),
        },
    }


def _suppressed_row(row: dict) -> dict:
    out = {
        "target": row["target"], "counterpart": row["counterpart"],
        "head_rev": row.get("head_rev"),
        "pair_id": row.get("pair_id"), "pair_rank": row.get("pair_rank"),
        "reason": row.get("reason", ""),
        "budget_status": budget_status(row),
        # Audit inputs for the derived budget_status (counts only;
        # full response ids live in semdup_pairs.jsonl).
        "assess_ran": bool(row.get("assess_ran")),
        "terra_completed": row.get("terra_completed"),
        "terra_responses": len(row.get("terra_response_ids") or []),
        "luna_responses": len(row.get("luna_response_ids") or []),
        "detail": str(row.get("detail", "") or "")[:600],
        "terra": row.get("terra", {}),
        "terra_note": str(row.get("terra_note", "") or "")[:600],
        "anchors": row.get("anchors", {}),
    }
    # budget_reason only when set (shared-mode refusals): keeps the
    # serialized shape identical for the default-off path.
    if row.get("budget_reason") is not None:
        out["budget_reason"] = row.get("budget_reason")
    return out


def review_candidate_pairs(pairs: list, *, repo: str, repo_path: Path,
                           merged_db: Path, base_rev: str, head_rev: str,
                           head_tree: Path | str | None,
                           base_tree: Path | str | None,
                           run_dir: Path, ceiling_usd: float,
                           assess_fn: Callable | None = None,
                           edit_fn: Callable | None = None,
                           on_pair: Callable | None = None,
                           is_stale: Callable[[], bool] | None = None,
                           known_published_keys=None,
                           pr: Any = None) -> dict:
    """Terra-propose / Luna-edit over directly-retrieved pairs.

    SEMDUP-08 integrated stage: pairs come from deterministic retrieval
    (`oai_dupe.retrieve_pairs`), NOT from M11/Gemma records. No Gemma
    stage is invoked anywhere on this path; Terra and Luna are resolved
    via the standard product backends. Every failure degrades to a
    recorded row; this function never raises.

    `on_pair` (optional) receives each judged row right after it is
    recorded, so the caller can persist verdicts incrementally.

    `known_published_keys` (optional) maps unordered pair keys to the
    kept direction for already-established published verdicts
    (same-content continuation/cache hits). A reverse pair whose key
    is present is skipped before examination (MIRROR-01); nothing is
    ever skipped on a mere rejection.

    Returns {"ok", "models", "pairs", "published", "suppressed",
    "skipped", "spend_usd", "spend_unknown", "wall_s", "stage"/"error"}.
    """
    from types import SimpleNamespace
    t0 = time.monotonic()
    outcome: dict[str, Any] = {
        "enabled": True, "ok": False, "version": EDITORIAL_VERSION,
        "head_rev": head_rev, "base_rev": base_rev,
        "pairs": [], "published": [], "suppressed": [],
        "skipped": [], "spend_usd": 0.0, "wall_s": 0.0,
        "preflight_usd": None, "mirror_skipped": 0,
        "mirror_saved_usd_est": None, "mirror_saved_wall_s_est": None,
        "scaffold_skipped": 0, "scaffold_saved_usd_est": None,
        "scaffold_saved_wall_s_est": None,
    }
    try:
        from codatiel_i2 import providers as P
        from codatiel_i2 import runtime_sdk as R
        from codatiel_i2.core.spend import SpendLedger
        from codatiel_i2.semdup_dev import editor as ED
        from codatiel_i2.semdup_dev import oai_dupe as OD
    except Exception as exc:  # noqa: BLE001 - stack unavailable
        outcome["ok"] = False
        outcome["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    if head_tree is None or base_tree is None:
        outcome["ok"] = True
        outcome["stage"] = "no_head_tree"
        outcome["skipped"] = [
            {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                     f"{p['counterpart']['path']}::"
                     f"{p['counterpart']['symbol']}",
             "reason": "head/base tree unavailable; candidates recorded, "
                       "no inference attempted"} for p in pairs]
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    try:
        terra_res = P.resolve_backend("terra")
        luna_res = P.resolve_backend("luna")
    except Exception as exc:  # noqa: BLE001 - no credentials => no spend
        outcome["ok"] = True
        outcome["stage"] = "backends_unavailable"
        outcome["skipped"] = [
            {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                     f"{p['counterpart']['path']}::"
                     f"{p['counterpart']['symbol']}",
             "reason": f"model backend unavailable: {exc}; no inference "
                       "attempted"} for p in pairs]
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    terra_model_id, terra_settings = P.build_model("terra")
    luna_model_id, luna_settings = P.build_model("luna")
    half = max(ceiling_usd / 2.0, 0.0)
    # Shared allocation experiment (flag-gated, default off): one shared
    # cap C with a dynamic editorial reserve instead of the fixed split.
    # Off path below is byte-identical to the historical behavior.
    shared = new_shared_state() if shared_budget_enabled() else None
    if shared is None:
        # Each model ledger is capped at half the delta ceiling so the
        # two together can never exceed ceiling_usd (preflight estimates
        # stop well short of the ledger cap; the cap is the backstop).
        terra_ledger = SpendLedger(rate=terra_res["rate_usd_per_mtok"],
                                   rate_status=terra_res["rate_status"],
                                   ceiling_usd=half)
        luna_ledger = SpendLedger(rate=luna_res["rate_usd_per_mtok"],
                                  rate_status=luna_res["rate_status"],
                                  ceiling_usd=half)
    else:
        # Hard backstop = the remainder for both; the reserve-aware
        # admission below is the real guard, the ledgers only backstop.
        terra_ledger = SpendLedger(rate=terra_res["rate_usd_per_mtok"],
                                   rate_status=terra_res["rate_status"],
                                   ceiling_usd=ceiling_usd)
        luna_ledger = SpendLedger(rate=luna_res["rate_usd_per_mtok"],
                                  rate_status=luna_res["rate_status"],
                                  ceiling_usd=ceiling_usd)
    try:
        run_dir = Path(run_dir)
        terra_ledger.attach_sink(run_dir / "spend_semdup_terra.jsonl")
        luna_ledger.attach_sink(run_dir / "spend_semdup_luna.jsonl")
    except Exception:  # noqa: BLE001 - sinks are best-effort
        pass
    outcome["models"] = {
        "terra": {"model": terra_res.get("model"),
                  "provider": terra_res.get("provider"),
                  "instructions_id": OD.instructions_id(
                      "terra", OD.TERRA_INSTRUCTIONS)},
        "luna": {"model": luna_res.get("model"),
                 "provider": luna_res.get("provider"),
                 "instructions_id": ED.editor_instructions_id()},
    }
    # SIGNAL-01 B1: every pair carries a stable correlation id + rank
    # (idempotent: stage stamps first; direct callers get stamped here).
    try:
        from codatiel_i2.semdup_dev import pairing as _PA
        _PA.stamp_pairs(pairs, repo=repo, pr=pr, base_rev=base_rev,
                        head_rev=head_rev)
    except Exception:
        pass
    # SIGNAL-01 B3: immutable pricing identity for offline repricing.
    _write_rate_snapshot(run_dir, terra_res=terra_res, luna_res=luna_res,
                         terra_model=str(terra_model_id),
                         luna_model=str(luna_model_id))
    cfg = SimpleNamespace(repos=[repo])
    assess = assess_fn or OD.assess_pair
    edit = edit_fn or ED.edit_pair
    judged_preflight = _preflight_usd(pairs, merged_db, terra_res,
                                       luna_res)
    # MIRROR-01: first-published-wins. Seeds are same-revision
    # continuation verdicts; the loop adds each published twin. Skips
    # fire only on published verdicts, never on rejection.
    try:
        seeds = dict(known_published_keys or {})
    except (TypeError, ValueError):
        seeds = {k: "" for k in (known_published_keys or ())}
    published_keys: dict[str, str] = seeds
    outcome["mirror_skipped"] = 0
    for idx, pair in enumerate(pairs):
        # COST-01B: check live head before each additional paid assessment
        if is_stale is not None:
            try:
                if is_stale():
                    # Remaining pairs unexamined due to staleness
                    for remaining in pairs[idx:]:
                        skipped = {
                            "unit": f"{remaining['target']['path']}::{remaining['target']['symbol']} -> "
                                     f"{remaining['counterpart']['path']}::{remaining['counterpart']['symbol']}",
                            "reason": "stale_superseded",
                            "detail": "target head no longer current during duplication; no inference issued",
                        }
                        outcome["skipped"].append(skipped)
                        # Record a suppressed row for provenance (no inference)
                        outcome["pairs"].append({
                            "target": remaining["target"],
                            "counterpart": remaining["counterpart"],
                            "head_rev": head_rev,
                            "base_rev": base_rev,
                            "pair_id": (remaining if isinstance(
                                remaining, dict) else {}).get("pair_id"),
                            "pair_rank": (remaining if isinstance(
                                remaining, dict) else {}).get("pair_rank"),
                            "reason": "stale_superseded",
                            "detail": "stale head; skipped before inference",
                            "path": "suppressed",
                            "completed": False, "budget_refused": False,
                            "terra_completed": False, "luna_completed": None,
                        })
                    break
            except Exception:
                pass
        skipped, mkey = _mirror_preskip(
            pair, head_rev=head_rev, base_rev=base_rev,
            published_keys=published_keys, outcome=outcome,
            on_pair=on_pair)
        if skipped:
            continue
        if shared is not None:
            # Reserve-aware loop bounds (F2+F4): each ledger is capped so
            # the two TOGETHER can never exceed C, whatever the actuals
            # do — bounded unknown-usage reserves count against the caps.
            # Terra may fill everything above the reserve; Luna may use
            # anything Terra leaves. Admission (estimate-based) decides
            # what starts; these caps bound what runs.
            try:
                st = _spent(terra_ledger)
                sl = _spent(luna_ledger)
                b_t = (terra_ledger.bounded_reserve_usd()
                       if st is not None else None)
                b_l = (luna_ledger.bounded_reserve_usd()
                       if sl is not None else None)
                if (st is None or sl is None or b_t is None
                        or b_l is None):
                    raise ValueError("unaccountable spend")
                _r = editorial_reserve_usd(
                    ceiling_usd, shared.get("outstanding", 0),
                    shared_mean_edit_usd(shared))
                terra_ledger.ceiling_usd = max(
                    st, float(ceiling_usd) - _r - sl - b_t - b_l)
                luna_ledger.ceiling_usd = max(
                    sl, float(ceiling_usd) - st - b_t - b_l)
            except Exception:
                try:
                    # Fail closed (F8): freeze both loops at confirmed spend.
                    _st = _spent(terra_ledger)
                    terra_ledger.ceiling_usd = (
                        _st if _st is not None else 0.0)
                    _sl = _spent(luna_ledger)
                    luna_ledger.ceiling_usd = (
                        _sl if _sl is not None else 0.0)
                except Exception:
                    pass
        terra_before = _ledger_order(terra_ledger)
        luna_before = _ledger_order(luna_ledger)
        try:
            row = _review_pair(
                pair, cfg=cfg, repo_path=repo_path, merged_db=merged_db,
                base_rev=base_rev, head_rev=head_rev, head_tree=head_tree,
                base_tree=base_tree, terra_res=terra_res, luna_res=luna_res,
                terra_model_id=terra_model_id, terra_settings=terra_settings,
                luna_model_id=luna_model_id, luna_settings=luna_settings,
                terra_ledger=terra_ledger, luna_ledger=luna_ledger,
                ceiling_usd=ceiling_usd, half=half, assess=assess, edit=edit,
                OD=OD, ED=ED, R=R, shared=shared)
        except Exception:
            _append_pair_telemetry(
                run_dir, _telemetry_for_interrupted(
                    pair, repo=repo, pr=pr,
                    base_rev=base_rev, head_rev=head_rev,
                    terra_ledger=terra_ledger, luna_ledger=luna_ledger,
                    models=outcome.get("models"),
                    terra_before=terra_before, luna_before=luna_before))
            raise
        # SIGNAL-01 B2/B3: crash-durable per-pair economics, appended
        # as each examination completes (never raises, never blocks).
        try:
            _append_pair_telemetry(
                run_dir, _pair_telemetry_row(
                    row, repo=repo, pr=pr, terra_ledger=terra_ledger,
                    luna_ledger=luna_ledger, models=outcome.get("models")))
        except Exception:
            pass
        _record_judged(outcome, row, mkey, published_keys, on_pair=on_pair)
    spent_t, spent_l = _spent(terra_ledger), _spent(luna_ledger)
    # Per-model spend from the priced ledger results (persisted with the
    # stage outcome). None means unknown — never represented as $0.
    # The aggregate is a confirmed lower bound only when both sides are
    # known; otherwise it is None with spend_unknown True.
    outcome["spend_terra_usd"] = spent_t
    outcome["spend_luna_usd"] = spent_l
    outcome["spend_terra_unknown"] = spent_t is None
    outcome["spend_luna_unknown"] = spent_l is None
    if spent_t is None or spent_l is None:
        outcome["spend_usd"] = (None if (spent_t is None and spent_l is None)
                                else round((spent_t or 0.0) + (spent_l or 0.0), 6))
    else:
        outcome["spend_usd"] = round(spent_t + spent_l, 6)
    outcome["spend_unknown"] = spent_t is None or spent_l is None
    outcome["preflight_usd"] = judged_preflight
    _record_preskip_savings(outcome, spent_t, spent_l, t0)
    outcome["budget_refusals"] = summarize_budget_refusals(
        outcome.get("pairs"))
    outcome["ok"] = True
    outcome["wall_s"] = round(time.monotonic() - t0, 1)
    return outcome
