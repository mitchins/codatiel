"""Codatiel v0.1.1 production store: SQLite-backed watcher state.

One file, WAL mode, explicit transactions. No silent fallbacks:
- missing DB / missing activation -> caller must refuse (raised, never defaulted)
- corrupt / unreadable / schema-mismatched DB -> StoreError (refuse to run)
- failed writes -> StoreError (stop dispatch)
- concurrent instances -> lease arbitration (second instance refused)

Tables:
  meta         single row: schema_version, activated_at, config_version
  pairs        one row per (repo, pr, base, head): lifecycle state machine
                 discovered -> eligible -> queued -> running ->
                 complete | failed | suppressed (+ delivered flag via deliveries)
  spend        aggregate counters + seed; reservations tracked per pair
  deliveries   durable intent/receipt records keyed by (run_dir, body_sha)
  lease        single-instance heartbeat row
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "codatiel_i2.watchstore.v1"

_BEGIN = "BEGIN IMMEDIATE;"
_COMMIT = "COMMIT;"
_ROLLBACK = "ROLLBACK;"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pairs (
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    base TEXT NOT NULL,
    head TEXT NOT NULL,
    pr_url TEXT NOT NULL DEFAULT '',
    pr_created_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'discovered',
    run_dir TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    second_pass INTEGER NOT NULL DEFAULT 0,
    usd REAL,
    spend_unknown INTEGER NOT NULL DEFAULT 0,
    reserved_usd REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    updated_ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, pr, base, head)
);
CREATE INDEX IF NOT EXISTS idx_pairs_status ON pairs(status);
CREATE TABLE IF NOT EXISTS spend (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries (
    run_dir TEXT NOT NULL,
    body_sha TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    receipt TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (run_dir, body_sha)
);
CREATE TABLE IF NOT EXISTS repos_paused (
    repo TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS lease (
    key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    heartbeat REAL NOT NULL
);
-- SETTLE-01: pending review-settle state. One row per PR: the latest
-- observed head, when it was first seen (never reset by later pushes
-- within the episode) and when it last changed (restarted by every new
-- head). Survives daemon restarts; cleared on dispatch, cancellation
-- (close/merge/pause) or an already-reviewed head.
CREATE TABLE IF NOT EXISTS pending_settle (
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    base TEXT NOT NULL,
    head TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_change REAL NOT NULL,
    override_by TEXT NOT NULL DEFAULT '',
    override_reason TEXT NOT NULL DEFAULT '',
    updated_ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, pr)
);
"""


