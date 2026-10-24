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

    watcher = sub.add_parser("watcher", help="v0.1.0 continuous shadow-review watcher.")
    watcher.add_argument("op", choices=["run-once", "daemon", "status",
                                        "backfill", "replay-delivery",
                                        "pause-repo", "resume-repo"],
                         help="run-once: single poll; daemon: loop until "
                              "SIGTERM; status: state summary; backfill: "
                              "run-once with debounce=0; replay-delivery: "
                              "resend one run's digest without new inference")
    watcher.add_argument("--repos", default="",
                         help="comma-separated owner/name allowlist "
                              "(default: CODATIEL_WATCH_REPOS)")
    watcher.add_argument("--to", default="",
                         help="comma-separated recipients "
                              "(default: CODATIEL_WATCH_TO)")
    watcher.add_argument("--sender", default="",
                         help="envelope sender (default: CODATIEL_WATCH_SENDER)")
    watcher.add_argument("--state", default="",
                         help="state file (default: CODATIEL_WATCH_STATE)")
    watcher.add_argument("--runs-root", default="",
                         help="default: CODATIEL_WATCH_RUNS")
    watcher.add_argument("--work-root", default="",
                         help="default: CODATIEL_WATCH_WORK")
    watcher.add_argument("--debounce", type=int, default=-1,
                         help="debounce seconds (-1: env/default 120)")
    watcher.add_argument("--interval", type=int, default=-1,
                         help="daemon poll seconds (-1: env/default 300)")
    watcher.add_argument("--spend-cap", type=float, default=-1.0,
                         help="total pilot cap USD (-1: env/default 5.0)")
    watcher.add_argument("--run-dir", default="",
                         help="replay-delivery target run dir")
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
    if args.cmd == "watcher":
        payload = cmd_watcher(args)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload.get("status") in ("ok", "delivered",
                                              "duplicate-suppressed",
                                              "paused", "resumed") else 1
    return 1


def watcher_config_from_env(args: Any) -> Any:
    import os
    from codatiel_i2 import watcher as watcher_mod

    def pick(flag: str, env: str, default: str = "") -> str:
        return (flag or os.environ.get(env, "") or default).strip()

    repos_raw = pick(args.repos, "CODATIEL_WATCH_REPOS")
    repos = ([r.strip() for r in repos_raw.split(",") if r.strip()]
             if repos_raw else None)
    exclude = [r.strip() for r in
               pick("", "CODATIEL_WATCH_EXCLUDE").split(",") if r.strip()]
    to = [a.strip() for a in
          pick(args.to, "CODATIEL_WATCH_TO").split(",") if a.strip()]
    sender = pick(args.sender, "CODATIEL_WATCH_SENDER")
    state = pick(args.state, "CODATIEL_WATCH_STATE", "watcher_state.json")
    runs_root = pick(args.runs_root, "CODATIEL_WATCH_RUNS", "runs")
    work_root = pick(args.work_root, "CODATIEL_WATCH_WORK",
                     ".codatiel-i2-work")
    debounce = int(args.debounce) if args.debounce is not None and args.debounce >= 0 \
        else int(os.environ.get("CODATIEL_WATCH_DEBOUNCE", "120"))
    interval = int(args.interval) if args.interval is not None and args.interval >= 0 \
        else int(os.environ.get("CODATIEL_WATCH_INTERVAL", "300"))
    cap = float(args.spend_cap) if args.spend_cap is not None and args.spend_cap >= 0 \
        else float(os.environ.get("CODATIEL_WATCH_SPEND_CAP", "5.0"))
    daily = float(os.environ.get("CODATIEL_WATCH_DAILY_CAP", "2.0"))
    seed = float(os.environ.get("CODATIEL_WATCH_SEED_SPENT", "0.389"))
    cfg = watcher_mod.WatcherConfig(
        repos=repos, exclude=exclude, poll_interval_s=interval,
        debounce_s=debounce, state_path=Path(state),
        runs_root=Path(runs_root), work_root=Path(work_root),
        recipients=to, sender=sender, spend_total_cap_usd=cap,
        spend_seed_usd=seed, spend_daily_cap_usd=daily)
    return cfg


