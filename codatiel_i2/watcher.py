"""Codatiel v0.1 shared watcher helpers: GitHub auth resolution, the
legacy digest builder, and transport-receipt classification.

Production polling lives in watcher_manager.py (SQLite store,
activation-gated eligibility, subprocess-isolated dispatch). GitHub
stays read-only everywhere: discovery uses ``gh pr list`` /
``gh pr view`` only; nothing is posted, labelled, or mutated.
"""

from __future__ import annotations

from typing import Any


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


def classify_receipt(receipt: Any) -> str:
    """delivered | failed | partial | ambiguous — never invent certainty.

    Partial (some recipients accepted, some failed) is reported as its
    own state so callers retry the same bytes instead of marking the
    pair delivered and dropping the failed recipients.
    """
    if not isinstance(receipt, dict):
        return "ambiguous"
    ok = int(receipt.get("succeeded") or 0) >= 1
    bad = int(receipt.get("failed") or 0) >= 1
    if ok and not bad:
        return "delivered"
    if bad and not ok:
        return "failed"
    if ok and bad:
        return "partial"
    return "ambiguous"
