"""Additive platform schema (Slice 1: storage + application contract).

New `pl_*` tables coexist with the v1 `deliveries/checkpoints/reviews/
publications/spend` tables in the same SQLite file; no v1 table is
altered. Data preservation (v1 -> platform import) lives in
`codatiel_i2.platform.migrate`; this module only ensures storage.

Work states: pending, leased, running, retry_wait, deferred_budget,
awaiting_verification, succeeded, superseded, cancelled, failed,
needs_attention. Effect states: pending, leased, sending, confirmed,
retry_wait, suppressed, unknown, failed.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "codatiel_i2.platform.v1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pl_inbox(
  app TEXT NOT NULL, guid TEXT NOT NULL, digest TEXT NOT NULL,
  repo TEXT NOT NULL, repo_id INTEGER, pr INTEGER NOT NULL,
  head TEXT NOT NULL DEFAULT '', lifecycle TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending', received_ts REAL NOT NULL,
  PRIMARY KEY(app,guid));
CREATE TABLE IF NOT EXISTS pl_receipt_attempts(
  id INTEGER PRIMARY KEY, app TEXT NOT NULL, guid TEXT NOT NULL,
  outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
  ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_pl_receipts_guid
  ON pl_receipt_attempts(app,guid);
CREATE TABLE IF NOT EXISTS pl_pr_targets(
  repo TEXT NOT NULL, pr INTEGER NOT NULL,
  repo_id INTEGER, app TEXT NOT NULL DEFAULT '',
  lifecycle TEXT NOT NULL DEFAULT 'open',
  lifecycle_version INTEGER NOT NULL DEFAULT 0,
  target_generation INTEGER NOT NULL DEFAULT 0,
  dirty_rev INTEGER NOT NULL DEFAULT 0,
  live_head TEXT NOT NULL DEFAULT '',
  live_observed REAL NOT NULL DEFAULT 0,
  live_base TEXT NOT NULL DEFAULT '',
  last_signal_head TEXT NOT NULL DEFAULT '',
  last_signal_base TEXT NOT NULL DEFAULT '',
  reviewed_head TEXT NOT NULL DEFAULT '',
  reviewed_base TEXT NOT NULL DEFAULT '',
  recon_owner TEXT NOT NULL DEFAULT '',
  recon_fence INTEGER NOT NULL DEFAULT 0,
  recon_expires REAL NOT NULL DEFAULT 0,
  updated_ts REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(repo,pr));
CREATE TABLE IF NOT EXISTS pl_jobs(
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL,
  target_generation INTEGER NOT NULL,
  mode TEXT NOT NULL DEFAULT 'incremental',
  full_base TEXT NOT NULL DEFAULT '',
  comparison_base TEXT NOT NULL DEFAULT '',
  full_head TEXT NOT NULL DEFAULT '',
  diff_hash TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL DEFAULT '',
  rerun_generation INTEGER NOT NULL DEFAULT 0,
  dirty_seen INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending',
  owner TEXT NOT NULL DEFAULT '', fence INTEGER NOT NULL DEFAULT 0,
  lease_expires REAL NOT NULL DEFAULT 0, attempt INTEGER,
  created_ts REAL NOT NULL DEFAULT 0,
  UNIQUE(repo,pr,target_generation,rerun_generation),
  FOREIGN KEY(attempt) REFERENCES pl_attempts(id));
CREATE TABLE IF NOT EXISTS pl_attempts(
  id INTEGER PRIMARY KEY, job INTEGER NOT NULL,
  fence INTEGER NOT NULL, reserve_micro INTEGER NOT NULL,
  actual_micro INTEGER, phase TEXT NOT NULL DEFAULT 'reserved',
  state TEXT NOT NULL DEFAULT 'reserved',
  FOREIGN KEY(job) REFERENCES pl_jobs(id));
CREATE TABLE IF NOT EXISTS pl_scopes(
  attempt INTEGER NOT NULL, scope TEXT NOT NULL,
  PRIMARY KEY(attempt,scope),
  FOREIGN KEY(attempt) REFERENCES pl_attempts(id),
  FOREIGN KEY(scope) REFERENCES pl_budgets(name));
CREATE TABLE IF NOT EXISTS pl_budgets(
  name TEXT PRIMARY KEY, cap_micro INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pl_provider_calls(
  id INTEGER PRIMARY KEY, attempt INTEGER NOT NULL,
  stage TEXT NOT NULL, call_id TEXT NOT NULL UNIQUE,
  phase TEXT NOT NULL DEFAULT 'intent', ts REAL NOT NULL,
  FOREIGN KEY(attempt) REFERENCES pl_attempts(id));
CREATE TABLE IF NOT EXISTS pl_results(
  job INTEGER PRIMARY KEY, attempt INTEGER NOT NULL UNIQUE,
  run_path TEXT NOT NULL, manifest_sha TEXT NOT NULL,
  digest TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(job) REFERENCES pl_jobs(id),
  FOREIGN KEY(attempt) REFERENCES pl_attempts(id));
CREATE TABLE IF NOT EXISTS pl_effects(
  id INTEGER PRIMARY KEY, job INTEGER NOT NULL,
  channel TEXT NOT NULL, destination TEXT NOT NULL DEFAULT '',
  content_revision TEXT NOT NULL DEFAULT '',
  target_generation INTEGER NOT NULL,
  lifecycle_version INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  token TEXT NOT NULL DEFAULT '', lease_expires REAL NOT NULL DEFAULT 0,
  remote_id TEXT NOT NULL DEFAULT '',   uncertain INTEGER NOT NULL DEFAULT 0,
  UNIQUE(job,channel,destination,content_revision),
  FOREIGN KEY(job) REFERENCES pl_jobs(id));
CREATE TABLE IF NOT EXISTS pl_effect_attempts(
  id INTEGER PRIMARY KEY, effect INTEGER NOT NULL,
  phase TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '',
  remote_id TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
  FOREIGN KEY(effect) REFERENCES pl_effects(id));
CREATE TABLE IF NOT EXISTS pl_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
  ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_pl_events_seq ON pl_events(seq);
CREATE TABLE IF NOT EXISTS pl_config_revisions(
  revision INTEGER PRIMARY KEY AUTOINCREMENT, doc TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pl_config_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pl_operator(
  id TEXT PRIMARY KEY, created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pl_sessions(
  token_hash TEXT PRIMARY KEY, operator_id TEXT NOT NULL,
  created_ts REAL NOT NULL, expires_ts REAL NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0, last_seen_ts REAL NOT NULL DEFAULT 0,
  FOREIGN KEY(operator_id) REFERENCES pl_operator(id));
CREATE TABLE IF NOT EXISTS pl_recon(
  kind TEXT PRIMARY KEY, repo TEXT NOT NULL DEFAULT '',
  pr INTEGER NOT NULL DEFAULT 0, watermark TEXT NOT NULL DEFAULT '',
  updated_ts REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pl_openings(
  scope TEXT PRIMARY KEY, micro INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS pl_publications_imported(
  repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '', comment_id TEXT NOT NULL,
  outcome TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  created_ts REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(repo,pr,head_sha));
"""


def ensure(db: sqlite3.Connection) -> str:
    """Create platform tables; record schema version in meta. Idempotent.

    The version insert tolerates a lost first-start race: two
    concurrent fresh starts both create tables (IF NOT EXISTS), then
    exactly one wins the version row and the loser re-reads it.
    """
    db.executescript(SCHEMA_SQL)
    row = db.execute("SELECT value FROM meta WHERE key=?",
                     ("platform_schema_version",)).fetchone()
    if row is None:
        try:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?)",
                       ("platform_schema_version", SCHEMA_VERSION))
        except sqlite3.IntegrityError:
            row = db.execute("SELECT value FROM meta WHERE key=?",
                             ("platform_schema_version",)).fetchone()
    if row is None:
        row = db.execute("SELECT value FROM meta WHERE key=?",
                         ("platform_schema_version",)).fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        raise RuntimeError(
            f"platform schema mismatch: have "
            f"{(row[0] if row else None)!r}, need {SCHEMA_VERSION!r}; "
            f"refusing to run")
    return SCHEMA_VERSION
