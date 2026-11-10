"""Integrated semantic-duplication stage (SEMDUP-08, DEV-only).

Runs INSIDE the normal event-pipeline review, after the standard
review completes: deterministic retrieval candidates flow DIRECTLY
into Terra (propose) / Luna (KEEP/REVISE/DROP). No Gemma M11 stage is
invoked anywhere on this path; Terra/Luna resolve via the standard
product backends.

Rules:
- Prospective-only: invoked only from the pipeline's normal review
  flow for the pinned base..head range. Keeps no independent cursor,
  performs no backfill, never delays or fails the standard review.
- Never misrepresents inability as a clean scan: when inference is
  impossible the status is "unavailable"/"degraded" with an explicit
  reason, never "ok with zero findings".
- Bounded: at most `max_targets` changed units examined, at most
  `max_pairs` candidate pairs per delta, at most
  PR_FINDING_CAP approved findings per review email, spend ceiling
  enforced before every model call.
- No GitHub comments. Approved findings merge into the single
  consolidated digest email via `semdup.json` in the run dir.
- This module never raises: every failure becomes a recorded status.
  Top-level imports are stdlib-only so the event pipeline (and the
  production image, which lacks the embedding stack) imports safely;
  all semdup machinery is imported lazily inside `run_stage`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

STAGE_VERSION = "semdup-stage-0.1"
PR_FINDING_CAP = 2
DEFAULT_MAX_TARGETS = 8
DEFAULT_MAX_PAIRS_PER_TARGET = 2
DEFAULT_MAX_PAIRS_PER_DELTA = 4
DEFAULT_CEILING_USD = 0.20
DEDUP_LINE_WINDOW = 5


def run_stage(*, repo_path: Path, repo: str, pr: int,
              base_rev: str, head_rev: str, run_dir: Path | str,
              state_dir: Path | str,
              max_targets: int = DEFAULT_MAX_TARGETS,
              max_pairs_per_target: int = DEFAULT_MAX_PAIRS_PER_TARGET,
              max_pairs: int = DEFAULT_MAX_PAIRS_PER_DELTA,
              ceiling_usd: float = DEFAULT_CEILING_USD,
              allow_embeddings: bool = True,
              assess_fn: Callable | None = None,
              edit_fn: Callable | None = None) -> dict:
    """Run the integrated duplication stage for one pinned base..head.

    Writes `semdup.json` into run_dir. Never raises.
    """
    t0 = time.monotonic()
    run_dir = Path(run_dir)
    state_dir = Path(state_dir)
    outcome: dict[str, Any] = {
        "version": STAGE_VERSION, "status": "unavailable",
        "repo": repo, "pr": pr, "base_rev": base_rev,
        "head_rev": head_rev, "published": [], "suppressed": [],
        "skipped": [], "targets": [], "coverage": "unknown",
        "spend_usd": 0.0, "wall_s": 0.0, "reason": "",
    }
    try:
        return _run(repo_path=Path(repo_path), repo=repo, pr=pr,
                     base_rev=base_rev, head_rev=head_rev, run_dir=run_dir,
                     state_dir=state_dir, max_targets=max_targets,
                     max_pairs_per_target=max_pairs_per_target,
                     max_pairs=max_pairs, ceiling_usd=ceiling_usd,
                     allow_embeddings=allow_embeddings,
                     assess_fn=assess_fn, edit_fn=edit_fn,
                     outcome=outcome, t0=t0)
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        outcome["status"] = "unavailable"
        outcome["reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome


def _run(*, repo_path, repo, pr, base_rev, head_rev, run_dir,
         state_dir, max_targets, max_pairs_per_target, max_pairs,
         ceiling_usd, allow_embeddings, assess_fn, edit_fn,
         outcome, t0) -> dict:
    try:
        from codatiel_i2.semdup_dev import delta as D
        from codatiel_i2.semdup_dev import editorial_shadow as ES
        from codatiel_i2.semdup_dev import index_state as IX
        from codatiel_i2.semdup_dev import merged_view as MV
        from codatiel_i2.semdup_dev import oai_dupe as OD
        from codatiel_i2.semdup_dev import worker as WK
    except Exception as exc:  # noqa: BLE001 - stack (incl. torch /
        # tree-sitter) unavailable: honest unavailable, never a fake zero
        outcome["status"] = "unavailable"
        outcome["reason"] = ("semdup stack unavailable: "
                             f"{type(exc).__name__}: {str(exc)[:200]}")
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome

    base_st = IX.base_index_status(state_dir, repo, base_rev)
    if not base_st["present"]:
        built = _ensure_base(IX, repo_path, repo, base_rev, state_dir,
                             allow_embeddings)
        if not built.get("present"):
            outcome["status"] = "unavailable"
            outcome["reason"] = ("base index unavailable: "
                                 f"{built.get('reason', '')[:200]}")
            outcome["wall_s"] = round(time.monotonic() - t0, 1)
            _persist(run_dir, outcome)
            return outcome
        base_st = built
    base_db = IX.base_paths(state_dir, repo, base_rev)["db"]
    ov = IX.build_overlay(repo_path, repo, pr, base_rev, head_rev,
                          state_dir, base_db,
                          allow_embeddings=allow_embeddings)
    if not ov.get("ok"):
        outcome["status"] = "unavailable"
        outcome["reason"] = f"overlay unavailable: {ov.get('reason', '')[:200]}"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome
    merged = MV.build_merged_view(
        base_db, Path(ov["overlay_dir"]),
        run_dir / "semdup_view.sqlite")
    if not merged.get("ok"):
        outcome["status"] = "unavailable"
        outcome["reason"] = (f"head view unavailable: "
                             f"{merged.get('reason', '')[:200]}")
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome
    outcome["coverage"] = merged.get("retrieval_coverage", "unknown")

    # Targets: added/changed units restricted to the reviewed range,
    # mirroring the worker's incremental filter (first review: the
    # range IS base..head, so the restriction is a no-op).
    ch = WK.changed_files(repo_path, base_rev, head_rev)
    if not ch.get("ok"):
        outcome["status"] = "degraded"
        outcome["reason"] = (f"changed set indeterminable: "
                             f"{ch.get('reason', '')[:200]}; no rescan")
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome
    if ch.get("empty"):
        outcome["status"] = "ok"
        outcome["reason"] = "no changes in range; nothing to examine"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome
    classification = D.classify_delta(base_db, Path(merged["db"]))
    selected = WK._select_targets(classification, max_targets)
    changed_set = set(ch.get("files") or [])
    targets = [t for t in selected["selected"]
               if t["path"] in changed_set]
    outcome["targets"] = [
        {"path": t["path"], "symbol": t["symbol"],
         "kind": WK._kind_of(classification, t)} for t in targets]
    if not targets:
        outcome["status"] = "ok"
        outcome["reason"] = "no added/changed units in reviewed range"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome

    head_tree = WK._materialize_head(repo_path, head_rev)
    base_tree = WK._materialize_head(repo_path, base_rev)
    try:
        pairs: list[dict] = []
        retrieval_failures = 0
        for target in targets:
            if len(pairs) >= max_pairs:
                outcome["skipped"].append(
                    {"unit": f"{target['path']}::{target['symbol']}",
                     "reason": "delta pair budget exhausted"})
                continue
            try:
                ret = OD.retrieve_pairs(
                    Path(merged["db"]), target, merged.get("emb"),
                    max_pairs_per_target)
            except Exception as exc:  # noqa: BLE001 - per-target degrade
                retrieval_failures += 1
                outcome["skipped"].append(
                    {"unit": f"{target['path']}::{target['symbol']}",
                     "reason": "retrieval failed",
                     "detail": f"{type(exc).__name__}: {str(exc)[:160]}"})
                continue
            kind = WK._kind_of(classification, target)
            for p in ret.get("pairs", []):
                if len(pairs) >= max_pairs:
                    break
                pairs.append({"target": dict(target),
                              "counterpart": dict(p),
                              "classification": kind})
        outcome["pairs_examined"] = len(pairs)
        outcome["retrieval_failures"] = retrieval_failures
        if head_tree is None or base_tree is None:
            outcome["status"] = "degraded"
            outcome["reason"] = ("head/base tree unavailable; candidates "
                                 "recorded, no inference attempted")
            outcome["skipped"].extend([
                {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                         f"{p['counterpart']['path']}::"
                         f"{p['counterpart']['symbol']}",
                 "reason": "tree unavailable"} for p in pairs])
            outcome["wall_s"] = round(time.monotonic() - t0, 1)
            _persist(run_dir, outcome)
            return outcome
        judged = ES.review_candidate_pairs(
            pairs, repo=repo, repo_path=repo_path,
            merged_db=Path(merged["db"]), base_rev=base_rev,
            head_rev=head_rev, head_tree=head_tree, base_tree=base_tree,
            run_dir=run_dir, ceiling_usd=ceiling_usd,
            assess_fn=assess_fn, edit_fn=edit_fn)
    finally:
        import shutil as _shutil
        for tree in (head_tree, base_tree):
            if tree is not None:
                _shutil.rmtree(str(Path(tree).parent), ignore_errors=True)
    outcome["models"] = judged.get("models", {})
    outcome["spend_usd"] = judged.get("spend_usd", 0.0)
    outcome["spend_unknown"] = judged.get("spend_unknown", False)
    outcome["skipped"].extend(judged.get("skipped", []))
    if not judged.get("ok") or judged.get("stage") in (
            "backends_unavailable",):
        # No inference was possible: an explicit unavailable, never a
        # clean-looking zero. (backends_unavailable reports ok=True
        # with zero pairs; that is inability, not a clean scan.)
        outcome["status"] = "unavailable"
        outcome["reason"] = judged.get("error") or (
            f"{judged.get('stage', 'judgment failed')}: "
            f"{(judged.get('skipped') or [{}])[0].get('reason', '')[:200]}")
        outcome["suppressed"] = judged.get("suppressed", [])
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome
    published = list(judged.get("published", []))
    suppressed = list(judged.get("suppressed", []))
    published, capped = _apply_pr_cap(published)
    suppressed.extend(capped)
    # Any retrieval failure means the delta was only partially examined:
    # degraded, never a clean-looking zero. Zero pairs with zero
    # failures is a genuine clean outcome (coverage stated above).
    if retrieval_failures:
        outcome["status"] = "degraded"
        outcome["reason"] = (f"examined {len(targets)} targets, "
                             f"{len(judged.get('pairs', []))} pairs; "
                             f"{len(published)} approved; "
                             f"{retrieval_failures} target(s) failed "
                             "retrieval and were not examined")
    else:
        outcome["status"] = "ok"
        outcome["reason"] = (f"examined {len(targets)} targets, "
                             f"{len(judged.get('pairs', []))} pairs; "
                             f"{len(published)} approved")
    # Dedup against the ordinary review's advisory anchors.
    published, dups = _dedup(run_dir, published)
    suppressed.extend(dups)
    outcome["published"] = published
    outcome["suppressed"] = suppressed
    outcome["wall_s"] = round(time.monotonic() - t0, 1)
    _persist(run_dir, outcome)
    return outcome


def _ensure_base(IX, repo_path, repo, rev, state_dir,
                 allow_embeddings) -> dict:
    """Synchronous base build (DEV integrated path runs it inline).

    Unlike the report-only worker's async schedule, the integrated
    stage builds inline so the finding can join the consolidated
    email; failure is an explicit unavailable status, never silent.
    """
    try:
        return IX.ensure_base_index(repo_path, repo, rev, state_dir,
                                    allow_embeddings=allow_embeddings)
    except Exception as exc:  # noqa: BLE001
        return {"present": False,
                "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _apply_pr_cap(published: list) -> tuple[list, list]:
    """Enforce at most PR_FINDING_CAP approved findings per review email.

    One stage run covers one reviewed delta; each incremental push is
    its own email, so a PR can accumulate more than the cap across
    pushes. Returns (kept, capped) where capped rows carry the reason.
    """
    kept = list(published[:PR_FINDING_CAP])
    capped = []
    for row in published[PR_FINDING_CAP:]:
        capped.append({
            "target": row.get("target"),
            "counterpart": row.get("counterpart"),
            "head_rev": row.get("head_rev"),
            "reason": "pr_cap",
            "detail": (f"approved but over the {PR_FINDING_CAP}-per-PR "
                       "cap; recorded, not emailed"),
            "terra": {}, "anchors": row.get("anchors", {})})
    return kept, capped


def _dedup(run_dir: Path, published: list) -> tuple[list, list]:
    """Suppress semdup findings overlapping the ordinary review.

    Same-file anchors within DEDUP_LINE_WINDOW lines are the same
    observation: the ordinary review owns it. Returns (kept, dups).
    """
    try:
        shadow = json.loads((run_dir / "shadow_result.json")
                            .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no normal findings to dedup vs
        return published, []
    anchors: list[tuple[str, int, str]] = []
    for f in shadow.get("advisory", []) or []:
        if not isinstance(f, dict):
            continue
        a = f.get("changed_anchor") or {}
        if isinstance(a, dict) and a.get("path"):
            try:
                anchors.append((str(a["path"]),
                                int(a.get("line_start") or 0),
                                str(f.get("finding_id") or "")))
            except (TypeError, ValueError):
                continue
    if not anchors:
        return published, []
    kept, dups = [], []
    for row in published:
        ta = ((row.get("anchors") or {}).get("target") or {})
        tpath, tline = str(ta.get("path") or ""), ta.get("start_line")
        hit = ""
        if tpath and isinstance(tline, int):
            for apath, aline, fid in anchors:
                if apath == tpath and abs(aline - tline) <= DEDUP_LINE_WINDOW:
                    hit = fid
                    break
        if hit:
            dups.append({**row, "path": "suppressed",
                         "reason": "duplicate_of_review",
                         "detail": (f"overlaps ordinary review finding "
                                    f"{hit}; ordinary review owns it")})
        else:
            kept.append(row)
    return kept, dups


def _persist(run_dir: Path, outcome: dict) -> None:
    try:
        (run_dir / "semdup.json").write_text(
            json.dumps(outcome, indent=1, default=str))
    except Exception:  # noqa: BLE001 - best effort; summary returned anyway
        pass
