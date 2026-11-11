"""Integrated semantic-duplication stage (COMPLETENESS-01, DEV-only).

Runs INSIDE the normal event-pipeline review, after the standard
review completes: deterministic retrieval candidates flow DIRECTLY
into Terra (propose) / Luna (KEEP/REVISE/DROP). No Gemma M11 stage is
invoked anywhere on this path; Terra/Luna resolve via the standard
product backends.

Rules:
- Prospective-only: invoked only from the pipeline's normal review
  flow for the pinned base..head range. Keeps no independent cursor,
  performs no backfill, never delays or fails the standard review.
- No artificial count limits: every eligible changed unit is retrieved
  and every retrieved pair is judged unless the whole-PR spending
  guard binds. An explicit max_pairs is a controlled-experiment limit
  only (None disables it).
- No numerical cap on validated findings: every Luna-approved pair is
  published (deduplicated against the ordinary review).
- Never misrepresents inability as a clean scan: when inference is
  impossible the status is "unavailable"/"degraded" with an explicit
  reason, never "ok with zero findings".
- Cost protection is the whole-PR ceiling (normal + semdup together),
  passed in as ceiling_usd: per-pair preflight estimates stop cleanly
  at the boundary, completed verdicts persist as continuation records
  so an authorized rerun never repeats paid work, and unexamined pairs
  are recorded with reasons.
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

STAGE_VERSION = "semdup-stage-0.2"
DEFAULT_MAX_PAIRS_PER_TARGET = 2
DEFAULT_CEILING_USD = 2.0
DEDUP_LINE_WINDOW = 5
CONTINUATION_EVERY = 10
# Deterministic pre-inference filters, mirroring the counterpart rules:
# oracle paths, region fragments and empty symbols never reach models.
FILTER_REASON = "deterministically filtered: oracle/fragment/empty unit"


def run_stage(*, repo_path: Path, repo: str, pr: int,
              base_rev: str, head_rev: str, run_dir: Path | str,
              state_dir: Path | str,
              max_pairs_per_target: int = DEFAULT_MAX_PAIRS_PER_TARGET,
              max_pairs: int | None = None,
              ceiling_usd: float = DEFAULT_CEILING_USD,
              allow_embeddings: bool = True,
              assess_fn: Callable | None = None,
              edit_fn: Callable | None = None) -> dict:
    """Run the integrated duplication stage for one pinned base..head.

    `max_pairs` None (normal operation) examines every retrieved pair
    until the whole-PR spending guard binds; a positive int is an
    explicit controlled-experiment limit only. Writes `semdup.json`
    into run_dir. Never raises.
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
        "pairs_total": 0, "pairs_reused": 0, "ceiling_usd": ceiling_usd,
    }
    try:
        return _run(repo_path=Path(repo_path), repo=repo, pr=pr,
                     base_rev=base_rev, head_rev=head_rev, run_dir=run_dir,
                     state_dir=state_dir,
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
         state_dir, max_pairs_per_target, max_pairs,
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
        outcome["coverage"] = "full"
        outcome["reason"] = "no changes in range; nothing to examine"
        outcome["coverage_detail"] = _empty_coverage_detail(merged)
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome
    classification = D.classify_delta(base_db, Path(merged["db"]))
    changed_set = set(ch.get("files") or [])
    eligible = _eligible_targets(classification, changed_set)
    targets, filtered = _filter_targets(eligible)
    for u in filtered:
        outcome["skipped"].append({"unit": u, "reason": FILTER_REASON})
    outcome["targets"] = [
        {"path": t["path"], "symbol": t["symbol"],
         "kind": WK._kind_of(classification, t)} for t in targets]
    if not targets:
        outcome["status"] = "ok"
        outcome["coverage"] = "full" if not [
            s for s in outcome["skipped"]
            if not str(s.get("reason", "")).startswith(
                "deterministically filtered")] else "partial"
        outcome["reason"] = (
            f"{len(eligible)} eligible unit(s); all deterministically "
            f"filtered ({FILTER_REASON}); nothing to infer")
        outcome["coverage_detail"] = _empty_coverage_detail(
            merged, skipped=list(outcome["skipped"]))
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome

    head_tree = WK._materialize_head(repo_path, head_rev)
    base_tree = WK._materialize_head(repo_path, base_rev)
    try:
        # Retrieval runs for EVERY eligible target (no count cap): it is
        # deterministic and local. Inference order below is by retrieval
        # channel agreement (multi-channel pairs first), never by
        # filename; the whole-PR spending guard bounds inference.
        pairs: list[dict] = []
        retrieval_failures = 0
        for target in targets:
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
                pairs.append({"target": dict(target),
                              "counterpart": dict(p),
                              "classification": kind,
                              "channels": list(p.get("via") or [])})
        pairs = _order_pairs(pairs)
        if max_pairs is not None and len(pairs) > max_pairs:
            # Explicit controlled-experiment limit only (None disables).
            cut = pairs[max_pairs:]
            pairs = pairs[:max_pairs]
            for p in cut:
                outcome["skipped"].append(
                    {"unit": (f"{p['target']['path']}::"
                              f"{p['target']['symbol']} -> "
                              f"{p['counterpart']['path']}::"
                              f"{p['counterpart']['symbol']}"),
                     "reason": (f"experimental pair budget "
                                f"(max_pairs={max_pairs}) exhausted")})
        outcome["pairs_examined"] = 0  # inference-attempted count, set below
        outcome["retrieval_failures"] = retrieval_failures
        outcome["pairs_total"] = len(pairs)
        if head_tree is None or base_tree is None:
            outcome["status"] = "degraded"
            outcome["coverage"] = "partial"
            outcome["reason"] = ("head/base tree unavailable; candidates "
                                 "recorded, no inference attempted")
            outcome["skipped"].extend([
                {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                         f"{p['counterpart']['path']}::"
                         f"{p['counterpart']['symbol']}",
                 "reason": "tree unavailable"} for p in pairs])
            outcome["coverage_detail"] = _empty_coverage_detail(
                merged, skipped=list(outcome["skipped"]))
            outcome["wall_s"] = round(time.monotonic() - t0, 1)
            _persist(run_dir, outcome)
            return outcome
        # Continuation: verdicts recorded for this exact delta are reused
        # at $0; only new pairs are judged. An authorized rerun (or a
        # higher ceiling) never repeats completed paid work.
        known = _load_continuation(state_dir, repo, pr, base_rev, head_rev)
        new_pairs, reused_rows = _partition_new(pairs, known)
        outcome["pairs_reused"] = len(reused_rows)
        # Incremental persist: every CONTINUATION_EVERY judged pairs
        # merge into the record, so even a killed run keeps completed
        # verdicts for the authorized rerun.
        cont_buf: list[dict] = []

        def _on_pair(row: dict) -> None:
            # Budget-stopped pairs hold no verdict (a higher ceiling may
            # still examine them) and are never persisted for reuse.
            if not isinstance(row, dict) or row.get("reason") == (
                    "budget_stop"):
                return
            cont_buf.append(row)
            if len(cont_buf) >= CONTINUATION_EVERY:
                _merge_continuation(state_dir, repo, pr, base_rev,
                                    head_rev, cont_buf)
                cont_buf.clear()

        judged = ES.review_candidate_pairs(
            new_pairs, repo=repo, repo_path=repo_path,
            merged_db=Path(merged["db"]), base_rev=base_rev,
            head_rev=head_rev, head_tree=head_tree, base_tree=base_tree,
            run_dir=run_dir, ceiling_usd=ceiling_usd,
            assess_fn=assess_fn, edit_fn=edit_fn,
            on_pair=_on_pair)
        if cont_buf:
            _merge_continuation(state_dir, repo, pr, base_rev, head_rev,
                                cont_buf)
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
    # Merge reused verdicts (prior paid work, $0 this run) with fresh
    # judgments. No numerical cap: every Luna-approved pair publishes.
    new_rows = [r for r in judged.get("pairs", []) if isinstance(r, dict)]
    published = list(judged.get("published", []))
    suppressed = list(judged.get("suppressed", []))
    for r in reused_rows:
        if not isinstance(r, dict):
            continue
        if r.get("path") == "published":
            published.append(ES._public_row(r))
        else:
            suppressed.append(ES._suppressed_row(r))
    # Mirror consolidation: when both directions of the same unordered
    # unit pair publish, keep the first (stronger retrieval signal) and
    # record the other as a mirror instead of emailing the same shared
    # code twice with opposite directions.
    published, mirrors = _dedup_mirrors(published)
    suppressed.extend(mirrors)
    # Honest coverage: four layers, each recorded. "full" requires every
    # eligible target retrieved, zero retrieval failures, and zero skips
    # beyond deterministic filtering. Anything less is "partial".
    examined_pairs = sum(1 for r in new_rows if r.get("assess_ran"))
    examined_targets = _targets_with_inference(new_rows)
    outcome["pairs_examined"] = examined_pairs
    guard_stops = [r for r in suppressed
                   if r.get("reason") == "budget_stop"]
    coverage_detail = {
        "index": {"base": "present", "overlay": "ok"},
        "retrieval": {"failures": retrieval_failures,
                      "coverage": merged.get("retrieval_coverage",
                                             "unknown")},
        "examination": {
            "targets_selected": len(targets),
            "targets_examined": examined_targets,
            "targets_selection_deferred": 0,
            "pairs_total": len(pairs),
            "pairs_examined": examined_pairs,
            "pairs_reused": len(reused_rows),
            "skipped": list(outcome["skipped"]),
        },
        "editorial": {"published": len(published),
                      "suppressed": len(suppressed),
                      "models": judged.get("models", {})},
    }
    outcome["coverage_detail"] = coverage_detail
    skip_summary = _skip_summary(outcome["skipped"])
    hard_skips = [s for s in outcome["skipped"]
                 if not str(s.get("reason", "")).startswith(
                     "deterministically filtered")]
    guard_note = (f"; {len(guard_stops)} pair(s) unexamined by the "
                  "budget guard" if guard_stops else "")
    reused_note = (f" (+{len(reused_rows)} reused prior verdicts)"
                   if reused_rows else "")
    # Any retrieval failure means the delta was only partially examined:
    # degraded, never a clean-looking zero.
    if retrieval_failures:
        outcome["status"] = "degraded"
        outcome["coverage"] = "partial"
        outcome["reason"] = (f"examined {examined_targets} targets, "
                             f"{examined_pairs} pairs{reused_note}; "
                             f"{len(published)} approved; "
                             f"{retrieval_failures} target(s) failed "
                             "retrieval and were not examined"
                             f"{skip_summary}")
    else:
        outcome["status"] = "ok"
        outcome["coverage"] = ("partial"
                               if (hard_skips or guard_stops) else "full")
        outcome["reason"] = (f"examined {examined_targets} targets, "
                             f"{examined_pairs} pairs{reused_note}; "
                             f"{len(published)} approved"
                             f"{skip_summary}{guard_note}")
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


def _dedup_mirrors(published: list) -> tuple[list, list]:
    """Collapse A->B / B->A double publications to the first row.

    Both directions describe the same shared code; emailing both is
    noise. Returns (kept, mirrors) where mirrors carry the kept
    direction for traceability.
    """
    seen: set[str] = set()
    kept, mirrors = [], []
    for row in published:
        t, c = row.get("target") or {}, row.get("counterpart") or {}
        a = f"{t.get('path')}::{t.get('symbol')}"
        b = f"{c.get('path')}::{c.get('symbol')}"
        key = "~~".join(sorted((a, b)))
        if key in seen:
            mirrors.append({**row, "path": "suppressed",
                            "reason": "mirror_of_published",
                            "detail": ("same unordered unit pair already "
                                       "published; opposite direction")})
            continue
        seen.add(key)
        kept.append(row)
    return kept, mirrors


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


def _skip_summary(skipped: list) -> str:
    """Compact '; K skipped (reasons)' suffix for the reason line."""
    if not skipped:
        return ""
    reasons = sorted({str(s.get("reason") or "") for s in skipped
                      if isinstance(s, dict)})
    return f"; {len(skipped)} skipped ({'; '.join(reasons)[:160]})"


def _empty_coverage_detail(merged: dict, deferred: int = 0,
                           skipped: list | None = None) -> dict:
    return {
        "index": {"base": "present", "overlay": "ok"},
        "retrieval": {"failures": 0,
                      "coverage": (merged or {}).get("retrieval_coverage",
                                                     "unknown")},
        "examination": {
            "targets_selected": 0, "targets_examined": 0,
            "targets_selection_deferred": deferred,
            "pairs_examined": 0, "skipped": skipped or [],
        },
        "editorial": {"published": 0, "suppressed": 0, "models": {}},
    }


def _pair_key(target: dict, counterpart: dict) -> str:
    return (f"{target.get('path')}::{target.get('symbol')}>>"
            f"{counterpart.get('path')}::{counterpart.get('symbol')}")


def _is_filtered_unit(path: str, symbol: str) -> bool:
    """Oracle paths, region fragments and empty symbols never reach
    models — mirroring the counterpart exclusion rules. Deterministic,
    filename-independent."""
    if not path or not (symbol or "").strip():
        return True
    if path == "oracles" or path.startswith("oracles/"):
        return True
    return "#region" in symbol


def _eligible_targets(classification: dict,
                      changed_set: set) -> list[dict]:
    """All added/changed (non-moved) units in the reviewed file set.

    No alphabetical truncation, no count: eligibility is the pinned
    diff, nothing else.
    """
    out = []
    for path, symbol in classification.get("introduced_or_changed", []):
        if path in changed_set:
            out.append({"path": path, "symbol": symbol})
    return out


def _filter_targets(eligible: list[dict]) -> tuple[list, list]:
    """Split eligible targets into retrievable vs deterministically
    filtered (with recorded reasons)."""
    targets, filtered = [], []
    for t in eligible:
        if _is_filtered_unit(t["path"], t["symbol"]):
            filtered.append(f"{t['path']}::{t['symbol']}")
        else:
            targets.append(t)
    return targets, filtered


def _order_pairs(pairs: list[dict]) -> list[dict]:
    """Deterministic credibility order: pairs whose counterpart was
    retrieved through more channels first (stronger deterministic
    signal), then by stable (path, symbol) keys. No filename bias:
    alphabetical order is only the final tiebreak."""
    def _key(p: dict) -> tuple:
        t, c = p.get("target") or {}, p.get("counterpart") or {}
        return (-len(p.get("channels") or []),
                str(t.get("path") or ""), str(t.get("symbol") or ""),
                str(c.get("path") or ""), str(c.get("symbol") or ""))
    return sorted(pairs, key=_key)


def _targets_with_inference(rows: list[dict]) -> int:
    """Targets where at least one pair reached Terra inference."""
    seen = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("assess_ran"):
            t = r.get("target") or {}
            seen.add(f"{t.get('path')}::{t.get('symbol')}")
    return len(seen)


def _continuation_path(state_dir: Path, repo: str, pr: int,
                       base_rev: str, head_rev: str) -> Path:
    slug = str(repo).replace("/", "__")
    return (Path(state_dir) / "semdup" / "continued" / slug / str(pr) /
            f"{base_rev}__{head_rev}.json")


def _load_continuation(state_dir: Path, repo: str, pr: int,
                       base_rev: str, head_rev: str) -> dict:
    """Prior verdicts for this exact delta ({} when absent/stale)."""
    try:
        raw = json.loads(_continuation_path(
            state_dir, repo, pr, base_rev, head_rev).read_text(
            encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no prior record: fresh run
        return {}
    if not isinstance(raw, dict) or raw.get("version") != STAGE_VERSION:
        return {}
    pairs = raw.get("pairs")
    return pairs if isinstance(pairs, dict) else {}


def _save_continuation(state_dir: Path, repo: str, pr: int,
                       base_rev: str, head_rev: str, pairs: dict) -> None:
    try:
        path = _continuation_path(state_dir, repo, pr, base_rev, head_rev)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"version": STAGE_VERSION, "repo": repo, "pr": pr,
             "base_rev": base_rev, "head_rev": head_rev, "pairs": pairs},
            indent=1, default=str))
    except Exception:  # noqa: BLE001 - best effort; run dir persists anyway
        pass


