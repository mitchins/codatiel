"""Codatiel v0.1.0 continuous shadow-review watcher.

Polls an explicit repository allowlist, runs the canonical agentic
``review_pr`` path (Luna-A default, one conditional Haiku follow-up),
and delivers a private email for every processed PR revision.
GitHub stays read-only: discovery uses ``gh pr list`` / ``gh pr view``
only; nothing is posted, labelled, or mutated.

All side effects flow through injected callables so the poll state
machine is deterministically testable without inference, network, or
GitHub. See tests/test_watcher_v01.py for the acceptance gates.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

STATE_SCHEMA = "codatiel_i2.watcher_state.v1"
CONFIG_VERSION = "v0.1.0|luna-A-canonical|haiku-conditional|max12x40|file-list-0.1"

# Luna statuses that trigger exactly one Haiku follow-up. Failed runs
# (infra, no model verdict) stay retryable instead: a second model
# cannot fix billing/auth/network.
HAIKU_TRIGGER_STATUSES = ("incomplete_budget_exhausted",
                          "unresolved_empty_final",
                          "unresolved_unparseable")


def resolve_github_auth() -> tuple[str, Any]:
    """App installation token preferred; read-only PAT fallback.

    Returns ("app", AppAuth) when GITHUB_APP_ID is configured, else
    ("token", None) after verifying GH_TOKEN exists for gh/git.
    """
    import os
    if (os.environ.get("GITHUB_APP_ID") or "").strip():
        from codatiel_i2.github import app_auth as app_mod
        return "app", app_mod.AppAuth.from_env()
    if (os.environ.get("GH_TOKEN") or "").strip():
        return "token", None
    raise RuntimeError("no GitHub auth: set GITHUB_APP_ID (+ private key) "
                       "or GH_TOKEN")


def resolve_repo_list(cfg: WatcherConfig, state: dict,
                      app: Any = None) -> tuple[list[str] | None, str | None]:
    """Auto-discover installation repos, minus excluded and paused.

    Explicit cfg.repos (token mode) is used verbatim. Returns (repos, error).
    """
    excluded = set(cfg.exclude)
    if cfg.repos is not None:
        return [r for r in cfg.repos if r not in excluded], None
    if app is None:
        return None, "auto-discovery needs GitHub App auth (GITHUB_APP_ID)"
    try:
        from codatiel_i2.github import app_auth as app_mod
        names = app_mod.active_repo_names(
            app_mod.list_installation_repos(app.token()))
    except Exception as exc:
        return None, f"discovery failed: {type(exc).__name__}: {exc}"
    paused = {name for name, e in (state.get("repos") or {}).items()
              if isinstance(e, dict) and e.get("paused")}
    return [n for n in names if n not in excluded and n not in paused], None


@dataclass
class WatcherConfig:
    repos: list[str] | None = None
    exclude: list[str] = field(default_factory=list)
    poll_interval_s: int = 300
    debounce_s: int = 120
    pr_limit: int = 20
    state_path: Path = Path("watcher_state.json")
    runs_root: Path = Path("runs")
    work_root: Path = Path(".codatiel-i2-work")
    recipients: list[str] = field(default_factory=list)
    sender: str = ""
    spend_total_cap_usd: float = 5.0
    spend_seed_usd: float = 0.389
    spend_daily_cap_usd: float = 2.0


def default_state() -> dict:
    return {"schema_version": STATE_SCHEMA,
            "config_version": CONFIG_VERSION,
            "prs": {},
            "repos": {},
            "spend": {"seed_usd": 0.0, "reviews_usd": 0.0,
                      "days": {}, "paused": False,
                      "paused_notified": False}}


def load_state(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("schema_version") == STATE_SCHEMA:
            raw.setdefault("prs", {})
            raw.setdefault("spend", default_state()["spend"])
            return raw
    except Exception:
        pass
    st = default_state()
    return st


def save_state(path: Path, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def spend_total(state: dict) -> float:
    sp = state.get("spend", {})
    return float(sp.get("seed_usd", 0.0) + sp.get("reviews_usd", 0.0))


def spend_today(state: dict, day: str) -> float:
    return float(state.get("spend", {}).get("days", {}).get(day, 0.0))


def debit_spend(state: dict, day: str, usd: float) -> None:
    sp = state.setdefault("spend", default_state()["spend"])
    sp["reviews_usd"] = float(sp.get("reviews_usd", 0.0)) + (usd or 0.0)
    days = sp.setdefault("days", {})
    days[day] = float(days.get(day, 0.0)) + (usd or 0.0)


def budget_exhausted(state: dict, day: str, cfg: WatcherConfig) -> bool:
    return (spend_total(state) >= cfg.spend_total_cap_usd
            or spend_today(state, day) >= cfg.spend_daily_cap_usd)


def pr_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def discover_prs(repo: str, limit: int = 20) -> tuple[list[dict] | None, str | None]:
    """Read-only open-PR discovery (gh pr list). No mutations, ever."""
    from codatiel_i2.github import tailer as tailer_mod
    return tailer_mod.discover_prs(repo, limit)


def fetch_pr_shas(repo: str, number: int) -> tuple[dict | None, str | None]:
    """Read-only SHA revalidation (gh pr view). No mutations, ever."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", repo,
             "--json", "number,headRefOid,baseRefOid,url"],
            capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, proc.stderr.strip()[:1000]
    try:
        row = json.loads(proc.stdout)
        return row if isinstance(row, dict) else None, None
    except Exception as exc:
        return None, f"parse error: {exc}"


