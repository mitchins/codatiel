"""Versioned operator settings: the database is authoritative.

`import_operator_json` runs once at migration: it validates the legacy
`operator.json` with the existing validation schema and records it as
configuration revision 1. After that the JSON file is export-only, not
a second live writer. Every later change appends a revision plus an
audit row (actor, action, detail). `resolve_policy` maps a revision to
the admission/dispatch gates the queue enforces.
"""

from __future__ import annotations

import json
import sqlite3
import time

from codatiel_i2.platform.db import tx

CURRENT_ACTOR = "operator"


def current_revision(db: sqlite3.Connection) -> dict | None:
    row = db.execute("SELECT revision,doc,actor,ts FROM pl_config_revisions "
                     "ORDER BY revision DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {"revision": row[0], "doc": json.loads(row[1]),
            "actor": row[2], "ts": row[3]}


def _append(db: sqlite3.Connection, doc: dict, actor: str, action: str,
            detail: dict, ts: float) -> int:
    blob = json.dumps(doc, sort_keys=True)
    cur = db.execute("INSERT INTO pl_config_revisions(doc,actor,ts) "
                     "VALUES(?,?,?)", (blob, actor, ts))
    rev = int(cur.lastrowid or 0)
    db.execute("INSERT INTO pl_config_audit(revision,action,actor,detail,ts)"
               " VALUES(?,?,?,?,?)",
               (rev, action, actor, json.dumps(detail, sort_keys=True), ts))
    return int(rev)


def import_operator_json(db: sqlite3.Connection, doc: dict,
                         actor: str = "migration",
                         now: float | None = None) -> int:
    """One-time import of the legacy operator.json document.

    Reuses the existing validation schema; refuses unvalidated or
    second imports (JSON is export-only afterwards).
    """
    from codatiel_i2 import operator_config as op_mod
    ts = time.time() if now is None else now
    with tx(db):
        if current_revision(db) is not None:
            raise RuntimeError("settings already imported; JSON is "
                               "export-only, change settings by revision")
        errors = op_mod.validate_doc(doc)
        if errors:
            raise ValueError(f"operator.json invalid: {errors[:3]}")
        rev = _append(db, doc, actor, "import_operator_json",
                      {"schema": op_mod.SCHEMA_VERSION}, ts)
    return rev


def update(db: sqlite3.Connection, patch: dict, actor: str = CURRENT_ACTOR,
           now: float | None = None) -> int:
    """Append a validated revision. Unknown keys refused, never ignored.

    Reachability is judged against the schema's KNOWN_KEYS (not the
    keys present in the current revision), so a key introduced after
    the one-time import — e.g. pause_publication — can still be set.
    """
    from codatiel_i2 import operator_config as op_mod
    ts = time.time() if now is None else now
    with tx(db):
        cur = current_revision(db)
        if cur is None:
            raise RuntimeError("no settings revision; import first")
        base = dict(cur["doc"])
        for key in patch:
            if key not in op_mod.KNOWN_KEYS:
                raise ValueError(f"unknown settings key {key!r}")
        merged = {**base, **patch}
        errors = op_mod.validate_doc(merged)
        if errors:
            raise ValueError(f"settings invalid: {errors[:3]}")
        rev = _append(db, merged, actor, "update", {"patch": patch}, ts)
    return rev


def resolve_policy(doc: dict, *, env_monthly_usd: float,
                   env_per_pr_usd: float | None = None,
                   env_repos: list[str] | None = None) -> dict:
    """Map a settings revision to queue admission/dispatch gates.

    Reuses the existing effective-budget semantics: a stale file value
    can only narrow, never raise, the env limits.
    """
    from codatiel_i2 import operator_config as op_mod
    settings = op_mod.parse(doc)
    caps = op_mod.effective_spend_caps(
        settings, env_monthly_usd=env_monthly_usd,
        env_per_pr_usd=env_per_pr_usd)
    repos = op_mod.effective_repos(settings, list(env_repos or []))
    # pause_publication unifies email under the same dispatch gate as
    # comments (deliberate change from the PROD-02 email path). The new
    # key wins when present; otherwise the legacy comment pause carries
    # over so migration never silently unpauses publication.
    pause_publication = (settings.pause_publication
                         if settings.pause_publication is not None
                         else settings.pause_github_comments)
    return {
        "monthly_effective_usd": caps["monthly_effective_usd"],
        "daily_effective_usd": caps.get("daily_effective_usd"),
        "per_pr_effective_usd": caps.get("per_pr_effective_usd"),
        "repos": repos,
        "pause_reviews": bool(settings.pause_reviews),
        "pause_publication": bool(pause_publication),
        "email_allowed": bool(op_mod.email_sending_allowed(settings)),
    }


def to_policy(resolved: dict) -> "Policy":
    """Build the queue admission/dispatch Policy from resolve_policy.

    The only supported construction path: Policy must never be
    hand-built at call sites, or a pause default can silently diverge.
    Budget caps travel separately: the worker applies the resolved
    monthly/daily/per-PR amounts via set_budget at startup, so caps
    stay in the versioned settings while the ledger enforces them.
    """
    from codatiel_i2.platform.queue import Policy
    repos = resolved.get("repos")
    return Policy(
        pause_reviews=bool(resolved.get("pause_reviews", False)),
        pause_publication=bool(resolved.get("pause_publication", False)),
        email_allowed=bool(resolved.get("email_allowed", True)),
        # None (key absent) = no constraint configured; () = the
        # effective set is empty, i.e. deny all, fail closed.
        allowed_repos=(tuple(repos) if repos is not None else None),
    )
