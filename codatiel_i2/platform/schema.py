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
  quarantined INTEGER NOT NULL DEFAULT 0,
  quarantine_note TEXT NOT NULL DEFAULT '',
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

    Additive columns land via tolerant ALTERs so a database created
    by an earlier Slice-1 revision upgrades in place; failures other
    than duplicate-column are fatal.

    Incompatible schemas fail closed BEFORE any write where that is
    promised, with a non-destructive diagnostic: the operator
    preserves the original file, backs it up, and — only then —
    initializes a fresh database at a separate path. Nothing here
    deletes, overwrites, or auto-migrates historical records. The
    four-column uniqueness and the schema-version are preflighted
    read-only before any DDL so an incompatible file is refused with
    zero writes and all legacy rows intact.
    """
    # Preflight read-only checks before any DDL, satisfying the
    # "fail closed BEFORE any write" promise for the two
    # incompatible-schema paths. No DDL has run yet.
    try:
        exists = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='pl_jobs'").fetchone()
        if exists is not None and not _has_four_column_unique(db):
            raise RuntimeError(_incompatible_diagnostic(
                db, "pl_jobs lacks the "
                "(repo,pr,target_generation,rerun_generation) uniqueness "
                "constraint, which SQLite cannot add after table creation"))
    except RuntimeError:
        raise
    except Exception:
        pass
    try:
        row0 = db.execute(
            "SELECT value FROM meta WHERE key='platform_schema_version'"
            ).fetchone()
        if row0 is not None and row0[0] != SCHEMA_VERSION:
            raise RuntimeError(_incompatible_diagnostic(
                db, f"schema version is {row0[0]!r}, need "
                f"{SCHEMA_VERSION!r}"))
    except RuntimeError:
        raise
    except Exception:
        pass
    db.executescript(SCHEMA_SQL)
    _require_unique_constraint(db)
    for table, column, ddl in (
            ("pl_pr_targets", "live_base",
             "ALTER TABLE pl_pr_targets ADD COLUMN live_base TEXT "
             "NOT NULL DEFAULT ''"),
            ("pl_pr_targets", "last_signal_head",
             "ALTER TABLE pl_pr_targets ADD COLUMN last_signal_head TEXT "
             "NOT NULL DEFAULT ''"),
            ("pl_pr_targets", "last_signal_base",
             "ALTER TABLE pl_pr_targets ADD COLUMN last_signal_base TEXT "
             "NOT NULL DEFAULT ''"),
            ("pl_pr_targets", "quarantined",
             "ALTER TABLE pl_pr_targets ADD COLUMN quarantined INTEGER "
             "NOT NULL DEFAULT 0"),
            ("pl_pr_targets", "quarantine_note",
             "ALTER TABLE pl_pr_targets ADD COLUMN quarantine_note TEXT "
             "NOT NULL DEFAULT ''"),
            ("pl_jobs", "comparison_base",
             "ALTER TABLE pl_jobs ADD COLUMN comparison_base TEXT "
             "NOT NULL DEFAULT ''"),
            ("pl_jobs", "dirty_seen",
             "ALTER TABLE pl_jobs ADD COLUMN dirty_seen INTEGER "
             "NOT NULL DEFAULT 0"),
            ("pl_jobs", "rerun_generation",
             "ALTER TABLE pl_jobs ADD COLUMN rerun_generation INTEGER "
             "NOT NULL DEFAULT 0")):
        try:
            db.execute(ddl)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
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
        raise RuntimeError(_incompatible_diagnostic(
            db, f"schema version is {(row[0] if row else None)!r}, "
            f"need {SCHEMA_VERSION!r}"))
    _require_capabilities(db)
    return SCHEMA_VERSION


def _db_path(db: sqlite3.Connection) -> str:
    try:
        rows = db.execute("PRAGMA database_list").fetchall()
    except Exception:
        return "<unknown>"
    for row in rows:
        if row[1] == "main":
            return row[2] or "<memory>"
    return "<unknown>"


def _incompatible_diagnostic(db: sqlite3.Connection, reason: str) -> str:
    """Non-destructive fail-closed diagnostic for unusable databases.

    Historical review outputs, findings, checkpoints, costs and
    publication records are valuable operational evidence and are
    preserved by default — re-fetching GitHub PRs is not an adequate
    replacement. Never delete or overwrite the original file.
    """
    return (
        f"platform database at {_db_path(db)} cannot be interpreted "
        f"safely ({reason}); refusing to run. "
        f"Operator action, in order, none destructive: "
        f"(1) preserve the original database file exactly as it is — "
        f"it still holds all historical reviews, checkpoints, spend "
        f"and publication records; "
        f"(2) take a SQLite-consistent backup BEFORE anything else "
        f"(e.g. sqlite3 original.db \".backup main 'backup.db'\" "
        f"with the database quiesced, keeping any -wal/-shm files "
        f"with it); "
        f"(3) initialize any fresh platform database at a SEPARATE "
        f"destination and import preserved history only through the "
        f"explicit, operator-run cutover import — never by replacing "
        f"this file. No automatic migration, backup management, or "
        f"version upgrade is performed by this release."
    )


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in
            db.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_four_column_unique(db: sqlite3.Connection) -> bool:
    sql = (db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                      "AND name='pl_jobs'").fetchone() or ("",))[0] or ""
    need = {"repo", "pr", "target_generation", "rerun_generation"}
    uniques = [part for part in sql.replace("\n", " ").split("UNIQUE(")
               if ")" in part]
    return any(need <= {c.strip().strip('"').strip("'").strip("[]").lower()
                        for c in part.split(")", 1)[0].split(",")}
               for part in uniques)


def _require_unique_constraint(db: sqlite3.Connection) -> None:
    """Pre-write gate: the four-column job uniqueness cannot be added
    after the fact, and without it routing's ON CONFLICT target is
    invalid and reruns cannot be distinguished. Checked before any
    ALTER so an incompatible database is refused with zero writes.
    """
    if not _has_four_column_unique(db):
        raise RuntimeError(_incompatible_diagnostic(
            db, "pl_jobs lacks the "
            "(repo,pr,target_generation,rerun_generation) uniqueness "
            "constraint, which SQLite cannot add after table creation"))


def _require_capabilities(db: sqlite3.Connection) -> None:
    """Fail closed on platform databases that cannot safely be used.

    Additive columns upgrade in place (tolerant ALTERs above). The
    uniqueness gate already ran pre-write; this final check confirms
    the columns landed and the constraint is present. Anything missing
    refuses with the non-destructive operator diagnostic — never with
    an instruction to delete history.
    """
    missing = {"quarantined", "quarantine_note", "live_base",
               "last_signal_head", "last_signal_base"} - _columns(
                   db, "pl_pr_targets")
    missing |= {"rerun_generation", "dirty_seen",
                "comparison_base"} - _columns(db, "pl_jobs")
    if missing:
        raise RuntimeError(_incompatible_diagnostic(
            db, f"pl_* tables lack columns {sorted(missing)}"))
    if not _has_four_column_unique(db):
        raise RuntimeError(_incompatible_diagnostic(
            db, "pl_jobs lacks the "
            "(repo,pr,target_generation,rerun_generation) uniqueness "
            "constraint, which SQLite cannot add after table creation"))
