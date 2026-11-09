"""`codatiel-i2 semdup ...` operations (DEV-only, report-only).

- config-check: validate the DEV semdup env, echo non-secret config.
- build-base: synchronous persistent base-index build for (repo, rev).
- process-event: process one PR-delta payload through the DEV worker.
- report: render the latest run for (repo, pr); --send digests it to
  the configured DEV destination only (never a PR comment).
"""

from __future__ import annotations

import json
from pathlib import Path


def cmd_config_check(args) -> dict:
    from codatiel_i2.semdup_dev import config as C
    try:
        cfg = C.config_from_env()
    except C.SemdupConfigError as exc:
        return {"status": "failed", "stage": "config", "error": str(exc)}
    return {"status": "ok", "env": cfg.env,
            "state_dir": str(cfg.state_dir),
            "repos": cfg.repos,
            "investigator_url": cfg.investigator_url,
            "investigator_model": cfg.investigator_model,
            "investigator_expected": cfg.investigator_expected,
            "challenger_model": cfg.challenger_model,
            "challenger_expected": cfg.challenger_expected,
            "max_targets_per_delta": cfg.max_targets_per_delta,
            "neighbors_k": cfg.neighbors_k,
            "allow_embeddings": cfg.allow_embeddings,
            "notify_to": cfg.notify_to,
            "notify_sender": cfg.notify_sender,
            "qualified_default": C.QUALIFIED_MODEL_ID}


def _dev_store(state_dir: Path):
    from codatiel_i2 import event_store as store_mod
    state_dir.mkdir(parents=True, exist_ok=True)
    return store_mod.Store(state_dir / "events.sqlite")


def cmd_build_base(args) -> dict:
    from codatiel_i2.semdup_dev import config as C
    from codatiel_i2.semdup_dev import index_state as IX
    try:
        cfg = C.config_from_env()
    except C.SemdupConfigError as exc:
        return {"status": "failed", "stage": "config", "error": str(exc)}
    if not args.repo or not args.rev or not args.repo_path:
        return {"status": "failed", "stage": "config",
                "error": "build-base needs --repo-path, --repo and --rev"}
    if args.repo not in cfg.repos:
        return {"status": "failed", "stage": "config",
                "error": "repo not in DEV scope"}
    out = IX.ensure_base_index(Path(args.repo_path), args.repo, args.rev,
                               cfg.state_dir,
                               allow_embeddings=cfg.allow_embeddings)
    return {"status": "ok" if out.get("present") else "failed", **out}


def cmd_process_event(args) -> dict:
    from codatiel_i2 import event_store as store_mod
    from codatiel_i2.semdup_dev import config as C
    from codatiel_i2.semdup_dev import worker as W
    try:
        cfg = C.config_from_env()
    except C.SemdupConfigError as exc:
        return {"status": "failed", "stage": "config", "error": str(exc)}
    if not args.payload or not args.delivery_id or not args.repo_path:
        return {"status": "failed", "stage": "config",
                "error": "process-event needs --payload, --delivery-id "
                         "and --repo-path"}
    try:
        ev = json.loads(Path(args.payload).read_text())
    except (ValueError, OSError) as exc:
        return {"status": "failed", "stage": "payload",
                "error": f"unreadable payload: {exc}"}
    for key in ("repo", "base_rev", "head_rev"):
        if not ev.get(key):
            return {"status": "failed", "stage": "payload",
                    "error": f"payload missing {key!r}"}
    if ev.get("pr") is None or ev.get("pr") == "":
        return {"status": "failed", "stage": "payload",
                "error": "payload missing 'pr' (use 0 for synthetic deltas)"}
    try:
        pr = int(ev["pr"])
    except (TypeError, ValueError):
        return {"status": "failed", "stage": "payload",
                "error": "pr must be an integer"}
    try:
        store = _dev_store(cfg.state_dir)
    except store_mod.StoreError as exc:
        return {"status": "failed", "stage": "store", "error": str(exc)}
    try:
        out = W.process_delta(
            cfg=cfg, store=store, repo_path=Path(args.repo_path),
            repo=ev["repo"], pr=pr, base_rev=ev["base_rev"],
            head_rev=ev["head_rev"], delivery_id=args.delivery_id,
            action=str(ev.get("action", "semdup")),
            grandfather=bool(getattr(args, "grandfather", False)
                              or ev.get("grandfather")))
        return {"status": out.get("status", "unknown"), **out}
    except store_mod.StoreError as exc:
        return {"status": "failed", "stage": "store", "error": str(exc)}
    finally:
        store.close()


