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
    review.add_argument("--expected-base", default="",
                        help="refuse PR review unless live base matches")
    review.add_argument("--expected-head", default="",
                        help="refuse PR review unless live head matches")

    deliver = sub.add_parser("deliver-shadow", help="Deliver a run's shadow digest.")
    deliver.add_argument("--run-dir", required=True)
    deliver.add_argument("--to", required=True,
                         help="comma-separated recipient addresses")
    deliver.add_argument("--sender", default="",
                         help="envelope sender address (required; no default is assumed)")
    deliver.add_argument("--transport", default="smtp2go",
                         help="smtp2go (ops) or fixture (tests/gates)")

    watcher = sub.add_parser("watcher", help="v0.1.0 continuous shadow-review watcher.")
    watcher.add_argument("op", choices=["activate", "run-once", "daemon",
                                        "status", "backfill",
                                        "replay-delivery", "pause-repo",
                                        "resume-repo", "reconcile"],
                         help="run-once: single poll; daemon: loop until "
                              "SIGTERM; status: state summary; backfill: "
                              "run-once; replay-delivery: "
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
    watcher.add_argument("--interval", type=int, default=-1,
                         help="daemon poll seconds (-1: env/default 300)")
    watcher.add_argument("--spend-cap", type=float, default=-1.0,
                         help="total pilot cap USD (-1: env/default 5.0)")
    watcher.add_argument("--run-dir", default="",
                         help="replay-delivery/reconcile target run dir")
    watcher.add_argument("--pr", default="",
                         help="directed backfill PR number (single --repos entry)")
    watcher.add_argument("--actual-usd", type=float, default=None,
                         help="reconcile: provider-confirmed actual USD")
    watcher.add_argument("--note", default="",
                         help="reconcile: evidence note (required)")
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
    from codatiel_i2 import watcher_manager as mgr_mod

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
    interval = int(args.interval) if args.interval is not None and args.interval >= 0 \
        else int(os.environ.get("CODATIEL_WATCH_INTERVAL", "300"))
    cap = float(args.spend_cap) if args.spend_cap is not None and args.spend_cap >= 0 \
        else float(os.environ.get("CODATIEL_WATCH_SPEND_CAP", "5.0"))
    daily = float(os.environ.get("CODATIEL_WATCH_DAILY_CAP", "2.0"))
    seed = float(os.environ.get("CODATIEL_WATCH_SEED_SPENT", "0.4226"))
    cfg = mgr_mod.ManagerConfig(
        repos=repos, exclude=exclude,
        runs_root=Path(runs_root), work_root=Path(work_root),
        recipients=to, sender=sender, spend_total_cap_usd=cap,
        spend_seed_usd=seed, spend_daily_cap_usd=daily,
        per_poll_cap=int(os.environ.get("CODATIEL_WATCH_PER_POLL_CAP",
                                        "3")))
    return cfg, state, interval


def cmd_watcher(args: Any) -> dict:
    import os
    import time as _time
    from codatiel_i2 import watcher as watcher_mod
    from codatiel_i2 import watcher_manager as mgr_mod
    from codatiel_i2 import watcher_store as store_mod

    op = args.op
    cfg, state_path, interval = watcher_config_from_env(args)
    if op == "activate":
        try:
            store = store_mod.Store(
                Path(state_path).with_suffix(".sqlite"))
        except store_mod.StoreError as exc:
            return {"status": "failed", "stage": "store",
                    "error": str(exc)}
        try:
            at = store.activate()
        except store_mod.StoreError as exc:
            return {"status": "failed", "stage": "activate",
                    "error": str(exc)}
        finally:
            store.close()
        return {"status": "ok", "activated_at": at}
    if op == "status":
        try:
            store = store_mod.Store(
                Path(state_path).with_suffix(".sqlite"))
        except store_mod.StoreError as exc:
            return {"status": "failed", "stage": "store",
                    "error": str(exc)}
        try:
            auth_mode, _ = watcher_mod.resolve_github_auth()
        except RuntimeError as exc:
            auth_mode = f"missing: {exc}"
        try:
            activated = store.require_activated()
        except store_mod.StoreError as exc:
            activated = f"missing: {exc}"
        return {"status": "ok",
                "auth": auth_mode,
                "activated_at": activated,
                "repos": cfg.repos, "exclude": cfg.exclude,
                "recipients": cfg.recipients,
                "spend_total_usd": store.spend_total(),
                "reserved_usd": store.reserved_total(),
                "spend_cap_usd": cfg.spend_total_cap_usd,
                "queued": store.count_status("queued"),
                "running": store.count_status("running"),
                "delivered": store.count_status("delivered")}
    if op in ("pause-repo", "resume-repo"):
        if not args.repos:
            return {"status": "failed", "stage": "config",
                    "error": "pass --repos owner/name,..."}
        try:
            from codatiel_i2 import watcher_store as _sm
            _st = _sm.Store(Path(state_path).with_suffix(".sqlite"))
        except Exception as exc:
            return {"status": "failed", "stage": "store",
                    "error": str(exc)}
        try:
            _st.cx.execute("CREATE TABLE IF NOT EXISTS repos_paused("
                           "repo TEXT PRIMARY KEY)")
            _st.cx.commit()
            for repo in [r.strip() for r in args.repos.split(",")
                         if r.strip()]:
                if op == "pause-repo":
                    _st.cx.execute("INSERT OR IGNORE INTO repos_paused(repo)"
                                   " VALUES(?)", (repo,))
                else:
                    _st.cx.execute("DELETE FROM repos_paused WHERE repo=?",
                                   (repo,))
            _st.cx.commit()
        except Exception as exc:
            return {"status": "failed", "stage": "store",
                    "error": str(exc)}
        finally:
            _st.close()
        return {"status": "paused" if op == "pause-repo" else "resumed",
                "repos": args.repos}
    if op == "reconcile":
        return cmd_watcher_reconcile(args)
    if op == "replay-delivery":
        return cmd_watcher_replay(args)
    if op == "backfill":
        return cmd_watcher_backfill(args)
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
    try:
        store = store_mod.Store(Path(state_path).with_suffix(".sqlite"))
        store.require_activated()
    except store_mod.StoreError as exc:
        return {"status": "failed", "stage": "store", "error": str(exc)}

    def sender(run_dir: Any, subject: str, body: str) -> dict:
        if run_dir is None:
            from codatiel_i2.delivery import smtp2go as smtp_mod
            return smtp_mod.send_email(cfg.recipients, cfg.sender,
                                       subject, body)
        from codatiel_i2 import service as service_mod
        return service_mod.deliver_shadow(
            run_dir, to=cfg.recipients, sender=cfg.sender,
            subject=subject, text_body=body)["transport_receipt"]

    if auth_mode == "app":
        try:
            os.environ["GH_TOKEN"] = app.token()
        except Exception as exc:
            return {"status": "failed", "stage": "auth", "error": str(exc)}

        def _app_repos() -> tuple[list[str] | None, str]:
            try:
                from codatiel_i2.github import app_auth as _am
                token = app.token()
                os.environ["GH_TOKEN"] = token
                names = _am.active_repo_names(
                    _am.list_installation_repos(token))
                return names, ""
            except Exception as exc:
                return None, f"{type(exc).__name__}: {exc}"
        lister = _app_repos
    else:
        lister = None
    if (store.spend_get("seed_usd") or 0.0) == 0.0:
        store.spend_set("seed_usd", cfg.spend_seed_usd)
    mgr = mgr_mod.Manager(cfg, store, sender=sender, list_repos=lister)
    try:
        mgr.check_in()
    except store_mod.StoreError as exc:
        return {"status": "failed", "stage": "lease",
                "error": str(exc)}
    recovery = mgr.recover()
    import signal as _signal
    stop = {"flag": False}

    def _handle(signum: int, frame: Any) -> None:
        mgr.stop()
        stop["flag"] = True

    if op == "daemon":
        _signal.signal(_signal.SIGTERM, _handle)
        _signal.signal(_signal.SIGINT, _handle)
    while True:
        try:
            mgr.check_in()
        except store_mod.StoreError as exc:
            return {"status": "failed", "stage": "lease",
                    "error": str(exc), "recovery": recovery}
        if auth_mode == "app":
            try:
                os.environ["GH_TOKEN"] = app.token()
            except Exception as exc:
                if op != "daemon":
                    return {"status": "failed", "stage": "auth",
                            "error": str(exc)}
                _time.sleep(min(interval, 300))
                continue
        try:
            summary = mgr.cycle()
        except store_mod.StoreError as exc:
            return {"status": "failed", "stage": "store",
                    "error": str(exc), "recovery": recovery}
        summary["recovery"] = recovery
        recovery = {"adopted": 0, "requeued": 0, "delivered_adopted": 0}
        Path(str(state_path) + ".heartbeat").write_text(
            json.dumps({"ts": _time.time(),
                        "spend_total_usd": store.spend_total(),
                        "summary": summary}, ensure_ascii=False),
            encoding="utf-8")
        if op != "daemon":
            summary["status"] = "ok"
            try:
                store.release_lease(mgr.instance_id)
            except Exception:
                pass
            store.close()
            return summary
        if stop["flag"]:
            summary["status"] = "ok"
            summary["stopped"] = True
            try:
                store.release_lease(mgr.instance_id)
            except Exception:
                pass
            store.close()
            return summary
        _time.sleep(interval)


def cmd_watcher_replay(args: Any) -> dict:
    """Resend one run's exact digest bytes without new inference.

    Store-backed: the deliveries table is authoritative. An already
    delivered (run_dir, body_sha) is suppressed as a duplicate; anything
    else resends the identical bytes via the manager email path.
    """
    import hashlib as _hashlib
    import os
    from codatiel_i2 import watcher as watcher_mod
    from codatiel_i2 import watcher_manager as mgr_mod
    from codatiel_i2 import watcher_store as store_mod

    cfg, state_path, _interval = watcher_config_from_env(args)
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
    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        metrics = json.loads((run_dir / "metrics.json").read_text(
            encoding="utf-8"))
    except Exception as exc:
        return {"status": "failed", "stage": "delivery",
                "error": f"unreadable run record: {exc}"}
    identity = {"repo": run.get("repo"), "number": run.get("pr_number"),
                "url": run.get("pr_url"), "base": run.get("base_sha"),
                "head": run.get("head_sha")}
    try:
        subj, body = mgr_mod.build_email(identity, run_dir)
    except ValueError as exc:
        return {"status": "failed", "stage": "delivery",
                "error": f"identity invalid, delivery blocked: {exc}"}
    sha = _hashlib.sha256(body.encode()).hexdigest()
    try:
        store = store_mod.Store(Path(state_path).with_suffix(".sqlite"))
    except store_mod.StoreError as exc:
        return {"status": "failed", "stage": "store", "error": str(exc)}
    existing = store.get_delivery(str(run_dir), sha)
    if existing and existing["status"] == "delivered":
        store.close()
        return {"status": "duplicate-suppressed", "subject": subj,
                "body_sha256": sha,
                "email_id": (existing.get("receipt") or {}).get("email_id")}
    # Adopt on-disk receipts (crash orphans): a delivered delivery.json
    # suppresses the resend and is adopted into the store.
    disk_path = run_dir / "delivery.json"
    if disk_path.exists():
        try:
            disk = json.loads(disk_path.read_text(encoding="utf-8"))
            from codatiel_i2 import watcher as _wm
            if (_wm.classify_receipt(disk.get("transport_receipt"))
                    == "delivered"
                    and disk.get("body_sha256") == sha):
                store.record_delivery(str(run_dir), sha, subj, "delivered",
                                      disk.get("transport_receipt") or {})
                store.close()
                return {"status": "duplicate-suppressed", "subject": subj,
                        "body_sha256": sha, "adopted_from_disk": True,
                        "email_id": (disk.get("transport_receipt") or {}
                                     ).get("email_id")}
        except Exception:
            pass
    from codatiel_i2 import service as service_mod

    def _send(t: list[str], s_: str, b: str) -> dict:
        return service_mod.deliver_shadow(
            str(run_dir), to=t, sender=sender, subject=s_,
            text_body=b)["transport_receipt"]

    try:
        receipt = _send(to, subj, body)
    except Exception as exc:
        store.record_delivery(str(run_dir), sha, subj, "failed",
                              {"error": f"{type(exc).__name__}: {exc}"})
        store.close()
        return {"status": "failed", "stage": "delivery",
                "error": f"{type(exc).__name__}: {exc}"}
    result = watcher_mod.classify_receipt(receipt)
    store.record_delivery(str(run_dir), sha, subj, result, receipt)
    store.close()
    return {"status": "delivered" if result == "delivered" else "failed",
            "subject": subj, "body_sha256": sha, "receipt": receipt}


def cmd_watcher_reconcile(args: Any) -> dict:
    """Replace an unknown-usage reservation with provider-confirmed actuals.

    Requires the run dir and the actual billed USD (from the provider
    dashboard) plus an evidence note. Releases the $1.00 conservative
    reservation and debits actuals instead.
    """
    from codatiel_i2 import watcher_store as store_mod

    cfg, state_path, _interval = watcher_config_from_env(args)
    run_dir = (args.run_dir or "").strip()
    if not run_dir:
        return {"status": "failed", "stage": "config",
                "error": "pass --run-dir"}
    if args.actual_usd is None or not (args.note or "").strip():
        return {"status": "failed", "stage": "config",
                "error": "pass --actual-usd and --note (evidence required)"}
    try:
        store = store_mod.Store(Path(state_path).with_suffix(".sqlite"))
    except store_mod.StoreError as exc:
        return {"status": "failed", "stage": "store", "error": str(exc)}
    row = store.cx.execute(
        "SELECT repo, pr, base, head, usd, spend_unknown, reserved_usd "
        "FROM pairs WHERE run_dir=?", (run_dir,)).fetchone()
    if not row:
        store.close()
        return {"status": "failed", "stage": "config",
                "error": "no pair recorded for that run dir"}
    repo, pr, base, head, _usd, unknown, reserved = row
    if not unknown:
        store.close()
        return {"status": "failed", "stage": "config",
                "error": "pair accounting is already known; nothing to do"}
    import time as _t
    day = _t.strftime("%Y-%m-%d", _t.gmtime())
    store.set_status(repo, pr, base, head, store.get_pair(
        repo, pr, base, head)["status"], usd=float(args.actual_usd),
                     spend_unknown=0, reserved_usd=0.0,
                     note=f"reconciled: {args.note.strip()}")
    store.spend_add(day, float(args.actual_usd))
    store.close()
    return {"status": "ok", "run_dir": run_dir,
            "actual_usd": float(args.actual_usd)}


def cmd_watcher_backfill(args: Any) -> dict:
    """Directed manual backfill of ONE repo+PR. Outside the polling path.

    Validates eligibility (open, not draft, valid SHAs) but bypasses the
    activation createdAt gate: the operator explicitly names the pair.
    Still subject to budget and delivery rules. One review only.
    """
    import time as _time
    from codatiel_i2 import watcher_eligibility as _E
    from codatiel_i2 import watcher_manager as _M
    from codatiel_i2 import watcher_store as _S

    cfg, state_path, _interval = watcher_config_from_env(args)
    repos = [r.strip() for r in (args.repos or "").split(",") if r.strip()]
    if len(repos) != 1 or not args.pr:
        return {"status": "failed", "stage": "config",
                "error": "directed backfill needs exactly --repos R and --pr N"}
    try:
        number = int(args.pr)
    except ValueError:
        return {"status": "failed", "stage": "config",
                "error": f"invalid --pr {args.pr!r}"}
    try:
        store = _S.Store(Path(state_path).with_suffix(".sqlite"))
        store.require_activated()
    except _S.StoreError as exc:
        return {"status": "failed", "stage": "store", "error": str(exc)}
    if not cfg.recipients or not cfg.sender:
        store.close()
        return {"status": "failed", "stage": "config",
                "error": "recipients and sender required"}
    from codatiel_i2 import watcher as _W
    try:
        _mode, _app = _W.resolve_github_auth()
        if _mode == "app":
            import os as _os
            _os.environ["GH_TOKEN"] = _app.token()
    except RuntimeError as exc:
        store.close()
        return {"status": "failed", "stage": "auth", "error": str(exc)}

    def _sender(run_dir: Any, subject: str, body: str) -> dict:
        from codatiel_i2 import service as _S2
        return _S2.deliver_shadow(
            run_dir, to=cfg.recipients, sender=cfg.sender,
            subject=subject, text_body=body)["transport_receipt"]

    mgr = _M.Manager(cfg, store, sender=_sender)
    try:
        mgr.check_in()
    except _S.StoreError as exc:
        store.close()
        return {"status": "failed", "stage": "lease", "error": str(exc)}
    import time as _t2
    if mgr.availability(_t2.strftime("%Y-%m-%d", _t2.gmtime())) <= 0:
        store.close()
        return {"status": "paused", "stage": "budget",
                "error": "spend caps exhausted; backfill refused"}
    raw, err = _E.fetch_pr(repos[0], number)
    if raw is None:
        store.close()
        return {"status": "failed", "stage": "fetch", "error": err}
    vpr, reason = _E.validate_pr_metadata(repos[0], raw)
    if vpr is None:
        store.close()
        return {"status": "failed", "stage": "validate",
                "error": reason}
    if vpr.is_draft or vpr.merged_at or vpr.state != "OPEN":
        store.close()
        return {"status": "failed", "stage": "validate",
                "error": "not open/ready (manual backfill still requires "
                         "open, ready, unmerged)"}
    store.upsert_pair(vpr.repo, vpr.number, vpr.base_sha, vpr.head_sha,
                      pr_url=vpr.url, pr_created_at=vpr.created_at)
    store.set_status(vpr.repo, vpr.number, vpr.base_sha, vpr.head_sha,
                     "eligible", note="manual directed backfill")
    rec = mgr.dispatch(vpr.repo, vpr.number, vpr.base_sha, vpr.head_sha,
                         mgr.day())
    store.close()
    return {"status": "ok", "backfill": rec}
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
    if getattr(args, "expected_base", "") or getattr(args, "expected_head", ""):
        kwargs["expected_base"] = getattr(args, "expected_base", "") or None
        kwargs["expected_head"] = getattr(args, "expected_head", "") or None
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
