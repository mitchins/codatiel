"""The DEV semantic-duplication worker (report-only, DEV-only).

Driven by the same delivery/checkpoint mechanics as the event
pipeline (deliveries table for duplicate suppression, reviews table
with kind="semdup-dev" for last-processed-head checkpoints), but a
separate worker: it never runs the standard PR review, never delays
it (index builds are scheduled asynchronously; a missing or stale
index yields an explicit incomplete/deferred record), and never
comments on GitHub.

Per delta:

1. Deduplicate: seen delivery ID, or same delta fingerprint as the
   last processed head -> no inference.
2. Establish the examined range: last processed head when known,
   else the PR base. Rebases/force-pushes compare actual trees
   (`git diff last_head head`); if the changed set cannot be
   determined, defer explicitly rather than rescanning the PR.
3. Ensure the persistent base index (async: missing/stale -> schedule
   build, return incomplete), build the head overlay, materialize the
   merged head view.
4. Classify (pure `check.classify` on unit maps). Only added/changed
   units are investigation targets; moves, pre-existing and removed
   stay counts.
5. Resolve each target exactly; run the M11 staged investigation on the
   merged view with retrieval coverage stated honestly.
6. Emit findings-contract records from the challenged assessment only.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

from codatiel_i2.semdup_dev import SEMDUP_DEV_KIND
from codatiel_i2.semdup_dev import config as C
from codatiel_i2.semdup_dev import delta as D
from codatiel_i2.semdup_dev import findings as F
from codatiel_i2.semdup_dev import index_state as IX
from codatiel_i2.semdup_dev import merged_view as MV
from codatiel_i2.semdup_dev import resolve as R

FILE_CAP = 300  # mirrors the GitHub compare changed-file cap


def _git(repo_path: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_path), *args],
                          capture_output=True, timeout=timeout)


def last_processed(store, repo: str, pr: int) -> dict | None:
    """Latest semdup-dev checkpoint for (repo, pr), if any."""
    cands = [r for r in store.reviews_list()
             if r["repo"] == repo and r["pr"] == pr
             and r["kind"] == SEMDUP_DEV_KIND
             and r["status"] in ("complete", "partial", "grandfathered")]
    if not cands:
        return None
    cands.sort(key=lambda r: r["finished_ts"])
    return cands[-1]


def changed_files(repo_path: Path, from_rev: str, head_rev: str,
                  cap: int = FILE_CAP) -> dict:
    """Actual tree diff. Uncertain (missing objects, errors, over cap)
    -> explicit deferral, never a silent whole-PR rescan."""
    if from_rev == head_rev:
        return {"ok": True, "files": [], "empty": True}
    proc = _git(repo_path, "diff", "--name-only", "-z", from_rev, head_rev)
    if proc.returncode != 0:
        return {"ok": False, "reason": "changed set not determinable",
                "detail": (proc.stderr.decode("utf-8", "replace")[:200])}
    files = [f for f in proc.stdout.decode("utf-8", "replace").split("\0")
             if f]
    if len(files) > cap:
        return {"ok": False, "reason": "changed set exceeds file cap",
                "detail": f"{len(files)} files > cap {cap}"}
    return {"ok": True, "files": files, "empty": not files}


def run_dir_for(state_dir: Path, repo: str, pr: int,
                head_rev: str) -> Path:
    d = (state_dir / "semdup" / "runs" / IX.slug(repo) / str(pr)
         / head_rev[:12])
    d.mkdir(parents=True, exist_ok=True)
    return d


def process_delta(*, cfg: C.SemdupDevConfig, store,
                  repo_path: Path, repo: str, pr: int,
                  base_rev: str, head_rev: str, delivery_id: str,
                  action: str = "semdup",
                  grandfather: bool = False) -> dict:
    """Process one PR delta. Returns a JSON-serializable outcome."""
    t0 = time.monotonic()
    if repo not in cfg.repos:
        return {"status": "refused", "reason": "repo not in DEV scope"}
    seen = store.delivery_get(delivery_id)
    if seen is not None and seen.get("decision") not in ("in_progress",):
        store.delivery_mark_replay(delivery_id, seen)
        return {"status": "skipped", "reason": "duplicate delivery",
                "delivery_id": delivery_id,
                "original_decision": seen.get("decision")}
    store.delivery_record(delivery_id=delivery_id, event_type="semdup-dev",
                          action=action, repo=repo, pr=pr, head_sha=head_rev,
                          decision="in_progress", reason="semdup worker claimed",
                          cost_usd=None, detail={})
    outcome = _process(cfg, store, repo_path, repo, pr, base_rev, head_rev,
                       grandfather=grandfather)
    final = outcome.get("status", "unknown")
    terminal = {"complete", "partial", "deferred", "skipped",
                "grandfathered", "indexing", "refused"}
    store.delivery_update_decision(
        delivery_id, final if final in terminal else "deferred",
        outcome.get("reason", ""), None)
    outcome["wall_s"] = round(time.monotonic() - t0, 1)
    return outcome


def _process(cfg, store, repo_path, repo, pr, base_rev, head_rev,
             grandfather: bool) -> dict:
    prev = last_processed(store, repo, pr)
    from_head = prev["head_sha"] if prev else base_rev
    if grandfather and prev is None:
        run_dir = run_dir_for(cfg.state_dir, repo, pr, head_rev)
        store.review_record(repo=repo, pr=pr, kind=SEMDUP_DEV_KIND,
                            base_sha=base_rev, head_sha=head_rev,
                            diff_sha="", files=0,
                            pipeline="semdup-dev-01", status="grandfathered",
                            terra_findings=0, luna_added=0, cost_usd=None,
                            cost_unknown=False, run_dir=str(run_dir))
        return {"status": "grandfathered",
                "reason": "PR predates DEV enable; baseline recorded, "
                          "only future deltas examined"}
    ch = changed_files(repo_path, from_head, head_rev)
    if not ch["ok"]:
        return _defer(store, cfg, repo, pr, base_rev, head_rev, from_head,
                      ch["reason"], ch.get("detail", ""))
    if ch["empty"]:
        return {"status": "skipped",
                "reason": "no changes since last processed head"}
    # Persistent base index (async contract).
    base_st = IX.base_index_status(cfg.state_dir, repo, base_rev)
    if not base_st["present"]:
        _schedule_base_build(cfg, repo_path, repo, base_rev)
        return _defer(store, cfg, repo, pr, base_rev, head_rev, from_head,
                      "base index unavailable",
                      f"{base_st.get('reason')}; build scheduled, "
                      f"delta deferred (never a clean negative)")
    base_db = IX.base_paths(cfg.state_dir, repo, base_rev)["db"]
    # Head overlay over the PINNED base (staleness checked, never assumed).
    ov_stat = IX.overlay_status(cfg.state_dir, repo, pr, head_rev, base_db)
    if not ov_stat["present"]:
        built = IX.build_overlay(repo_path, repo, pr, base_rev, head_rev,
                                 cfg.state_dir, base_db,
                                 allow_embeddings=cfg.allow_embeddings)
        if not built["ok"]:
            return _defer(store, cfg, repo, pr, base_rev, head_rev,
                          from_head, "overlay unavailable", built["reason"])
        ov_stat = IX.overlay_status(cfg.state_dir, repo, pr, head_rev,
                                    base_db)
        if not ov_stat["present"]:
            return _defer(store, cfg, repo, pr, base_rev, head_rev,
                          from_head, "overlay unavailable",
                          ov_stat.get("reason", ""))
    overlay_dir = Path(ov_stat["overlay_dir"])
    run_dir = run_dir_for(cfg.state_dir, repo, pr, head_rev)
    merged = MV.build_merged_view(base_db, overlay_dir,
                                  run_dir / "head_view.sqlite")
    if not merged["ok"]:
        return _defer(store, cfg, repo, pr, base_rev, head_rev, from_head,
                      "head view unavailable", merged["reason"])
    classification = D.classify_delta(base_db, Path(merged["db"]))
    units = classification["units"]
    fp = D.delta_fingerprint(base_rev, head_rev, from_head,
                             classification["touched"])
    if prev and prev.get("diff_sha") == fp and prev.get("head_sha") == head_rev:
        return {"status": "skipped",
                "reason": "delta fingerprint already processed"}
    targets = _select_targets(classification, cfg.max_targets_per_delta)
    records = [_target_record(cfg, repo_path, repo, pr, base_rev, head_rev,
                              from_head, fp, merged, target, classification)
               for target in targets["selected"]]
    for extra in targets["deferred"]:
        records.append(F.deferred_record(
            repo=repo, pr=pr, base_rev=base_rev, head_rev=head_rev,
            from_head=from_head, delta_fingerprint=fp,
            reason="target budget exhausted", detail=extra,
            classification="introduced-or-worsened"))
    summary = F.summarize_records(records)
    status = "complete"
    if any(r["status"] == "deferred" for r in records):
        status = "partial" if summary["actionable"] or any(
            r["status"] == "finding" for r in records) else "deferred"
    run_id = f"semdup-dev-{head_rev[:12]}-{fp}"
    (run_dir / "run.json").write_text(json.dumps(
        {"run_id": run_id, "detector": "semdup-dev-01", "repo": repo,
         "pr": pr, "base_sha": base_rev, "head_sha": head_rev,
         "from_head": from_head, "delta_fingerprint": fp,
         "config": {"model": cfg.investigator_model,
                    "expected_model": cfg.investigator_expected},
         "status": status}, indent=1))
    (run_dir / "shadow_result.json").write_text(json.dumps(
        {"status": status,
         "counts": {"records": len(records), **summary["by_status"]},
         "advisory": [
             {"detector": r.get("detector"),
              "changed_anchor": {"path": (r.get("target") or {}).get("path"),
                                 "line_start": (r.get("target") or {}).get("start_line")},
              "finding_id": f"{r.get('target', {}).get('path', '?')}::"
                            f"{r.get('target', {}).get('symbol', '?')}",
              "substantive_assessment": r.get("recommended_action")}
             for r in records if r.get("status") == "finding"]},
        indent=1, default=str))
    (run_dir / "findings.json").write_text(
        json.dumps({"records": records, "summary": summary,
                    "classification": units,
                    "counts": merged["counts"],
                    "retrieval_coverage": merged["retrieval_coverage"]},
                   indent=1, default=str))
    store.review_record(repo=repo, pr=pr, kind=SEMDUP_DEV_KIND,
                        base_sha=base_rev, head_sha=head_rev, diff_sha=fp,
                        files=len(ch["files"]), pipeline="semdup-dev-01",
                        status=status, terra_findings=0, luna_added=0,
                        cost_usd=0.0, cost_unknown=False,
                        run_dir=str(run_dir))
    return {"status": status, "run_dir": str(run_dir),
            "delta_fingerprint": fp,
            "retrieval_coverage": merged["retrieval_coverage"],
            "summary": summary,
            "records": records}


def _defer(store, cfg, repo, pr, base_rev, head_rev, from_head, reason,
           detail) -> dict:
    fp = D.delta_fingerprint(base_rev, head_rev, from_head, [])
    rec = F.deferred_record(repo=repo, pr=pr, base_rev=base_rev,
                            head_rev=head_rev, from_head=from_head,
                            delta_fingerprint=fp, reason=reason,
                            detail=detail)
    run_dir = run_dir_for(cfg.state_dir, repo, pr, head_rev)
    (run_dir / "findings.json").write_text(
        json.dumps({"records": [rec]}, indent=1, default=str))
    store.review_record(repo=repo, pr=pr, kind=SEMDUP_DEV_KIND,
                        base_sha=base_rev, head_sha=head_rev, diff_sha=fp,
                        files=0, pipeline="semdup-dev-01", status="deferred",
                        terra_findings=0, luna_added=0, cost_usd=None,
                        cost_unknown=False, run_dir=str(run_dir))
    return {"status": "deferred", "reason": reason, "detail": detail,
            "run_dir": str(run_dir)}


def _select_targets(classification: dict, budget: int) -> dict:
    cands = classification["introduced_or_changed"]
    selected = [{"path": p, "symbol": s} for (p, s) in cands[:budget]]
    deferred = [f"{p}::{s}" for (p, s) in cands[budget:]]
    return {"selected": selected, "deferred": deferred,
            "moved_skipped": len(classification["units"].get("moved", []))}


def _target_record(cfg, repo_path, repo, pr, base_rev, head_rev, from_head,
                   fp, merged, target, classification) -> dict:
    from_cod = "codatiel_i2.semdup_vendored.semdup"
    import importlib
    ST = importlib.import_module(f"{from_cod}.staged")
    conn = sqlite3.connect(str(merged["db"]))
    try:
        resolved = R.resolve_target(conn, target["path"], target["symbol"])
    finally:
        conn.close()
    provenance = _provenance(cfg, merged)
    if not resolved.get("ok"):
        return F.deferred_record(
            repo=repo, pr=pr, base_rev=base_rev, head_rev=head_rev,
            from_head=from_head, delta_fingerprint=fp,
            reason="target unresolvable", detail=resolved.get("detail", ""),
            classification=_kind_of(classification, target))
    head_tree = _materialize_head(repo_path, head_rev)
    if head_tree is None:
        return F.deferred_record(
            repo=repo, pr=pr, base_rev=base_rev, head_rev=head_rev,
            from_head=from_head, delta_fingerprint=fp,
            reason="head tree unavailable",
            classification=_kind_of(classification, target))
    try:
        rep = ST.investigate(
            Path(head_tree), Path(merged["db"]),
            {"file": resolved["path"], "symbol": resolved["symbol"]},
            (cfg.investigator_url, cfg.investigator_model),
            (cfg.challenger_url, cfg.challenger_model)
            if cfg.challenger_url else None,
            emb=merged["emb"],
            investigator_expected=cfg.investigator_expected,
            challenger_expected=cfg.challenger_expected)
    except Exception as exc:  # noqa: BLE001 - engine failure is deferral
        return F.deferred_record(
            repo=repo, pr=pr, base_rev=base_rev, head_rev=head_rev,
            from_head=from_head, delta_fingerprint=fp,
            reason="investigation failed",
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            classification=_kind_of(classification, target))
    finally:
        import shutil as _shutil
        _shutil.rmtree(str(Path(head_tree).parent), ignore_errors=True)
    served = _verify_model_identity(rep, cfg)
    provenance = dict(provenance)
    provenance["served_model_check"] = served
    return F.build_finding(
        repo=repo, pr=pr, base_rev=base_rev, head_rev=head_rev,
        from_head=from_head, delta_fingerprint=fp,
        target=resolved, report=rep, provenance=provenance,
        classification=_kind_of(classification, target),
        retrieval_coverage=merged["retrieval_coverage"])


def _kind_of(classification: dict, target: dict) -> str:
    key = (target["path"], target["symbol"])
    units = classification["units"]
    if key in units.get("added", []):
        return "introduced"
    if key in units.get("changed", []):
        return "worsened"
    return "unknown"


def _provenance(cfg: C.SemdupDevConfig, merged: dict) -> dict:
    return {
        "detector": "semdup-dev-01",
        "engine_sha": "4ec875aff60c5daa2e20a2491a237ce2465ee7d6",
        "investigator": {"url": cfg.investigator_url,
                         "requested_model": cfg.investigator_model,
                         "expected_model": cfg.investigator_expected},
        "challenger": {"url": cfg.challenger_url,
                       "requested_model": cfg.challenger_model,
                       "expected_model": cfg.challenger_expected},
        "retrieval_coverage": merged["retrieval_coverage"],
        "emb_model": (merged["emb"] or {}).get("model"),
        "merged_counts": merged["counts"],
    }


def _verify_model_identity(rep: dict, cfg: C.SemdupDevConfig) -> dict:
    """The transport verifies served==expected per call (fail-closed);
    a mismatch raises inside investigate and the delta defers. Reaching
    here means every model call passed verification."""
    return {"verified": True,
            "expected": cfg.investigator_expected,
            "note": "per-call served-id verification in inference.chat; "
                    "any mismatch aborts the investigation"}


def _materialize_head(repo_path: Path, head_rev: str) -> str | None:
    import tempfile
    tmp = tempfile.mkdtemp(prefix="semdup-headtree-")
    try:
        IX.materialize(head_rev, repo_path, Path(tmp) / "tree")
        return str(Path(tmp) / "tree")
    except Exception:  # noqa: BLE001
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)
        return None


def _schedule_base_build(cfg: C.SemdupDevConfig, repo_path: Path, repo: str,
                         rev: str) -> None:
    """Schedule the base index build asynchronously (never blocks)."""
    import os as _os
    log = (cfg.state_dir / "semdup" / "indexes" / IX.slug(repo)
           / f"{rev[:16]}.build.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        subprocess.Popen(
            ["python3", "-m", "codatiel_i2.semdup_dev.build_base",
             str(repo_path), repo, rev, str(cfg.state_dir),
             "1" if cfg.allow_embeddings else "0"],
            stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            cwd="/data/coding/codatiel-i2/repo",
            env={**_os.environ})