def _merge_continuation(state_dir: Path, repo: str, pr: int,
                         base_rev: str, head_rev: str,
                         rows: list[dict]) -> None:
    """Merge judged rows into the delta's continuation record."""
    try:
        known = _load_continuation(state_dir, repo, pr, base_rev, head_rev)
        for row in rows:
            if not isinstance(row, dict):
                continue
            t, c = row.get("target") or {}, row.get("counterpart") or {}
            if not t.get("path") or not c.get("path"):
                continue
            known[_pair_key(t, c)] = row
        if known:
            _save_continuation(state_dir, repo, pr, base_rev, head_rev,
                               known)
    except Exception:  # noqa: BLE001 - never break judging
        pass


def _partition_new(pairs: list[dict], known: dict) -> tuple[list, list]:
    """Split pairs into (to-judge, reused-verdict-rows).

    Rows that were recorded as model verdicts but carry a spending-guard
    refusal in their detail were never judged: they are re-judged, never
    reused. (Older records predate refusal classification.)
    """
    new_pairs, reused = [], []
    for p in pairs:
        key = _pair_key(p.get("target") or {}, p.get("counterpart") or {})
        row = known.get(key)
        if (isinstance(row, dict)
                and not _mislabeled_refusal(row)):
            row = dict(row)
            row["reused"] = True
            reused.append(row)
        else:
            new_pairs.append(p)
    return new_pairs, reused


def _mislabeled_refusal(row: dict) -> bool:
    """A stored model-verdict label that actually hides a guard refusal."""
    from codatiel_i2.semdup_dev import editorial_shadow as _ES
    if row.get("reason") not in ("terra_no_lead", "editor_unpublishable"):
        return False
    detail = str(row.get("detail") or "")
    return _ES._is_budget_error(detail)


def _persist(run_dir: Path, outcome: dict) -> None:
    try:
        (run_dir / "semdup.json").write_text(
            json.dumps(outcome, indent=1, default=str))
    except Exception:  # noqa: BLE001 - best effort; summary returned anyway
        pass
