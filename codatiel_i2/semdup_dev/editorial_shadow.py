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

import time
from pathlib import Path
from typing import Any, Callable

EDITORIAL_VERSION = "semdup-editorial-shadow-0.1"
DEFAULT_MAX_PAIRS = 4
DEFAULT_CEILING_USD = 0.20
ELIGIBLE_RECORD_STATUS = ("finding", "investigate")


def _is_oracle(path: str) -> bool:
    return path == "oracles" or path.startswith("oracles/")


def _is_fragment(symbol: str) -> bool:
    return "#region" in (symbol or "")


def _split_unit(ref: str) -> tuple[str, str] | None:
    if "::" not in ref:
        return None
    path, _, symbol = ref.partition("::")
    path, symbol = path.strip(), symbol.strip()
    if not path or not symbol:
        return None
    return path, symbol


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


def _spent(ledger) -> float | None:
    try:
        return ledger.confirmed_usd()
    except Exception:  # noqa: BLE001 - spend unknown => stop spending
        return None


def run_editorial(*, cfg, repo_path: Path, merged_db: Path,
                  base_rev: str, head_rev: str, records: list,
                  run_dir: Path, head_tree: Path | None,
                  base_tree: Path | None, max_pairs: int | None = None,
                  ceiling_usd: float | None = None,
                  assess_fn: Callable | None = None,
                  edit_fn: Callable | None = None) -> dict:
    """Run the editorial shadow pass for one delta. Never raises."""
    t0 = time.monotonic()
    outcome: dict[str, Any] = {
        "enabled": True, "ok": False, "version": EDITORIAL_VERSION,
        "head_rev": head_rev, "base_rev": base_rev,
        "pairs": [], "published": [], "suppressed": [],
        "skipped": [], "spend_usd": 0.0, "wall_s": 0.0,
    }
    try:
        return _run(cfg=cfg, repo_path=repo_path, merged_db=merged_db,
                    base_rev=base_rev, head_rev=head_rev, records=records,
                    run_dir=run_dir, head_tree=head_tree,
                    base_tree=base_tree, max_pairs=max_pairs,
                    ceiling_usd=ceiling_usd, assess_fn=assess_fn,
                    edit_fn=edit_fn, outcome=outcome, t0=t0)
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        outcome["ok"] = False
        outcome["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome


def _run(*, cfg, repo_path, merged_db, base_rev, head_rev, records,
         run_dir, head_tree, base_tree, max_pairs, ceiling_usd,
         assess_fn, edit_fn, outcome, t0) -> dict:
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
    for pair in pairs:
        row = _review_pair(
            pair, cfg=cfg, repo_path=repo_path, merged_db=merged_db,
            base_rev=base_rev, head_rev=head_rev, head_tree=head_tree,
            base_tree=base_tree, terra_res=terra_res, luna_res=luna_res,
            terra_model_id=terra_model_id, terra_settings=terra_settings,
            luna_model_id=luna_model_id, luna_settings=luna_settings,
            terra_ledger=terra_ledger, luna_ledger=luna_ledger,
            ceiling_usd=ceiling_usd, half=half, assess=assess, edit=edit,
            OD=OD, ED=ED, R=R)
        outcome["pairs"].append(row)
        if row["path"] == "published":
            outcome["published"].append(_public_row(row))
        else:
            outcome["suppressed"].append(_suppressed_row(row))
    spent_t, spent_l = _spent(terra_ledger), _spent(luna_ledger)
    outcome["spend_usd"] = round(
        (spent_t or 0.0) + (spent_l or 0.0), 6)
    outcome["spend_unknown"] = spent_t is None or spent_l is None
    outcome["ok"] = True
    outcome["wall_s"] = round(time.monotonic() - t0, 1)
    return outcome


def _review_pair(pair, *, cfg, repo_path, merged_db, base_rev, head_rev,
                 head_tree, base_tree, terra_res, luna_res,
                 terra_model_id, terra_settings, luna_model_id,
                 luna_settings, terra_ledger, luna_ledger, ceiling_usd,
                 half, assess, edit, OD, ED, R) -> dict:
    pt0 = time.monotonic()
    row = _review_inner(
        pair, cfg=cfg, repo_path=repo_path, merged_db=merged_db,
        base_rev=base_rev, head_rev=head_rev, head_tree=head_tree,
        base_tree=base_tree, terra_res=terra_res, luna_res=luna_res,
        terra_model_id=terra_model_id, terra_settings=terra_settings,
        luna_model_id=luna_model_id, luna_settings=luna_settings,
        terra_ledger=terra_ledger, luna_ledger=luna_ledger,
        ceiling_usd=ceiling_usd, half=half, assess=assess, edit=edit,
        OD=OD, ED=ED, R=R)
    row["wall_s"] = round(time.monotonic() - pt0, 1)
    return row


def _review_inner(pair, *, cfg, repo_path, merged_db, base_rev, head_rev,
                  head_tree, base_tree, terra_res, luna_res,
                  terra_model_id, terra_settings, luna_model_id,
                  luna_settings, terra_ledger, luna_ledger, ceiling_usd,
                  half, assess, edit, OD, ED, R) -> dict:
    row: dict[str, Any] = {
        "target": pair["target"], "counterpart": pair["counterpart"],
        "record_status": pair.get("record_status"),
        "classification": pair.get("classification"),
        "head_rev": head_rev, "base_rev": base_rev,
    }
    ev = ED.build_evidence_package(
        merged_db, repo_path, head_rev,
        pair["target"], pair["counterpart"])
    if not ev["ok"]:
        row.update({"path": "suppressed",
                    "reason": "evidence_failed",
                    "detail": f"{ev.get('code')}: {ev.get('detail', '')}"})
        return row
    row["anchors"] = {
        "target": {k: ev["target"][k] for k in
                   ("path", "symbol", "start_line", "end_line", "sha256")},
        "counterpart": {k: ev["counterpart"][k] for k in
                        ("path", "symbol", "start_line", "end_line",
                         "sha256")},
    }
    pre = ED.wrapper_prefilter(ev["target"]["text"],
                               ev["counterpart"]["symbol"])
    if pre["wrapper"]:
        row.update({"path": "suppressed", "reason": "deterministic_drop",
                    "detail": pre["detail"]})
        return row
    terra_ctx = R.ReviewContext(
        repo=cfg.repos[0] if cfg.repos else "dev", base_sha=base_rev,
        head_sha=head_rev, base_dir=base_tree, head_dir=head_tree,
        diff_repo=repo_path, tool_budget=OD.TERRA_TOOL_BUDGET)
    est = OD.estimate_call_usd(len(OD.TERRA_INSTRUCTIONS) + 12000,
                               terra_res["rate_usd_per_mtok"])
    s = _spent(terra_ledger)
    if s is None or (est is not None and s + est > half):
        row.update({"path": "suppressed", "reason": "budget_stop",
                    "detail": "terra estimate exceeds half-ceiling"})
        return row
    ta = assess(
        evidence={"target": ev["target"],
                  "counterpart": ev["counterpart"],
                  "pair_sha256": "editorial-shadow"},
        head_rev=head_rev,
        target_kind=pair.get("classification") or "introduced",
        tools=OD.scoped_tools(terra_ctx), model=terra_model_id,
        model_settings=terra_settings, ledger=terra_ledger, ctx=terra_ctx)
    row["terra_tool_calls"] = len(
        ((ta.get("transcript") or {}).get("tool_calls", [])))
    if not ta.get("ok") or not (ta.get("terra") or {}).get("overlap"):
        row.update({"path": "suppressed", "reason": "terra_no_lead",
                    "detail": ta.get("error", "")})
        return row
    terra = ta["terra"]
    row["terra"] = {k: terra.get(k) for k in
                    ("overlap", "relationship", "opportunity",
                     "shared_component", "differences")}
    if terra.get("opportunity", "none") == "none":
        row.update({"path": "suppressed",
                    "reason": "terra_no_opportunity"})
        return row
    proposed = ED.propose_comment(terra, ev)
    row["proposed_comment"] = proposed
    luna_ctx = R.ReviewContext(
        repo=cfg.repos[0] if cfg.repos else "dev", base_sha=base_rev,
        head_sha=head_rev, base_dir=base_tree, head_dir=head_tree,
        diff_repo=repo_path, tool_budget=8)
    est_l = OD.estimate_call_usd(len(ED.EDITOR_INSTRUCTIONS) + 12000,
                                 luna_res["rate_usd_per_mtok"])
    sl = _spent(luna_ledger)
    if sl is None or (est_l is not None and sl + est_l > half):
        row.update({"path": "suppressed", "reason": "budget_stop",
                    "detail": "luna estimate exceeds half-ceiling"})
        return row
    lt0 = time.monotonic()
    luna = edit(
        package=ev, proposed_comment=proposed,
        tools=R.build_i2_tools(luna_ctx), model=luna_model_id,
        model_settings=luna_settings, ledger=luna_ledger, ctx=luna_ctx)
    row["luna_wall_s"] = round(time.monotonic() - lt0, 1)
    row["luna_tool_calls"] = len(
        ((luna.get("transcript") or {}).get("tool_calls", [])))
    row["editor"] = luna.get("editor")
    row["editor_error"] = luna.get("error", "")
    if not luna.get("ok"):
        row.update({"path": "suppressed",
                    "reason": "editor_unpublishable"})
        return row
    decision = (luna.get("editor") or {}).get("decision")
    if decision not in ("KEEP", "REVISE"):
        row.update({"path": "suppressed", "reason": "editor_drop",
                    "detail": ((luna.get("editor") or {}).get("reason", "")
                               or "")[:600]})
        return row
    row.update({"path": "published", "decision": decision,
                "revised_comment": (luna.get("editor") or {}).get(
                    "revised_comment", ""),
                "reason": ((luna.get("editor") or {}).get("reason", "")
                           or "")[:800]})
    return row


def _public_row(row: dict) -> dict:
    return {
        "target": row["target"], "counterpart": row["counterpart"],
        "head_rev": row.get("head_rev"),
        "decision": row.get("decision"),
        "revised_comment": row.get("revised_comment", ""),
        "reason": row.get("reason", ""),
        "anchors": row.get("anchors", {}),
        "inference": {
            "terra_wall_s": row.get("terra_wall_s", 0.0),
            "terra_tool_calls": row.get("terra_tool_calls", 0),
            "luna_wall_s": row.get("luna_wall_s", 0.0),
            "luna_tool_calls": row.get("luna_tool_calls", 0),
        },
    }


def _suppressed_row(row: dict) -> dict:
    return {
        "target": row["target"], "counterpart": row["counterpart"],
        "head_rev": row.get("head_rev"),
        "reason": row.get("reason", ""),
        "detail": str(row.get("detail", "") or "")[:600],
        "terra": row.get("terra", {}),
        "anchors": row.get("anchors", {}),
    }