def cmd_watcher(args: Any) -> dict:
    import datetime as _datetime
    import os
    from codatiel_i2 import providers as providers_mod
    from codatiel_i2 import service as service_mod
    from codatiel_i2 import watcher as watcher_mod

    op = args.op
    if op == "status":
        cfg = watcher_config_from_env(args)
        state = watcher_mod.load_state(cfg.state_path)
        try:
            auth_mode, _ = watcher_mod.resolve_github_auth()
        except RuntimeError as exc:
            auth_mode = f"missing: {exc}"
        return {"status": "ok", "config_version":
                watcher_mod.CONFIG_VERSION,
                "auth": auth_mode,
                "repos": cfg.repos, "exclude": cfg.exclude,
                "recipients": cfg.recipients,
                "spend_total_usd": watcher_mod.spend_total(state),
                "spend_cap_usd": cfg.spend_total_cap_usd,
                "prs_tracked": len(state.get("prs", {})),
                "paused": state.get("spend", {}).get("paused", False)}
    if op in ("pause-repo", "resume-repo"):
        cfg = watcher_config_from_env(args)
        if not args.repos:
            return {"status": "failed", "stage": "config",
                    "error": "pass --repos owner/name,..."}
        state = watcher_mod.load_state(cfg.state_path)
        for repo in [r.strip() for r in args.repos.split(",") if r.strip()]:
            entry = state.setdefault("repos", {}).setdefault(repo, {})
            entry["paused"] = (op == "pause-repo")
        watcher_mod.save_state(cfg.state_path, state)
        return {"status": "paused" if op == "pause-repo" else "resumed",
                "repos": args.repos}
    if op == "replay-delivery":
        return cmd_watcher_replay(args)
    cfg = watcher_config_from_env(args)
    try:
        auth_mode, app = watcher_mod.resolve_github_auth()
    except RuntimeError as exc:
        return {"status": "failed", "stage": "auth", "error": str(exc)}
    if auth_mode == "token" and not cfg.repos:
        return {"status": "failed", "stage": "config",
                "error": "token mode needs an explicit allowlist: pass "
                         "--repos or CODATIEL_WATCH_REPOS"}
    if not cfg.recipients or not cfg.sender:
        return {"status": "failed", "stage": "config",
                "error": "recipients and sender required (--to/--sender or env)"}
    if op == "backfill":
        cfg.debounce_s = 0
    backends: dict[str, Any] = {}
    for name in ("luna", "haiku"):
        try:
            resolved = providers_mod.resolve_backend(name)
            model, settings = providers_mod.build_model(name)
        except Exception as exc:
            return {"status": "failed", "stage": "model",
                    "error": f"cannot build backend {name!r}: "
                             f"{type(exc).__name__}: {exc}"}
        backends[name] = (resolved, model, settings)

    def review_with(name: str, repo: str, number: int) -> dict:
        from codatiel_i2 import watcher as watcher_mod
        resolved, model, settings = backends[name]
        state = watcher_mod.load_state(cfg.state_path)
        day = _datetime.date.today().isoformat()
        remaining = min(cfg.spend_total_cap_usd - watcher_mod.spend_total(state),
                        cfg.spend_daily_cap_usd - watcher_mod.spend_today(state, day))
        ceiling = min(float(resolved.get("max_usd_per_review") or 5.0),
                      max(remaining, 0.0))
        if name == "luna":
            from agents.model_settings import ModelSettings
            settings = ModelSettings(store=False, max_tokens=3000)
        try:
            return service_mod.review_pr(
                repo, number, model=model,
                model_name=f"{name}:{resolved['model']}",
                runs_root=cfg.runs_root / name / "A",
                work_root=cfg.work_root / name / "A",
                max_turns=12, model_settings=settings,
                rate=resolved["rate_usd_per_mtok"],
                rate_status=resolved["rate_status"],
                max_usd_per_review=ceiling,
                variant="baseline", playbook_name=None,
                playbook_text=None,
                inventory_strategy="file-list-0.1")
        except Exception as exc:
            return {"status": "failed", "stage": "agent",
                    "error": f"{type(exc).__name__}: {exc}"}

    def deliver(run_dir: Any, subject: str, body: str) -> dict:
        if run_dir is None:
            from codatiel_i2.delivery import smtp2go as smtp_mod
            return smtp_mod.send_email(cfg.recipients, cfg.sender,
                                       subject, body)
        return service_mod.deliver_shadow(
            run_dir, to=cfg.recipients, sender=cfg.sender,
            subject=subject, text_body=body)

    import signal as _signal
    stop = {"flag": False}

    def _handle(signum: int, frame: Any) -> None:
        stop["flag"] = True

    if op == "daemon":
        _signal.signal(_signal.SIGTERM, _handle)
        _signal.signal(_signal.SIGINT, _handle)
    while True:
        today = _datetime.date.today().isoformat()
        if auth_mode == "app":
            try:
                os.environ["GH_TOKEN"] = app.token()
            except Exception as exc:
                summary = {"status": "ok", "auth_error": str(exc),
                           "heartbeat": time.time()}
                state = watcher_mod.load_state(cfg.state_path)
                watcher_mod.save_state(cfg.state_path, state)
                if op != "daemon":
                    return summary
                time.sleep(min(cfg.poll_interval_s, 300))
                continue
        state = watcher_mod.load_state(cfg.state_path)
        if not state.get("spend", {}).get("seed_usd"):
            state["spend"]["seed_usd"] = cfg.spend_seed_usd
        summary = watcher_mod.poll_once(
            cfg, state, now=time.time(), day=today,
            review_luna=lambda repo, number: review_with("luna", repo, number),
            review_haiku=lambda repo, number: review_with("haiku", repo, number),
            deliver=deliver, app=app)
        summary["heartbeat"] = time.time()
        watcher_mod.save_state(cfg.state_path, state)
        Path(str(cfg.state_path) + ".heartbeat").write_text(
            json.dumps({"ts": summary["heartbeat"],
                        "spend_total_usd": watcher_mod.spend_total(state),
                        "summary": {k: v for k, v in summary.items()
                                    if k != "reviewed"}}),
            encoding="utf-8")
        if op != "daemon":
            summary["status"] = "ok"
            return summary
        if stop["flag"]:
            summary["status"] = "ok"
            summary["stopped"] = True
            return summary
        time.sleep(cfg.poll_interval_s)


