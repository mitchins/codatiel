"""Codatiel Iteration 2 CLI: scan / index / tail entry points."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from codatiel_i2.core import coverage as coverage_mod
from codatiel_i2.core import diff as diff_mod
from codatiel_i2.core import files as files_mod
from codatiel_i2.core import runs as runs_mod
from codatiel_i2.core import workspace as workspace_mod
from codatiel_i2.detectors.noop import NoOpDetector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codatiel-i2", description="Codatiel Iteration 2 scanner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="Deterministic inventory/preflight: workspace, diff, classification. No models, no findings.")
    scan.add_argument("--repo", default="")
    scan.add_argument("--pr", default="")
    scan.add_argument("--repo-root", default="")
    scan.add_argument("--base", default="")
    scan.add_argument("--head", default="")
    scan.add_argument("--runs-root", default="runs")
    scan.add_argument("--work-root", default=".codatiel-i2-work")
    scan.add_argument("--detectors", default="none", help="comma list; 'none' runs no-op pipeline")

    index = sub.add_parser("index", help="Build main-branch index (Phase B skeleton).")
    index.add_argument("--repo-root", required=True)
    index.add_argument("--repo-id", required=True)
    index.add_argument("--commit", required=True)
    index.add_argument("--out", required=True)

    tail = sub.add_parser("tail", help="Discover open PRs and scan changed ones (report-only).")
    tail.add_argument("--repo", required=True)
    tail.add_argument("--runs-root", default="runs")
    tail.add_argument("--state", default="tailer_state.json")
    tail.add_argument("--limit", type=int, default=20)

    review = sub.add_parser("review", help="Canonical agentic review: local pair or GitHub PR.")
    review.add_argument("--repo-root", default="", help="local git repo (pair mode)")
    review.add_argument("--base", default="")
    review.add_argument("--head", default="")
    review.add_argument("--repo", default="", help="owner/name (PR mode)")
    review.add_argument("--pr", default="", help="PR number (PR mode)")
    review.add_argument("--model", default="",
                        help="scripted:<scenario.json> for mock runs, or "
                             "luna|haiku|gemma (provider wiring, M4)")
    review.add_argument("--runs-root", default="runs")
    review.add_argument("--work-root", default=".codatiel-i2-work")
    review.add_argument("--max-turns", type=int, default=12)
    review.add_argument("--max-spend-usd", type=float, default=None,
                        help="per-review ceiling (default: profile or "
                             "CODATIEL_I2_SPEND_CEILING_USD or 5.0)")

    deliver = sub.add_parser("deliver-shadow", help="Deliver a run's shadow digest.")
    deliver.add_argument("--run-dir", required=True)
    deliver.add_argument("--to", required=True,
                         help="comma-separated recipient addresses")
    deliver.add_argument("--sender", default="",
                         help="envelope sender address (required; no default is assumed)")
    deliver.add_argument("--transport", default="smtp2go",
                         help="smtp2go (ops) or fixture (tests/gates)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "scan":
        payload = cmd_scan(args)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload.get("status") == "ready" else 1
    if args.cmd == "index":
        payload = cmd_index(args)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.cmd == "tail":
        payload = cmd_tail(args)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.cmd == "review":
        payload = cmd_review(args)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload.get("status") == "complete" else 1
    if args.cmd == "deliver-shadow":
        payload = cmd_deliver_shadow(args)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        receipt = payload.get("transport_receipt") or {}
        delivered = (bool(receipt.get("parsed"))
                     and not receipt.get("failed")
                     and int(receipt.get("succeeded") or 0) == len(payload.get("to") or []))
        return 0 if delivered else 1
    return 1


def cmd_scan(args) -> dict:
    started = time.monotonic()
    runs_root = Path(args.runs_root)
    work_root = Path(args.work_root)

    if args.repo_root and args.base and args.head:
        ws = workspace_mod.prepare_local_workspace(Path(args.repo_root), args.base, args.head, work_root)
        repo_label = str(Path(args.repo_root).resolve())
        diff_repo = Path(args.repo_root)
    elif args.repo and args.pr:
        ws = workspace_mod.prepare_github_workspace(args.repo, int(args.pr), work_root)
        repo_label = args.repo
        diff_repo = Path(ws.head_dir) if ws.status == "ready" else Path(".")
    else:
        return {"status": "failed", "error": "scan needs --repo-root/--base/--head or --repo/--pr"}

    if ws.status != "ready":
        return {"status": "failed", "error": ws.error, "workspace": ws.to_dict()}

    from codatiel_i2.common.git import GitError

    try:
        changes = diff_mod.changed_files(diff_repo, ws.base_sha, ws.head_sha)
    except GitError as exc:
        return {"status": "failed", "error": f"diff failed: {exc}", "workspace": ws.to_dict()}
    binary_paths = {row["path"] for row in changes if row.get("is_binary")}
    classification = files_mod.classify_changes(changes, binary_paths=binary_paths)
    if not classification.get("accounting_complete"):
        return {"status": "failed", "error": "file accounting incomplete", "workspace": ws.to_dict()}

    # Detector pipeline (Phase A: no-op unless explicitly enabled).
    detectors = [d.strip() for d in str(args.detectors).split(",") if d.strip()] if args.detectors else []
    candidates: dict[str, list] = {d: [] for d in runs_mod.CANDIDATE_FILES}
    evidence: list[dict] = []
    verifications: list[dict] = []
    findings: list[dict] = []
    if "noop" in detectors:
        noop = NoOpDetector()
        change_set = {"changes": changes, "classification": classification}
        app = noop.applicable(change_set)
        _ = app  # recorded in metrics
    coverage = coverage_mod.empty_coverage()

    from codatiel_i2 import __version__ as _pkg_version

    config = {"detectors": detectors, "models": "none", "index": "none",
              "codatiel_i2_version": _pkg_version}
    scan_key = runs_mod.scan_key_for(repo_label, ws.base_sha, ws.head_sha, config)
    tool_ledger: list[dict] = []
    model_ledger: list[dict] = []
    metrics = {
        "schema_version": "codatiel_i2.metrics.v1",
        "runtime_seconds": round(time.monotonic() - started, 3),
        "changed_files": len(changes),
        "classified_files": classification.get("classified", 0),
        "findings": len(findings),
        "main_model_tokens": 0,
        "breadth_model_tokens": 0,
        "tool_calls": len(tool_ledger),
        "model_calls": len(model_ledger),
        "detectors_requested": detectors,
    }
    run_dir = None
    run_id = ""
    last_collision: Exception | None = None
    for _ in range(3):
        run_id = runs_mod.new_run_id()
        candidate = runs_mod.new_run_dir(runs_root, scan_key, run_id)
        try:
            runs_mod.write_run(run_dir=candidate, scan_key=scan_key, run_id=run_id,
                               repo=repo_label, base_sha=ws.base_sha, head_sha=ws.head_sha,
                               config=config,
                               workspace=ws.to_dict(), changes=changes, classification=classification,
                               coverage=coverage, candidates=candidates, evidence=evidence,
                               verifications=verifications, findings=findings, metrics=metrics,
                               tool_calls=tool_ledger, model_calls=model_ledger)
            run_dir = candidate
            break
        except FileExistsError as exc:
            last_collision = exc
            continue
    if run_dir is None:
        return {"status": "failed", "error": f"run_id collision after retries: {last_collision}"}
    return {"status": "ready", "run_dir": str(run_dir), "repo": repo_label,
            "scan_key": scan_key, "run_id": run_id,
            "base_sha": ws.base_sha, "head_sha": ws.head_sha,
            "changed_files": len(changes), "findings": 0}


def cmd_index(args) -> dict:
    from codatiel_i2.index.build import build_index
    from codatiel_i2.index.store import open_index

    conn = open_index(Path(args.out))
    result = build_index(Path(args.repo_root), args.repo_id, args.commit, conn)
    conn.close()
    return {"status": "ok", **result, "out": args.out}


def cmd_tail(args) -> dict:
    from codatiel_i2.github import tailer as tailer_mod

    prs, error = tailer_mod.discover_prs(args.repo, args.limit)
    if prs is None:
        return {"status": "failed", "error": error}
    state = tailer_mod.load_state(Path(args.state))
    scanned: list[dict] = []
    skipped = 0
    for pr in prs:
        head_sha = str(pr.get("headRefOid") or "")
        should, reason = tailer_mod.note_seen(state, args.repo, int(pr.get("number")), head_sha)
        if not should:
            skipped += 1
            continue
        result = tailer_mod.scan_pr(args.repo, int(pr.get("number")), Path(args.runs_root))
        if tailer_mod.scan_succeeded(result):
            tailer_mod.note_success(state, args.repo, int(pr.get("number")), head_sha)
        scanned.append({"pr": pr.get("number"), "head_sha": head_sha, **result})
    Path(args.state).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "ok", "discovered": len(prs), "scanned": len(scanned), "skipped": skipped, "runs": scanned}


def cmd_review(args) -> dict:
    """Canonical agentic review: local revision pair or GitHub PR.

    scripted:<scenario.json> for mock runs; luna|haiku|gemma build real
    backends through providers (credentials required). Anything else fails
    loudly: no static fallback.
    """
    from codatiel_i2 import providers as providers_mod
    from codatiel_i2 import service as service_mod

    spec = args.model or ""
    rate: dict[str, Any] = {}
    rate_status = "unconfirmed"
    model_settings = None
    profile_ceiling: float | None = None
    if spec.startswith("scripted:"):
        try:
            model = service_mod.scripted_model_from_scenario(spec[len("scripted:"):])
        except Exception as exc:
            return {"status": "failed", "stage": "model",
                    "error": f"cannot load scenario: {type(exc).__name__}: {exc}"}
        model_name = f"scripted:{spec[len('scripted:'):]}"
    elif spec in providers_mod.BACKENDS:
        try:
            resolved = providers_mod.resolve_backend(spec)
            model, model_settings = providers_mod.build_model(spec)
        except Exception as exc:
            return {"status": "failed", "stage": "model",
                    "error": f"cannot build backend {spec!r}: "
                             f"{type(exc).__name__}: {exc}"}
        model_name = f"{spec}:{resolved['model']}"
        rate = resolved["rate_usd_per_mtok"]
        rate_status = resolved["rate_status"]
        profile_ceiling = resolved.get("max_usd_per_review")
    else:
        return {"status": "failed", "stage": "model",
                "error": f"unsupported model {spec!r}: use scripted:<scenario.json> "
                         "or luna|haiku|gemma"}
    if args.max_spend_usd is not None:
        ceiling = args.max_spend_usd
    elif profile_ceiling is not None:
        ceiling = profile_ceiling
    else:
        ceiling = None
    kwargs: dict[str, Any] = {"model": model, "model_name": model_name,
                              "runs_root": args.runs_root,
                              "work_root": args.work_root,
                              "max_turns": args.max_turns,
                              "model_settings": model_settings,
                              "rate": rate, "rate_status": rate_status,
                              "max_usd_per_review": ceiling}
    if args.repo_root and args.base and args.head:
        return service_mod.review_local(
            args.repo_root, args.base, args.head, **kwargs)
    if args.repo and args.pr:
        try:
            pr_number = int(args.pr)
        except ValueError:
            return {"status": "failed", "stage": "args",
                    "error": f"invalid --pr {args.pr!r}"}
        return service_mod.review_pr(
            args.repo, pr_number, **kwargs)
    return {"status": "failed", "stage": "args",
            "error": "review needs --repo-root+--base+--head or --repo+--pr"}


def cmd_deliver_shadow(args) -> dict:
    """Deliver a run's shadow digest via smtp2go or a fixture transport."""
    from codatiel_i2 import service as service_mod

    to = [a.strip() for a in (args.to or "").split(",") if a.strip()]
    if not to:
        return {"status": "failed", "stage": "delivery",
                "error": "no recipients: pass --to addr@example.com,..."}
    if not (args.sender or "").strip():
        return {"status": "failed", "stage": "delivery",
                "error": "no sender: pass --sender addr@example.com"}

    sender = args.sender.strip()
    transport = None
    if args.transport == "fixture":
        sent: list[dict] = []

        def transport(to_addrs: list[str], subject: str,
                      body: str) -> dict:
            sent.append({"to": to_addrs, "subject": subject,
                         "body": body})
            return {"accepted_by_transport": True, "parsed": True,
                    "fixture": True, "succeeded": len(to_addrs),
                    "failed": 0, "email_id": "fixture-no-send"}
    elif args.transport != "smtp2go":
        return {"status": "failed", "stage": "delivery",
                "error": f"unknown transport {args.transport!r}: "
                         "smtp2go|fixture"}
    try:
        return service_mod.deliver_shadow(
            args.run_dir, to=to, sender=sender, transport=transport)

    except Exception as exc:
        return {"status": "failed", "stage": "delivery",
                "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