def build_review_email(run_dir: Path, *, stale: bool = False,
                       second_pass: bool = False) -> tuple[str, str]:
    """Full self-contained private digest for one processed revision.

    Returns (subject, text_body). Stable per run: same run_dir always
    yields the same subject and body, which makes delivery idempotent.
    Model-authored claims are labelled unverified; only independently
    validated facts (SHAs, status, cost, evidence pointers) are stated.
    """
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    try:
        shadow = json.loads((run_dir / "shadow_result.json").read_text(
            encoding="utf-8"))
    except Exception:
        shadow = {}
    findings = []
    try:
        for line in (run_dir / "findings.jsonl").read_text(
                encoding="utf-8").splitlines():
            if line.strip():
                findings.append(json.loads(line))
    except Exception:
        findings = []
    ev: dict[str, dict] = {}
    try:
        for line in (run_dir / "evidence.jsonl").read_text(
                encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                ev[e.get("evidence_id")] = e
    except Exception:
        pass
    repo = run.get("repo", "?")
    pr = run.get("pr_number", "?")
    base = run.get("base_sha", "")
    head = run.get("head_sha", "")
    status = metrics.get("status", "?")
    model = metrics.get("model", "?")
    spend = metrics.get("spend", {})
    usd = spend.get("usd_estimate")
    cost = f"${usd:.4f}" if usd is not None else "unconfirmed (quota/unknown)"
    subj = (f"[codatiel-shadow] {repo}#{pr} "
            f"{base[:8]}..{head[:8]} {status} {run.get('run_id')}")
    if second_pass:
        subj += " (conditional Haiku follow-up)"
    lines = [
        f"repo: {repo}  PR: #{pr}",
        f"link: {run.get('pr_url', '(see repo PR list)')}",
        f"base: {base}  head: {head}",
        f"status: {status}{'  STALE-DURING-RUN (head moved mid-review)' if stale else ''}",
        f"model: {model}  instructions: {metrics.get('instructions_id', '?')}",
        f"config: {CONFIG_VERSION}",
        f"tool calls: {metrics.get('tool_calls')}  wall: "
        f"{metrics.get('wall_seconds', '?')}s  cost: {cost} "
        f"({spend.get('accounting_status', '?')})",
        f"run: {run.get('run_id')}",
        "",
    ]
    if second_pass:
        lines.append("This is the conditional Haiku follow-up to an "
                     "incomplete/unresolved Luna review, labelled "
                     "independently. It is not a second opinion on a "
                     "completed review.")
        lines.append("")
    verified = [f for f in findings
                if f.get("disposition") == "verified_proof"]
    verified.sort(key=lambda f: -(f.get("confidence") or 0))
    if status == "complete" and not verified:
        lines.append("NO MATERIAL FINDINGS after a completed investigation.")
        sil = (shadow.get("silence") or {}).get("summary") or \
            "(no silence rationale recorded)"
        lines.append(f"Silence rationale (model-authored, unverified): {sil}")
    for i, f in enumerate(verified, 1):
        anchor = f.get("changed_anchor") or {}
        lines.append(f"--- finding {i} (model-authored, UNVERIFIED) ---")
        lines.append(f"outcome: {f.get('bad_outcome', '(untitled)')}")
        lines.append(f"location: {anchor.get('path')} lines "
                     f"{anchor.get('line_start')}-{anchor.get('line_end')}")
        lines.append(f"trigger: {f.get('trigger', '?')}")
        lines.append(f"head behaviour: {f.get('head_state', '?')}")
        lines.append(f"base behaviour: {f.get('base_state', '?')}")
        lines.append(f"detector: {f.get('detector')}  model confidence: "
                     f"{f.get('confidence')}  finding: {f.get('finding_id')}")
        for eid in f.get("evidence_ids") or []:
            e = ev.get(eid, {})
            excerpt = (e.get("excerpt") or "")[:400].replace("\n", " ")
            lines.append(f"evidence {eid} {e.get('path', '?')}@"
                         f"{str(e.get('head_sha') or e.get('base_sha') or '')[:12]}: "
                         f"{excerpt}")
    if status != "complete" and not verified:
        lines.append("INCOMPLETE INVESTIGATION: absence of findings is not "
                     "evidence of a clean change.")
    lines += ["",
              "Uncertain areas: anything above marked model-authored is a "
              "review candidate, not a confirmed defect. Corroborate before "
              "acting.",
              f"Dismissed candidates: model verified "
              f"{len(findings)} candidate(s) and surfaced {len(verified)}; "
              "dismissals are model judgements, not proof of cleanliness.",
              "Private shadow artifact. Never post to GitHub."]
    return subj, "\n".join(lines)


def classify_receipt(receipt: Any) -> str:
    """delivered | failed | ambiguous — never invent certainty."""
    if not isinstance(receipt, dict):
        return "ambiguous"
    if int(receipt.get("succeeded") or 0) >= 1:
        return "delivered"
    if int(receipt.get("failed") or 0) >= 1:
        return "failed"
    return "ambiguous"


def poll_once(cfg: WatcherConfig, state: dict, *,
             now: float,
             day: str,
             discover: Callable = discover_prs,
             fetch_shas: Callable = fetch_pr_shas,
             review_luna: Callable | None = None,
             review_haiku: Callable | None = None,
             deliver: Callable | None = None,
             list_repos: Callable | None = None) -> dict:
    """One poll cycle. Returns a summary; mutates `state` (caller saves).

    Injected callables keep this deterministic in tests:
    - discover(repo, limit) -> (prs|None, error)
    - review_luna/review_haiku(repo, number) -> service.review_pr out-dict
    - deliver(run_dir, subject, body) -> transport receipt dict
    """
    summary: dict[str, Any] = {"discovered": 0, "reviewed": [],
                               "skipped": [], "emails": [], "paused": False,
                               "errors": []}
    if budget_exhausted(state, day, cfg):
        summary["paused"] = True
        sp = state["spend"]
        if not sp.get("paused_notified") and deliver is not None and cfg.recipients:
            subj = "[codatiel-shadow] paused-by-budget"
            body = (f"Codatiel v0.1.0 paused: spend cap reached "
                    f"(total ${spend_total(state):.4f} / "
                    f"${cfg.spend_total_cap_usd:.2f}, today "
                    f"${spend_today(state, day):.4f} / "
                    f"${cfg.spend_daily_cap_usd:.2f}). No new paid reviews "
                    f"will start until caps reset/raise.")
            receipt = deliver(None, subj, body)
            summary["emails"].append({"kind": "paused",
                                      "result": classify_receipt(receipt)})
            if classify_receipt(receipt) == "delivered":
                sp["paused_notified"] = True
        return summary
    if list_repos is not None:
        repos, list_error = list_repos()
    else:
        repos, list_error = resolve_repo_list(cfg, state)
    if repos is None:
        summary["errors"].append({"repos": list_error or "no repo list"})
        return summary
    for repo in repos:
        if repo in set(cfg.exclude):
            summary["skipped"].append({"repo": repo, "why": "excluded"})
            continue
        if isinstance((state.get("repos") or {}).get(repo), dict) and (
                state["repos"][repo].get("paused")):
            summary["skipped"].append({"repo": repo, "why": "paused"})
            continue
        try:
            prs, error = discover(repo, cfg.pr_limit)
        except Exception as exc:
            prs, error = None, f"{type(exc).__name__}: {exc}"
        if prs is None:
            summary["errors"].append({"repo": repo, "error": error})
            continue
        summary["discovered"] += len(prs)
        for pr in prs:
            try:
                number = int(pr.get("number"))
            except (TypeError, ValueError):
                continue
            base = str(pr.get("baseRefOid") or "")
            head = str(pr.get("headRefOid") or "")
            key = pr_key(repo, number)
            entry = state["prs"].setdefault(key, {})
            entry["last_seen"] = now
            entry["last_seen_base"] = base
            entry["last_seen_head"] = head
            done = {(r.get("base"), r.get("head")) for r in
                    entry.get("reviews", []) if r.get("delivered")}
            if (base, head) in done:
                summary["skipped"].append({"pr": key, "why": "unchanged_sha"})
                continue
            first_seen = entry.setdefault("first_seen_pair",
                                          {"base": base, "head": head,
                                           "at": now})
            if (first_seen.get("base"), first_seen.get("head")) != (base, head):
                entry["first_seen_pair"] = {"base": base, "head": head,
                                            "at": now}
                first_seen = entry["first_seen_pair"]
            if now - first_seen.get("at", now) < cfg.debounce_s:
                summary["skipped"].append({"pr": key, "why": "debounce"})
                continue
            if review_luna is None or deliver is None:
                summary["errors"].append({"pr": key,
                                          "error": "review/deliver not wired"})
                continue
            if budget_exhausted(state, day, cfg):
                summary["paused"] = True
                break
            # Delivery-pending pair: redeliver the existing run's exact
            # digest bytes (rebuilt identically) — never pay for a second
            # review to fix a transport failure.
            pending = [r for r in entry.get("reviews", [])
                       if (r.get("base"), r.get("head")) == (base, head)
                       and r.get("run_dir") and not r.get("delivered")]
            if pending:
                rec = pending[-1]
                try:
                    subj, body = build_review_email(Path(rec["run_dir"]))
                    receipt = deliver(rec["run_dir"], subj, body)
                    result = classify_receipt(receipt)
                except Exception as exc:
                    result = "transport-error"
                    summary["errors"].append({"pr": key,
                                              "error": f"{type(exc).__name__}: {exc}"})
                rec["delivery"] = result
                if result == "delivered":
                    rec["delivered"] = True
                summary["emails"].append({"pr": key, "result": result,
                                          "redelivery": True})
                summary["reviewed"].append({"pr": key, **rec,
                                            "redelivered": True})
                continue
            try:
                out = review_luna(repo, number)
            except Exception as exc:
                out = {"status": "failed", "stage": "agent",
                       "error": f"{type(exc).__name__}: {exc}"}
            rec = register_review(state, entry, base, head, out, day,
                                  second_pass=False)
            stale = False
            if out.get("run_dir") and not all(
                    (Path(out["run_dir"]) / f).exists()
                    for f in ("run.json", "metrics.json",
                              "shadow_result.json")):
                rec["error"] = "run artifacts missing; no delivery attempted"
                summary["errors"].append({"pr": key,
                                          "error": rec["error"]})
            if out.get("run_dir") and "error" not in rec:
                try:
                    live, live_error = fetch_shas(repo, number)
                except Exception as exc:
                    live, live_error = None, f"{type(exc).__name__}: {exc}"
                if live_error:
                    summary["errors"].append({"pr": key,
                                              "error": live_error})
                if live and (str(live.get("baseRefOid") or ""),
                             str(live.get("headRefOid") or "")) != (base, head):
                    stale = True
                    rec["stale_during_run"] = True
                try:
                    subj, body = build_review_email(out["run_dir"],
                                                    stale=stale)
                    receipt = deliver(out["run_dir"], subj, body)
                    result = classify_receipt(receipt)
                except Exception as exc:
                    result = "transport-error"
                    summary["errors"].append({"pr": key,
                                              "error": f"{type(exc).__name__}: {exc}"})
                rec["delivery"] = result
                summary["emails"].append({"pr": key, "result": result,
                                          "stale": stale})
                if result == "delivered":
                    rec["delivered"] = True
            summary["reviewed"].append({"pr": key, **rec})
            if (out.get("run_dir") and review_haiku is not None
                    and out.get("status") in HAIKU_TRIGGER_STATUSES
                    and not budget_exhausted(state, day, cfg)):
                try:
                    out2 = review_haiku(repo, number)
                except Exception as exc:
                    out2 = {"status": "failed", "stage": "agent",
                            "error": f"{type(exc).__name__}: {exc}"}
                rec2 = register_review(state, entry, base, head, out2, day,
                                       second_pass=True)
                if out2.get("run_dir"):
                    try:
                        subj2, body2 = build_review_email(
                            out2["run_dir"], second_pass=True)
                        receipt2 = deliver(out2["run_dir"], subj2, body2)
                        result2 = classify_receipt(receipt2)
                    except Exception as exc:
                        result2 = "transport-error"
                        summary["errors"].append(
                            {"pr": key, "error": f"{type(exc).__name__}: {exc}"})
                    rec2["delivery"] = result2
                    summary["emails"].append({"pr": key, "result": result2,
                                              "follow_up": True})
                    if result2 == "delivered":
                        rec2["delivered"] = True
                summary["reviewed"].append({"pr": key, **rec2})
    return summary


def register_review(state: dict, entry: dict, base: str, head: str,
                    out: dict, day: str, second_pass: bool) -> dict:
    """Record one review attempt; debit actual spend. No delivery here."""
    rec: dict[str, Any] = {"base": base, "head": head,
                           "status": out.get("status"),
                           "stage": out.get("stage"),
                           "run_dir": out.get("run_dir"),
                           "model": (out.get("model")
                                     or ("haiku" if second_pass else "luna")),
                           "second_pass": second_pass,
                           "delivered": False}
    if out.get("run_dir"):
        try:
            metrics = json.loads((Path(out["run_dir"]) / "metrics.json")
                                 .read_text(encoding="utf-8"))
            usd = (metrics.get("spend") or {}).get("usd_estimate")
            usd = float(usd) if usd is not None else 0.0
        except Exception:
            usd = None
        if usd is None:
            # Never invent a zero: unreadable spend stays unknown and the
            # pair stays out of the delivered set, so it is revisited.
            rec["usd"] = "unknown"
            rec["spend_unknown"] = True
        else:
            debit_spend(state, day, usd)
            rec["usd"] = usd
    else:
        rec["error"] = out.get("error", "no run_dir")
    entry.setdefault("reviews", []).append(rec)
    return rec