def cmd_watcher_replay(args: Any) -> dict:
    """Resend one run's exact digest bytes without new inference.

    Same run_dir always rebuilds the same subject/body; if the stored
    delivery record already shows that body as delivered, the resend is
    suppressed as a duplicate. Prior attempts are preserved.
    """
    import os
    from codatiel_i2 import service as service_mod
    from codatiel_i2 import watcher as watcher_mod

    run_dir = Path(args.run_dir or "")
    if not run_dir.is_dir():
        return {"status": "failed", "stage": "delivery",
                "error": f"no such run dir {args.run_dir!r}"}
    to = [a.strip() for a in (args.to or os.environ.get(
        "CODATIEL_WATCH_TO", "") or "").split(",") if a.strip()]
    sender = (args.sender or os.environ.get("CODATIEL_WATCH_SENDER", "")
              ).strip()
    if not to or not sender:
        return {"status": "failed", "stage": "delivery",
                "error": "recipients and sender required"}
    subj, body = watcher_mod.build_review_email(run_dir)
    prior: dict = {}
    delivery_path = run_dir / "delivery.json"
    if delivery_path.exists():
        try:
            prior = json.loads(delivery_path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
    if (prior.get("body_sha256") ==
            __import__("hashlib").sha256(body.encode()).hexdigest()
            and watcher_mod.classify_receipt(
                prior.get("transport_receipt")) == "delivered"):
        return {"status": "duplicate-suppressed", "subject": subj,
                "body_sha256": prior["body_sha256"],
                "email_id": (prior.get("transport_receipt") or {}).get("email_id")}
    record = service_mod.deliver_shadow(run_dir, to=to, sender=sender,
                                        subject=subj, text_body=body)
    attempts = list(prior.get("attempts") or [])
    if prior.get("transport_receipt") is not None:
        attempts.append(prior["transport_receipt"])
    if attempts:
        record["attempts"] = attempts
        from codatiel_i2.common.jsonio import write_json
        write_json(run_dir / "delivery.json", record)
    return {"status": record.get("transport_receipt", {}).get("succeeded")
            and "delivered" or "failed",
            "subject": subj, "body_sha256": record.get("body_sha256"),
            "receipt": record.get("transport_receipt")}


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
