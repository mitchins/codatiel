"""Codatiel v0.1 shared watcher helpers: GitHub auth resolution, the
legacy digest builder, and transport-receipt classification.

Production polling lives in watcher_manager.py (SQLite store,
activation-gated eligibility, subprocess-isolated dispatch). GitHub
stays read-only everywhere: discovery uses ``gh pr list`` /
``gh pr view`` only; nothing is posted, labelled, or mutated.
"""

from __future__ import annotations

import json
import subprocess
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