def render_digest(run_dir: Path) -> str:
    data = json.loads((run_dir / "findings.json").read_text())
    run = {}
    try:
        run = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        pass
    lines = ["Codatiel DEV semdup digest (report-only; no PR comments)",
             f"repo: {run.get('repo')} pr: {run.get('pr')}",
             f"base: {str(run.get('base_sha', ''))[:12]} "
             f"head: {str(run.get('head_sha', ''))[:12]}",
             f"status: {run.get('status')} "
             f"coverage: {data.get('retrieval_coverage')}",
             f"summary: {json.dumps(data.get('summary', {}))}"]
    for r in data.get("records", []):
        tgt = r.get("target") or {}
        lines.append(
            f"- [{r.get('status')}] {r.get('classification')} "
            f"{tgt.get('path')}::{tgt.get('symbol')} -> "
            f"{r.get('recommended_action')} "
            f"rel={r.get('relationship')} "
            f"counterparts={r.get('counterparts')}")
        if r.get("reason"):
            lines.append(f"  reason: {r.get('reason')}: {r.get('detail', '')}")
    return "\n".join(lines)


def cmd_report(args) -> dict:
    from codatiel_i2.semdup_dev import config as C
    from codatiel_i2.semdup_dev import index_state as IX
    try:
        cfg = C.config_from_env()
    except C.SemdupConfigError as exc:
        return {"status": "failed", "stage": "config", "error": str(exc)}
    if not args.repo or not args.pr:
        return {"status": "failed", "stage": "config",
                "error": "report needs --repo and --pr"}
    try:
        pr = int(args.pr)
    except (TypeError, ValueError):
        return {"status": "failed", "stage": "config",
                "error": "pr must be an integer"}
    root = (cfg.state_dir / "semdup" / "runs" / IX.slug(args.repo)
            / str(pr))
    if not root.exists():
        return {"status": "failed", "stage": "report",
                "error": "no semdup runs for repo/pr"}
    cands = sorted(root.glob("*/findings.json"))
    if args.head:
        cands = [c for c in cands
                 if c.parent.name == str(args.head)[:12]] or cands
    if not cands:
        return {"status": "failed", "stage": "report",
                "error": "no findings for head"}
    latest = cands[-1].parent
    body = render_digest(latest)
    out: dict = {"status": "ok", "run_dir": str(latest), "digest": body}
    if getattr(args, "send", False):
        from codatiel_i2 import service as service_mod
        if not cfg.notify_to or not cfg.notify_sender:
            return {"status": "failed", "stage": "report",
                    "error": "DEV_TO/DEV_SENDER required to send"}
        transport_kind = __import__("os").environ.get(
            "CODATIEL_EVENTS_TRANSPORT", "fixture")
        if transport_kind == "fixture":
            def transport(to_addrs, subject, text):
                return {"accepted_by_transport": True, "parsed": True,
                        "fixture": True, "succeeded": len(to_addrs),
                        "failed": 0, "email_id": "fixture-no-send"}
        elif transport_kind == "smtp2go":
            transport = None
        else:
            return {"status": "failed", "stage": "report",
                    "error": "unknown CODATIEL_EVENTS_TRANSPORT"}
        receipt = service_mod.deliver_shadow(
            latest, to=list(cfg.notify_to), sender=cfg.notify_sender,
            transport=transport, text_body=body,
            subject=f"[codatiel-dev-semdup] {args.repo}#{pr}")
        out["transport_receipt"] = receipt.get("transport_receipt")
    return out
