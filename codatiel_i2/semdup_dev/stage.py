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

STAGE_VERSION = "semdup-stage-0.3"
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
              edit_fn: Callable | None = None,
              is_stale: Callable[[], bool] | None = None) -> dict:
    """Run the integrated duplication stage for one pinned base..head.

    `max_pairs` None (normal operation) examines every retrieved pair
    until the whole-PR spending guard binds; a positive int is an
    explicit controlled-experiment limit only. Writes `semdup.json`
    into run_dir. Never raises.

    `is_stale` (optional) is a callable returning True when the
    review's target head is no longer current. When stale, further
    paid calls are not issued (COST-01B stale-head optimization).
    """
    t0 = time.monotonic()
    run_dir = Path(run_dir)
    state_dir = Path(state_dir)
    outcome: dict[str, Any] = {
        "version": STAGE_VERSION, "status": "unavailable",
        "repo": repo, "pr": pr, "base_rev": base_rev,
        "head_rev": head_rev, "base_sha": base_rev, "head_sha": head_rev,
        "published": [], "suppressed": [],
        "skipped": [], "targets": [], "coverage": "unknown",
        "spend_usd": 0.0, "wall_s": 0.0, "reason": "",
        "pairs_total": 0, "pairs_reused": 0, "ceiling_usd": ceiling_usd,
        "preflight_usd": None,
    }
    bound_mismatch = _check_run_binding(run_dir, repo, pr, base_rev,
                                        head_rev)
    if bound_mismatch is not None:
        outcome["status"] = "unavailable"
        outcome["reason"] = bound_mismatch
        # The refusal must not poison later delivery: strip the
        # caller-supplied identity fields so a mismatched stamp can
        # never contradict the run's binding at render/delivery time.
        # The primary review's own artifacts still deliver normally.
        for key in ("repo", "pr", "base_rev", "head_rev",
                    "base_sha", "head_sha"):
            outcome[key] = None
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome
    try:
        return _run(repo_path=Path(repo_path), repo=repo, pr=pr,
                     base_rev=base_rev, head_rev=head_rev, run_dir=run_dir,
                     state_dir=state_dir,
                     max_pairs_per_target=max_pairs_per_target,
                     max_pairs=max_pairs, ceiling_usd=ceiling_usd,
                     allow_embeddings=allow_embeddings,
                     assess_fn=assess_fn, edit_fn=edit_fn,
                     outcome=outcome, t0=t0, is_stale=is_stale)
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        outcome["status"] = "unavailable"
        outcome["reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        _persist(run_dir, outcome)
        return outcome


def _run(*, repo_path, repo, pr, base_rev, head_rev, run_dir,
         state_dir, max_pairs_per_target, max_pairs,
         ceiling_usd, allow_embeddings, assess_fn, edit_fn,
         outcome, t0, is_stale=None) -> dict:
    # COST-01B: stale-head check before any duplication work
    if is_stale is not None:
        try:
            if is_stale():
                outcome["status"] = "stale_skipped"
                outcome["reason"] = "target head no longer current before duplication; no inference issued"
                outcome["coverage"] = "partial"
                outcome["wall_s"] = round(time.monotonic() - t0, 1)
                outcome["stale_skipped"] = True
                _persist(run_dir, outcome)
                return outcome
        except Exception:
            pass
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
                tgt = dict(target)
                if p.get("target_start_line") is not None:
                    tgt["start_line"] = p["target_start_line"]
                cpt = {"path": p.get("path"), "symbol": p.get("symbol")}
                if p.get("start_line") is not None:
                    cpt["start_line"] = p["start_line"]
                pairs.append({"target": tgt,
                              "counterpart": cpt,
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
        new_pairs, continuation_reused = _partition_new(pairs, known)
        # COST-01B: cross-revision content-addressed reuse (per-PR cache).
        # Continuation covers same base/head reruns; the cache covers
        # genuinely unchanged evidence across different revisions.
        cache_hits: list[dict] = []
        try:
            from codatiel_i2.semdup_dev import cache as CACHE
            from codatiel_i2.semdup_dev import oai_dupe as OD_mod
            from codatiel_i2.semdup_dev import editor as ED_mod
            import codatiel_i2.providers as P_mod
            # Resolve model identities without requiring credentials for the
            # key shape; missing backends still produce a stable key via
            # empty model strings (cache will then miss on model change).
            try:
                terra_resolve = P_mod.resolve_backend("terra")
                luna_resolve = P_mod.resolve_backend("luna")
                terra_model = str(terra_resolve.get("model") or "")
                luna_model = str(luna_resolve.get("model") or "")
            except Exception:
                terra_model = ""
                luna_model = ""
            try:
                terra_id = OD_mod.TERRA_INSTRUCTIONS_ID
            except Exception:
                terra_id = ""
            try:
                luna_id = ED_mod.editor_instructions_id()
            except Exception:
                luna_id = ""
            cache_dict = CACHE.load_cache(state_dir, repo, pr)
            still_new: list[dict] = []
            for p in new_pairs:
                ident = CACHE.build_identity(
                    merged_db=Path(merged["db"]),
                    target=p.get("target") or {},
                    counterpart=p.get("counterpart") or {},
                    classification=str(p.get("classification") or ""),
                    terra_model=terra_model,
                    luna_model=luna_model,
                    terra_instructions_id=terra_id,
                    luna_instructions_id=luna_id,
                    stage_version=STAGE_VERSION,
                )
                cached = CACHE.lookup(state_dir, repo, pr, ident, cache=cache_dict)
                if cached is not None:
                    # SCAFFOLD-01: never reuse a cached scaffold skip
                    # from a different rule version (or an unversioned
                    # legacy row). Re-judge instead; the current rule
                    # decides afresh.
                    try:
                        from codatiel_i2.semdup_dev import editorial_shadow as _ES2
                        usable = _ES2.scaffold_cache_usable(cached)
                    except Exception:
                        # Fail closed: without the rule module loaded we
                        # cannot validate a cached scaffold verdict, so
                        # re-judge instead of trusting it.
                        usable = False
                    if not usable:
                        still_new.append(p)
                        continue
                    rebound = CACHE.rebind_row(cached, Path(merged["db"]), repo, pr, head_rev)
                    if rebound is not None:
                        # Validate suggestion still applicable at current head
                        # CODE-01B: only reuse verdicts that did not depend on
                        # external tool reads (conservative) and were successful
                        if rebound.get("terra_tool_calls") or rebound.get("luna_tool_calls"):
                            # Depends on files outside the pair; decline reuse
                            still_new.append(p)
                            continue
                        cache_hits.append(rebound)
                        continue
                still_new.append(p)
            new_pairs = still_new
        except Exception:
            # Cache is best-effort; never break judging.
            pass
        # SIGNAL-01 B1: stamp the actually-examined list in
        # credibility order. The rank hashed into each id equals the
        # emitted rank. Ids are correlation labels only — never
        # continuation/cache key inputs.
        try:
            from codatiel_i2.semdup_dev import pairing as _PA
            _PA.stamp_pairs(new_pairs, repo=repo, pr=pr, base_rev=base_rev,
                            head_rev=head_rev)
        except Exception:
            pass
        # Keep continuation and cache hits separate for publication policy
        reused_rows = list(continuation_reused)
        outcome["pairs_reused"] = len(continuation_reused) + len(cache_hits)
        outcome["pairs_cache_hits"] = len(cache_hits)
        outcome["continuation_reused"] = len(continuation_reused)
        outcome["pairs_total"] = len(pairs)
        # Incremental persist: every CONTINUATION_EVERY judged pairs
        # merge into the record, so even a killed run keeps completed
        # verdicts for the authorized rerun.
        cont_buf: list[dict] = []

        def _on_pair(row: dict) -> None:
            # Completion-state gate: every failure (budget, provider,
            # evidence, stale) retries and is never cached. Completed
            # decisions retain reuse (same-revision continuation always;
            # cross-revision only when tool-independent).
            if not isinstance(row, dict):
                return
            try:
                from codatiel_i2.semdup_dev import cache as CACHE_CHK
                if CACHE_CHK.is_budget_stop_row(row) or CACHE_CHK.is_provider_failure_row(row):
                    return
                if not CACHE_CHK.is_completed_row(row):
                    return
            except Exception:
                if row.get("reason") == "budget_stop":
                    return
            # Preserve tool-dependent verdicts in exact-revision continuation
            # (same base/head retry must remain $0); decline only cross-revision reuse
            cont_buf.append(row)
            if len(cont_buf) >= CONTINUATION_EVERY:
                _merge_continuation(state_dir, repo, pr, base_rev,
                                    head_rev, cont_buf)
                cont_buf.clear()
            if row.get("terra_tool_calls") or row.get("luna_tool_calls"):
                return
            # SCAFFOLD-01 rows never enter the cross-revision cache:
            # the deterministic rule is expected to evolve, and a
            # cached skip must not outlive its rule version. Same-head
            # retries still reuse them via continuation (version-gated
            # in _partition_new).
            try:
                from codatiel_i2.semdup_dev import editorial_shadow as _ES2
            except Exception:
                _ES2 = None
            if _ES2 is None or not _ES2.scaffold_cacheable(row):
                # Fail closed (including on unreadable import): scaffold
                # rows never enter the cross-revision cache.
                return
            # COST-01B: persist to cross-revision cache as well.
            try:
                from codatiel_i2.semdup_dev import cache as CACHE2
                from codatiel_i2.semdup_dev import oai_dupe as OD2
                from codatiel_i2.semdup_dev import editor as ED2
                import codatiel_i2.providers as P2
                try:
                    tr = P2.resolve_backend("terra")
                    lr = P2.resolve_backend("luna")
                    tm = str(tr.get("model") or "")
                    lm = str(lr.get("model") or "")
                except Exception:
                    tm = ""
                    lm = ""
                try:
                    tid = OD2.TERRA_INSTRUCTIONS_ID
                except Exception:
                    tid = ""
                try:
                    lid = ED2.editor_instructions_id()
                except Exception:
                    lid = ""
                ident2 = CACHE2.build_identity(
                    merged_db=Path(merged["db"]),
                    target=row.get("target") or {},
                    counterpart=row.get("counterpart") or {},
                    classification=str(row.get("classification") or ""),
                    terra_model=tm,
                    luna_model=lm,
                    terra_instructions_id=tid,
                    luna_instructions_id=lid,
                    stage_version=STAGE_VERSION,
                )
                CACHE2.store(state_dir, repo, pr, ident2, row)
            except Exception:
                pass

        # MIRROR-01 seeds: same-revision published verdicts from the
        # continuation record only. Cross-revision cache hits are
        # deliberately NOT seeds: a cached twin may already be
        # delivered, and delivery dedup is direction-sensitive, so
        # preempting a fresh reverse pair with a delivered twin would
        # suppress the only deliverable direction. Reused cache hits
        # still collapse against fresh publications in the post-hoc
        # _dedup_mirrors below, preserving the old delivery behavior.
        # Rejections never seed a skip.
        known_published = mirror_seed_keys(continuation_reused)
        judged = ES.review_candidate_pairs(
            new_pairs, repo=repo, repo_path=repo_path,
            merged_db=Path(merged["db"]), base_rev=base_rev,
            head_rev=head_rev, head_tree=head_tree, base_tree=base_tree,
            run_dir=run_dir, ceiling_usd=ceiling_usd,
            assess_fn=assess_fn, edit_fn=edit_fn,
            on_pair=_on_pair, is_stale=is_stale,
            known_published_keys=known_published, pr=pr)
        if cont_buf:
            _merge_continuation(state_dir, repo, pr, base_rev, head_rev,
                                cont_buf)
    finally:
        import shutil as _shutil
        for tree in (head_tree, base_tree):
            if tree is not None:
                _shutil.rmtree(str(Path(tree).parent), ignore_errors=True)
    outcome["models"] = judged.get("models", {})
    # Per-model spend from the priced ledger results (persisted with the
    # stage outcome in semdup.json). None = unknown, never $0 confirmed.
    outcome["spend_usd"] = judged.get("spend_usd")
    outcome["spend_unknown"] = judged.get("spend_unknown", False)
    outcome["spend_terra_usd"] = judged.get("spend_terra_usd")
    outcome["spend_luna_usd"] = judged.get("spend_luna_usd")
    outcome["spend_terra_unknown"] = judged.get("spend_terra_unknown", False)
    outcome["spend_luna_unknown"] = judged.get("spend_luna_unknown", False)
    outcome["preflight_usd"] = judged.get("preflight_usd")
    # MIRROR-01 / SCAFFOLD-01 savings (estimates labeled _est).
    for prefix in ("mirror", "scaffold"):
        outcome[f"{prefix}_skipped"] = judged.get(f"{prefix}_skipped", 0)
        outcome[f"{prefix}_saved_usd_est"] = judged.get(
            f"{prefix}_saved_usd_est")
        outcome[f"{prefix}_saved_wall_s_est"] = judged.get(
            f"{prefix}_saved_wall_s_est")
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
    # judgments. No numerical cap: every Luna-approved pair publishes
    # for same-revision continuation; cross-revision cache hits are
    # kept as published here — delivery-layer (event_pipeline) will
    # suppress only when prior delivery is confirmed (COST-01B 1.1).
    new_rows = [r for r in judged.get("pairs", []) if isinstance(r, dict)]
    published = list(judged.get("published", []))
    suppressed = list(judged.get("suppressed", []))
    # SIGNAL-01 B1: reused rows are not examinations of this run —
    # their spend was attributed when originally judged. Drop stale
    # correlation labels (a cached id encodes the old head/rank and
    # must never resurface under a new revision).
    for r in continuation_reused:
        if not isinstance(r, dict):
            continue
        r = dict(r)
        r.pop("pair_id", None)
        r.pop("pair_rank", None)
        if r.get("path") == "published":
            published.append(ES._public_row(r))
        else:
            suppressed.append(ES._suppressed_row(r))
    for r in cache_hits:
        if not isinstance(r, dict):
            continue
        r = dict(r)
        r.pop("pair_id", None)
        r.pop("pair_rank", None)
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
        "pairs_mirror_skipped": int(outcome.get("mirror_skipped") or 0),
        "pairs_scaffold_skipped": int(
            outcome.get("scaffold_skipped") or 0),
        "pairs_scaffold_reused": sum(
            1 for r in list(continuation_reused)
            if isinstance(r, dict)
            and r.get("reason") == "scaffold_setup"),
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
    mirror_n = int(outcome.get("mirror_skipped") or 0)
    mirror_note = (f"; {mirror_n} mirror(s) skipped pre-examination"
                   if mirror_n else "")
    scaffold_n = int(outcome.get("scaffold_skipped") or 0)
    scaffold_note = (f"; {scaffold_n} scaffolding pair(s) skipped "
                     "pre-examination" if scaffold_n else "")
    reused_scaffold_n = sum(
        1 for r in list(continuation_reused)
        if isinstance(r, dict) and r.get("reason") == "scaffold_setup")
    if reused_scaffold_n:
        scaffold_note += (f" (+{reused_scaffold_n} reused prior "
                          "scaffolding verdicts)")
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
                             f"{skip_summary}{mirror_note}{scaffold_note}")
    else:
        outcome["status"] = "ok"
        outcome["coverage"] = ("partial"
                               if (hard_skips or guard_stops) else "full")
        outcome["reason"] = (f"examined {examined_targets} targets, "
                             f"{examined_pairs} pairs{reused_note}; "
                             f"{len(published)} approved"
                             f"{skip_summary}{guard_note}{mirror_note}{scaffold_note}")
    # Dedup against the ordinary review's advisory anchors.
    published, dups = _dedup(run_dir, published)
    suppressed.extend(dups)
    outcome["published"] = published
    outcome["suppressed"] = suppressed
    # SIGNAL-01 B4: budget-refusal states over fresh judgments (reused
    # rows predate the classification and report no status).
    outcome["budget_refusals"] = judged.get("budget_refusals")
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


def mirror_seed_keys(reused_rows) -> dict[str, str]:
    """Unordered keys of established published verdicts for MIRROR-01.

    Only rows with path == "published" seed a pre-examination skip;
    rejections, drops, budget stops and stale rows never do. Never
    raises: garbage in, empty set out.
    """
    known: dict[str, str] = {}
    try:
        items = list(reused_rows or [])
    except Exception:
        return known
    try:
        from codatiel_i2.semdup_dev import editorial_shadow as _ES
    except Exception:
        return known
    for r in items:
        try:
            if not isinstance(r, dict) or r.get("path") != "published":
                continue
            t = r.get("target") or {}
            c = r.get("counterpart") or {}
            if not t or not c:
                continue
            known[_ES.unordered_mirror_key(t, c)] = (
                f"{t.get('path')}::{t.get('symbol')} -> "
                f"{c.get('path')}::{c.get('symbol')}")
        except Exception:
            continue
    return known


def _dedup_mirrors(published: list) -> tuple[list, list]:
    """Collapse A->B / B->A double publications to the first row.

    Both directions describe the same shared code; emailing both is
    noise. Returns (kept, mirrors) where mirrors carry the kept
    direction for traceability. Key definition is shared with the
    pre-examination skip (MIRROR-01).
    """
    from codatiel_i2.semdup_dev import editorial_shadow as _ES
    seen: set[str] = set()
    kept, mirrors = [], []
    for row in published:
        t, c = row.get("target") or {}, row.get("counterpart") or {}
        key = _ES.unordered_mirror_key(t, c)
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
    """Identity key, line-qualified when lines are known.

    Same-name overloads across types share (path, symbol); without
    lines, unrelated overloads would share continuation verdicts.
    Pairs without lines (legacy records) never match line-qualified
    keys and are re-judged once.
    """
    def _loc(u: dict) -> str:
        sl = u.get("start_line")
        return (f"{u.get('path')}:{sl if sl is not None else '?'}::"
                f"{u.get('symbol')}")
    return f"{_loc(target)}>>{_loc(counterpart)}"


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
            # SIGNAL-01 B1: continuation stores verdicts, not run
            # correlation labels (rank/order may shift on re-drive).
            stored = dict(row)
            stored.pop("pair_id", None)
            stored.pop("pair_rank", None)
            known[_pair_key(t, c)] = stored
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
    Scaffold skips recorded under an older rule version are likewise
    re-judged: the deterministic rule may have changed.
    """
    try:
        from codatiel_i2.semdup_dev import editorial_shadow as _ES
        rule_version = str(_ES.SCAFFOLD_RULE)
    except Exception:
        rule_version = ""
    new_pairs, reused = [], []
    for p in pairs:
        key = _pair_key(p.get("target") or {}, p.get("counterpart") or {})
        row = known.get(key)
        if (isinstance(row, dict)
                and not _mislabeled_refusal(row)):
            if (row.get("reason") == "scaffold_setup"
                    and row.get("_scaffold_rule") != rule_version):
                new_pairs.append(p)
                continue
            row = dict(row)
            row["reused"] = True
            reused.append(row)
        else:
            new_pairs.append(p)
    return new_pairs, reused


def _mislabeled_refusal(row: dict) -> bool:
    """A stored verdict that is actually a retryable failure.

    Completion state first: any non-completed row (budget stop,
    provider failure, evidence failure, stale skip) is re-judged, never
    reused. Legacy rows without flags fall back to the budget-string
    check so old mislabeled refusals still retry.
    """
    try:
        from codatiel_i2.semdup_dev import cache as _CACHE
        if hasattr(_CACHE, "is_completed_row"):
            return not _CACHE.is_completed_row(row)
    except Exception:
        pass
    from codatiel_i2.semdup_dev import editorial_shadow as _ES
    if row.get("reason") not in ("terra_no_lead", "editor_unpublishable"):
        return False
    detail = str(row.get("detail") or "")
    return _ES._is_budget_error(detail)


def _check_run_binding(run_dir: Path, repo: str, pr: int,
                       base_rev: str, head_rev: str) -> str | None:
    """Refuse to stamp semdup rows onto a run bound to another review.

    The stage's (repo, PR, SHAs) arguments must equal the run dir's
    bound identity when one exists. Returns a reason string on
    mismatch, else None. Never raises (the stage degrades, it never
    breaks the standard review).
    """
    try:
        from codatiel_i2 import review_identity as _RI
        ident = _RI.load_identity(run_dir)
    except Exception:  # noqa: BLE001 - no binding readable, nothing to check
        return None
    if ident is None:
        if (run_dir / "identity.json").exists():
            return ("stage identity file present but unreadable; "
                    "refusing semdup stamp until repaired")
        return None
    try:
        want = {"repo_slug": repo, "pr_number": int(pr),
                "base_sha": base_rev, "head_sha": head_rev}
    except (TypeError, ValueError):
        return (f"stage identity invalid: repo={repo!r} pr={pr!r} "
                f"not bindable; refusing semdup stamp")
    for key, val in want.items():
        try:
            got = ident.get(key)
            if key == "pr_number" and got is not None:
                got = int(got)
        except (TypeError, ValueError):
            got = None
        if got != val:
            return (f"stage identity mismatch: run bound to "
                    f"{ident.get('repo_slug')}#{ident.get('pr_number')} "
                    f"{str(ident.get('head_sha') or '')[:12]} but stage "
                    f"called for {repo}#{pr}; refusing semdup stamp")
    return None


def _persist(run_dir: Path, outcome: dict) -> None:
    try:
        (run_dir / "semdup.json").write_text(
            json.dumps(outcome, indent=1, default=str))
    except Exception:  # noqa: BLE001 - best effort; summary returned anyway
        pass