class StoreError(RuntimeError):
    """Any persistence problem: caller must refuse/stop, never default."""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.cx = sqlite3.connect(str(self.path), timeout=30.0,
                                      isolation_level=None)
            self.cx.execute("PRAGMA journal_mode=WAL;")
            self.cx.execute("PRAGMA foreign_keys=ON;")
            self.cx.executescript(SCHEMA_SQL)
        except Exception as exc:
            raise StoreError(f"cannot open store {self.path}: "
                             f"{type(exc).__name__}: {exc}")
        try:
            got = self.meta_get("schema_version")
            if got is None:
                self.meta_set("schema_version", SCHEMA_VERSION)
            elif got != SCHEMA_VERSION:
                raise StoreError(
                    f"schema mismatch at {self.path}: have {got!r}, "
                    f"need {SCHEMA_VERSION!r}; refusing to run")
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"unreadable store {self.path}: "
                             f"{type(exc).__name__}: {exc}")
        for key in ("seed_usd", "reviews_usd"):
            if self.spend_get(key) is None:
                self.spend_set(key, 0.0)

    # -- meta -----------------------------------------------------------
    def meta_get(self, key: str) -> str | None:
        try:
            row = self.cx.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except Exception as exc:
            raise StoreError(f"meta read failed: {exc}")
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"meta write failed: {exc}")

    def require_activated(self) -> str:
        at = self.meta_get("activated_at")
        if not at:
            raise StoreError("store has no activation timestamp; "
                             "run `watcher activate` first (refusing to run)")
        return at

    def activate(self) -> str:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.cx.execute(_BEGIN)
            row = self.cx.execute(
                "SELECT value FROM meta WHERE key='activated_at'").fetchone()
            if row and row[0]:
                raise StoreError(
                    "already activated at "
                    f"{row[0]}; production paths cannot silently reset "
                    "the boundary")
            self.cx.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES"
                "('activated_at', ?)", (now,))
            self.cx.execute(_COMMIT)
        except StoreError:
            raise
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"activation failed: {exc}")
        return now

    # -- spend ----------------------------------------------------------
    def spend_get(self, key: str) -> float | None:
        row = self.cx.execute(
            "SELECT value FROM spend WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else None

    def spend_set(self, key: str, value: float) -> None:
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "INSERT INTO spend(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, float(value)))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"spend write failed: {exc}")

    def spend_total(self) -> float:
        return (self.spend_get("seed_usd") or 0.0) + (
            self.spend_get("reviews_usd") or 0.0)

    def spend_day(self, day: str) -> float:
        return self.spend_get(f"day:{day}") or 0.0

    def spend_add(self, day: str, usd: float) -> None:
        try:
            self.cx.execute(_BEGIN)
            for key, val in (("reviews_usd",
                              (self.spend_get("reviews_usd") or 0.0) + usd),
                             (f"day:{day}",
                              (self.spend_get(f"day:{day}") or 0.0) + usd)):
                self.cx.execute(
                    "INSERT INTO spend(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"spend debit failed: {exc}")

    def reserved_total(self) -> float:
        row = self.cx.execute(
            "SELECT COALESCE(SUM(reserved_usd), 0) FROM pairs").fetchone()
        return float(row[0]) if row else 0.0

    # -- pairs ----------------------------------------------------------
    VALID_STATUSES = ("discovered", "eligible", "queued", "running",
                      "complete", "failed", "suppressed", "delivered")

    def upsert_pair(self, repo: str, pr: int, base: str, head: str, *,
                    pr_url: str = "", pr_created_at: str = "") -> None:
        if "" in (repo, base, head) or not pr:
            raise StoreError("refusing to store identity-deficient pair")
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                """INSERT INTO pairs(repo, pr, base, head, pr_url,
                                     pr_created_at, status, updated_ts)
                   VALUES(?, ?, ?, ?, ?, ?, 'discovered', ?)
                   ON CONFLICT(repo, pr, base, head) DO UPDATE SET
                     pr_url=excluded.pr_url,
                     pr_created_at=excluded.pr_created_at,
                     updated_ts=excluded.updated_ts""",
                (repo, pr, base, head, pr_url, pr_created_at, time.time()))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"pair upsert failed: {exc}")

    def set_status(self, repo: str, pr: int, base: str, head: str,
                   status: str, **fields: Any) -> None:
        if status not in self.VALID_STATUSES:
            raise StoreError(f"invalid pair status {status!r}")
        allowed = {"run_dir", "model", "second_pass", "usd",
                   "spend_unknown", "reserved_usd", "note"}
        bad = set(fields) - allowed
        if bad:
            raise StoreError(f"invalid pair fields {sorted(bad)}")
        sets = ["status=?", "updated_ts=?"]
        vals: list[Any] = [status, time.time()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals += [repo, pr, base, head]
        try:
            self.cx.execute(_BEGIN)
            cur = self.cx.execute(
                f"UPDATE pairs SET {', '.join(sets)} WHERE repo=? AND pr=? "
                "AND base=? AND head=?", vals)
            if cur.rowcount != 1:
                raise StoreError("pair row missing for status transition")
            self.cx.execute(_COMMIT)
        except StoreError:
            raise
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"status transition failed: {exc}")

    def get_pair(self, repo: str, pr: int, base: str,
                 head: str) -> dict | None:
        row = self.cx.execute(
            "SELECT repo, pr, base, head, pr_url, pr_created_at, status, "
            "run_dir, model, second_pass, usd, spend_unknown, reserved_usd, "
            "note FROM pairs WHERE repo=? AND pr=? AND base=? AND head=?",
            (repo, pr, base, head)).fetchone()
        if not row:
            return None
        keys = ("repo", "pr", "base", "head", "pr_url", "pr_created_at",
                "status", "run_dir", "model", "second_pass", "usd",
                "spend_unknown", "reserved_usd", "note")
        return dict(zip(keys, row))

    def count_status(self, *statuses: str) -> int:
        q = ",".join("?" for _ in statuses)
        row = self.cx.execute(
            f"SELECT COUNT(*) FROM pairs WHERE status IN ({q})",
            statuses).fetchone()
        return int(row[0]) if row else 0

    def queued_pairs(self, limit: int) -> list[dict]:
        rows = self.cx.execute(
            "SELECT repo, pr, base, head, pr_url, pr_created_at, status, "
            "run_dir, model, second_pass FROM pairs WHERE status='queued' "
            "ORDER BY updated_ts ASC LIMIT ?", (limit,)).fetchall()
        keys = ("repo", "pr", "base", "head", "pr_url", "pr_created_at",
                "status", "run_dir", "model", "second_pass")
        return [dict(zip(keys, r)) for r in rows]

    # -- deliveries -----------------------------------------------------
    def record_delivery(self, run_dir: str, body_sha: str, subject: str,
                        status: str, receipt: dict) -> None:
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                """INSERT INTO deliveries(run_dir, body_sha, subject, status,
                                          receipt, attempts, updated_ts)
                   VALUES(?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(run_dir, body_sha) DO UPDATE SET
                     subject=excluded.subject, status=excluded.status,
                     receipt=excluded.receipt,
                     attempts=deliveries.attempts+1,
                     updated_ts=excluded.updated_ts""",
                (run_dir, body_sha, subject, status,
                 json.dumps(receipt), time.time()))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"delivery record failed: {exc}")

    def get_delivery(self, run_dir: str, body_sha: str) -> dict | None:
        row = self.cx.execute(
            "SELECT subject, status, receipt, attempts FROM deliveries "
            "WHERE run_dir=? AND body_sha=?",
            (run_dir, body_sha)).fetchone()
        if not row:
            return None
        return {"subject": row[0], "status": row[1],
                "receipt": json.loads(row[2]), "attempts": row[3]}

    def paused_repos(self) -> set[str]:
        try:
            rows = self.cx.execute(
                "SELECT repo FROM repos_paused").fetchall()
        except Exception:
            return set()
        return {r[0] for r in rows}

    # -- pending settle ------------------------------------------------
    def upsert_pending(self, repo: str, pr: int, base: str, head: str, *,
                       now: float) -> dict:
        """Record an eligible observation for settle scheduling.

        A new PR starts an episode (first_seen = last_change = now). A new
        head within an episode restarts the quiet period (last_change)
        without resetting first_seen, so the maximum deferral is measured
        from the first eligible observation. An unchanged head only
        touches the row. Returns the pending row.
        """
        if "" in (repo, base, head) or not pr:
            raise StoreError("refusing to store identity-deficient pending")
        try:
            self.cx.execute(_BEGIN)
            row = self.cx.execute(
                "SELECT base, head, first_seen, last_change, override_by, "
                "override_reason FROM pending_settle WHERE repo=? AND pr=?",
                (repo, pr)).fetchone()
            if row is None:
                self.cx.execute(
                    "INSERT INTO pending_settle(repo, pr, base, head, "
                    "first_seen, last_change, updated_ts) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (repo, pr, base, head, now, now, now))
                first_seen, last_change = now, now
                override_by, override_reason = "", ""
            elif (row[0], row[1]) == (base, head):
                self.cx.execute(
                    "UPDATE pending_settle SET updated_ts=? "
                    "WHERE repo=? AND pr=?", (now, repo, pr))
                first_seen, last_change = row[2], row[3]
                override_by, override_reason = row[4], row[5]
            else:
                self.cx.execute(
                    "UPDATE pending_settle SET base=?, head=?, "
                    "last_change=?, updated_ts=? WHERE repo=? AND pr=?",
                    (base, head, now, now, repo, pr))
                first_seen, last_change = row[2], now
                override_by, override_reason = row[4], row[5]
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"pending upsert failed: {exc}")
        return {"repo": repo, "pr": pr, "base": base, "head": head,
                "first_seen": first_seen, "last_change": last_change,
                "override_by": override_by,
                "override_reason": override_reason}

    def get_pending(self, repo: str, pr: int) -> dict | None:
        row = self.cx.execute(
            "SELECT repo, pr, base, head, first_seen, last_change, "
            "override_by, override_reason FROM pending_settle "
            "WHERE repo=? AND pr=?", (repo, pr)).fetchone()
        if not row:
            return None
        keys = ("repo", "pr", "base", "head", "first_seen", "last_change",
                "override_by", "override_reason")
        return dict(zip(keys, row))

    def list_pending(self) -> list[dict]:
        rows = self.cx.execute(
            "SELECT repo, pr, base, head, first_seen, last_change, "
            "override_by, override_reason FROM pending_settle "
            "ORDER BY first_seen ASC").fetchall()
        keys = ("repo", "pr", "base", "head", "first_seen", "last_change",
                "override_by", "override_reason")
        return [dict(zip(keys, row)) for row in rows]

    def clear_pending(self, repo: str, pr: int) -> None:
        self.cx.execute("DELETE FROM pending_settle WHERE repo=? AND pr=?",
                        (repo, pr))
        self.cx.commit()

    def set_pending_override(self, repo: str, pr: int, *, by: str,
                             reason: str, now: float) -> dict:
        """Flag a pending PR for immediate review. Explicit and logged:
        who requested it and why are stored on the row. No pending row
        means nothing is waiting: refuse rather than invent one."""
        if not by or not reason:
            raise StoreError("override needs who requested it and why")
        try:
            self.cx.execute(_BEGIN)
            row = self.cx.execute(
                "SELECT base, head FROM pending_settle WHERE repo=? AND pr=?",
                (repo, pr)).fetchone()
            if row is None:
                raise StoreError(f"no pending settle for {repo}#{pr}")
            self.cx.execute(
                "UPDATE pending_settle SET override_by=?, override_reason=?, "
                "updated_ts=? WHERE repo=? AND pr=?",
                (by, reason, now, repo, pr))
            self.cx.execute(_COMMIT)
        except StoreError:
            raise
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"pending override failed: {exc}")
        return self.get_pending(repo, pr) or {}

    def consume_pending_override(self, repo: str, pr: int, *,
                                 now: float) -> None:
        """Clear an override once its dispatch has been attempted. The
        pending episode itself continues on retry; terminal outcomes clear
        the whole row via clear_pending."""
        self.cx.execute(
            "UPDATE pending_settle SET override_by='', override_reason='', "
            "updated_ts=? WHERE repo=? AND pr=?", (now, repo, pr))
        self.cx.commit()

    # -- lease ----------------------------------------------------------
    def acquire_lease(self, owner: str, ttl_s: float = 600.0) -> None:
        """Single-instance arbitration. Raises StoreError if live lease held."""
        now = time.time()
        try:
            self.cx.execute(_BEGIN)
            row = self.cx.execute(
                "SELECT owner, heartbeat FROM lease WHERE key='w'").fetchone()
            if row and row[0] != owner and now - float(row[1]) < ttl_s:
                raise StoreError(
                    f"another watcher instance holds the lease ({row[0]}, "
                    f"heartbeat {now - float(row[1]):.0f}s ago); refusing")
            self.cx.execute(
                "INSERT INTO lease(key, owner, heartbeat) VALUES('w', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET owner=excluded.owner, "
                "heartbeat=excluded.heartbeat", (owner, now))
            self.cx.execute(_COMMIT)
        except StoreError:
            raise
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"lease acquisition failed: {exc}")

    def heartbeat_lease(self, owner: str) -> None:
        self.cx.execute(
            "UPDATE lease SET heartbeat=? WHERE key='w' AND owner=?",
            (time.time(), owner))
        self.cx.commit()

    def release_lease(self, owner: str) -> None:
        """Graceful shutdown releases the lease so a restart (or a
        supervised replacement) is admitted immediately. Crashes rely
        on ttl expiry instead."""
        self.cx.execute("DELETE FROM lease WHERE key='w' AND owner=?",
                        (owner,))
        self.cx.commit()

    def close(self) -> None:
        try:
            self.cx.close()
        except Exception:
            pass
