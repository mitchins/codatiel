"""Event-driven incremental review state: SQLite-backed per-env store.

One file per environment (TEST / DEV have separate files, separate
ledgers). WAL mode, explicit transactions, fail-closed StoreError.

Tables:
  meta         schema_version, enabled_at (grandfather watermark),
               pipeline mode at enable time
  deliveries   one row per GitHub delivery ID: replay detection. A repeat
               delivery returns the stored decision and NEVER re-runs
               inference, including across worker restarts.
  checkpoints  one row per (repo, pr): the persistent per-PR review
               checkpoint. Keyed on content (SHAs, diff fingerprints),
               never on updated_at.
  spend        env spend ledger: settled USD, outstanding reservations,
               unknown-spend holds.

Concurrency: claim_for_review() performs check-and-set inside one
BEGIN IMMEDIATE transaction, so two workers racing on one PR serialize:
exactly one wins in_progress, the other queues.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "codatiel_i2.eventstore.v1"

_BEGIN = "BEGIN IMMEDIATE;"
_COMMIT = "COMMIT;"
_ROLLBACK = "ROLLBACK;"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_ts REAL NOT NULL,
    event_type TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    pr INTEGER NOT NULL DEFAULT 0,
    head_sha TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    cost_usd REAL,
    detail TEXT NOT NULL DEFAULT '{}',
    finished_ts REAL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    head_branch TEXT NOT NULL DEFAULT '',
    pr_created_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    baseline_head TEXT NOT NULL DEFAULT '',
    last_reviewed_head TEXT NOT NULL DEFAULT '',
    last_reviewed_base TEXT NOT NULL DEFAULT '',
    reviewed_diff_sha TEXT NOT NULL DEFAULT '',
    reviewed_files INTEGER NOT NULL DEFAULT 0,
    terra_findings INTEGER NOT NULL DEFAULT 0,
    luna_added INTEGER NOT NULL DEFAULT 0,
    last_cost_usd REAL,
    cost_unknown INTEGER NOT NULL DEFAULT 0,
    last_run_dir TEXT NOT NULL DEFAULT '',
    last_result TEXT NOT NULL DEFAULT '',
    queued_head TEXT NOT NULL DEFAULT '',
    queued_base TEXT NOT NULL DEFAULT '',
    in_progress_head TEXT NOT NULL DEFAULT '',
    in_progress_base TEXT NOT NULL DEFAULT '',
    in_progress_run TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    updated_ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, pr)
);
CREATE TABLE IF NOT EXISTS spend (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    base_sha TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    diff_sha TEXT NOT NULL DEFAULT '',
    files INTEGER NOT NULL DEFAULT 0,
    pipeline TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    terra_findings INTEGER NOT NULL DEFAULT 0,
    luna_added INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    cost_unknown INTEGER NOT NULL DEFAULT 0,
    run_dir TEXT NOT NULL DEFAULT '',
    finished_ts REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reviews_repo_pr ON reviews(repo, pr);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    head_sha TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    comment_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL DEFAULT 0,
    UNIQUE(repo, pr, head_sha, run_id)
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
            try:
                self.cx.execute("ALTER TABLE deliveries "
                                "ADD COLUMN finished_ts REAL")
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        except Exception as exc:
            raise StoreError(f"cannot open event store {self.path}: "
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
            raise StoreError(f"unreadable event store {self.path}: "
                             f"{type(exc).__name__}: {exc}")
        for key in ("settled_usd", "reserved_usd", "unknown_holds"):
            if self.spend_get(key) is None:
                self.spend_set(key, 0.0)

    def close(self) -> None:
        try:
            self.cx.close()
        except Exception:
            pass

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

    def require_enabled(self) -> str:
        at = self.meta_get("enabled_at")
        if not at:
            raise StoreError("event pipeline not enabled: run "
                             "`events enable` first (grandfathers open PRs)")
        return at

    # -- deliveries (replay detection) ----------------------------------
    def delivery_get(self, delivery_id: str) -> dict | None:
        try:
            row = self.cx.execute(
                "SELECT delivery_id, received_ts, event_type, action, repo,"
                " pr, head_sha, decision, reason, cost_usd, detail"
                " FROM deliveries WHERE delivery_id=?",
                (delivery_id,)).fetchone()
        except Exception as exc:
            raise StoreError(f"delivery read failed: {exc}")
        if not row:
            return None
        keys = ("delivery_id", "received_ts", "event_type", "action",
                "repo", "pr", "head_sha", "decision", "reason",
                "cost_usd", "detail")
        return dict(zip(keys, row))

    def delivery_record(self, *, delivery_id: str, event_type: str,
                        action: str, repo: str, pr: int, head_sha: str,
                        decision: str, reason: str,
                        cost_usd: float | None,
                        detail: dict | None = None,
                        now: float | None = None) -> dict:
        """Insert-or-keep: the first decision for a delivery ID is final.

        Returns the stored row. A concurrent duplicate insert loses the
        race and the winner's row is returned, so replays always observe
        the original decision.
        """
        ts = time.time() if now is None else now
        blob = json.dumps(detail or {}, sort_keys=True)
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "INSERT INTO deliveries(delivery_id, received_ts,"
                " event_type, action, repo, pr, head_sha, decision,"
                " reason, cost_usd, detail, finished_ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(delivery_id) DO NOTHING",
                (delivery_id, ts, event_type, action, repo, pr, head_sha,
                 decision, reason, cost_usd, blob,
                 ts if decision != "in_progress" else None))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"delivery record failed: {exc}")
        row = self.delivery_get(delivery_id)
        if row is None:
            raise StoreError("delivery record vanished after insert")
        return row

    def delivery_update_decision(self, delivery_id: str, decision: str,
                                 reason: str,
                                 cost_usd: float | None,
                                 now: float | None = None) -> None:
        """Narrow the in-progress placeholder to its final decision.

        Only the original claimant's placeholder may be narrowed: rows
        already holding a terminal decision are never overwritten.
        Stamps finished_ts so spend is attributed to the day it was
        incurred, not the day the delivery arrived.
        """
        ts = time.time() if now is None else now
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "UPDATE deliveries SET decision=?, reason=?, cost_usd=?,"
                " finished_ts=? "
                "WHERE delivery_id=? AND decision='in_progress'",
                (decision, reason, cost_usd, ts, delivery_id))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"delivery update failed: {exc}")

    # -- checkpoints ------------------------------------------------------
    def delivery_mark_replay(self, delivery_id: str,
                               original: dict) -> dict:
        """Record a duplicate delivery without any new work.

        The row keeps the original outcome in its reason; the decision
        becomes skipped:replay so dashboards count blocked replays. A
        repeat replay returns the row unchanged (no reason chaining).
        The original cost is preserved on the row (never zeroed): for
        superseded runs the delivery row is the sole spend record, and
        zeroing it would erase settled money from the daily ledger.
        """
        if original.get("decision") == "skipped:replay":
            row = self.delivery_get(delivery_id)
            if row is None:
                raise StoreError("delivery vanished")
            return row
        try:
            detail = json.loads(original.get("detail") or "{}")
        except Exception:
            detail = {}
        detail["original_decision"] = original.get("decision")
        detail["original_reason"] = original.get("reason")
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "UPDATE deliveries SET decision='skipped:replay',"
                " reason=?, detail=? WHERE delivery_id=?",
                (f"duplicate delivery; original decision "
                 f"{original.get('decision')}: "
                 f"{original.get('reason') or ''}"[:500],
                 json.dumps(detail, sort_keys=True), delivery_id))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"replay mark failed: {exc}")
        row = self.delivery_get(delivery_id)
        if row is None:
            raise StoreError("delivery vanished after replay mark")
        return row

    def delivery_reopen_draft(self, delivery_id: str,
                              now: float | None = None) -> dict:
        """Supersede a deferred skipped:draft row for a readiness rerun.

        Narrow escape from replay protection: only a terminal
        skipped:draft decision (or a skipped:replay wrapper preserving
        it as the original decision) may be reclaimed to in_progress.
        Every other decision is left untouched and the row returned.
        """
        ts = time.time() if now is None else now
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "UPDATE deliveries SET decision='in_progress',"
                " reason='readiness transition re-claimed from draft"
                " deferral', cost_usd=NULL, received_ts=?"
                " WHERE delivery_id=? AND (decision='skipped:draft'"
                " OR decision='skipped:replay')",
                (ts, delivery_id))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"delivery reopen failed: {exc}")
        row = self.delivery_get(delivery_id)
        if row is None:
            raise StoreError("delivery vanished after reopen")
        return row

    def checkpoint_get(self, repo: str, pr: int) -> dict | None:
        try:
            row = self.cx.execute(
                "SELECT repo, pr, head_branch, pr_created_at, status,"
                " baseline_head, last_reviewed_head, last_reviewed_base,"
                " reviewed_diff_sha, reviewed_files, terra_findings,"
                " luna_added, last_cost_usd, cost_unknown, last_run_dir,"
                " last_result, queued_head, queued_base, in_progress_head,"
                " in_progress_base, in_progress_run, note, updated_ts"
                " FROM checkpoints WHERE repo=? AND pr=?",
                (repo, pr)).fetchone()
        except Exception as exc:
            raise StoreError(f"checkpoint read failed: {exc}")
        if not row:
            return None
        keys = ("repo", "pr", "head_branch", "pr_created_at", "status",
                "baseline_head", "last_reviewed_head",
                "last_reviewed_base", "reviewed_diff_sha",
                "reviewed_files", "terra_findings", "luna_added",
                "last_cost_usd", "cost_unknown", "last_run_dir",
                "last_result", "queued_head", "queued_base",
                "in_progress_head", "in_progress_base", "in_progress_run",
                "note", "updated_ts")
        return dict(zip(keys, row))

    def checkpoint_upsert(self, row: dict, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        cols = ("repo", "pr", "head_branch", "pr_created_at", "status",
                "baseline_head", "last_reviewed_head",
                "last_reviewed_base", "reviewed_diff_sha",
                "reviewed_files", "terra_findings", "luna_added",
                "last_cost_usd", "cost_unknown", "last_run_dir",
                "last_result", "queued_head", "queued_base",
                "in_progress_head", "in_progress_base",
                "in_progress_run", "note")
        vals = [row.get(c, "") for c in cols]
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "INSERT INTO checkpoints(" + ",".join(cols) +
                ", updated_ts) VALUES(" + ",".join("?" * len(cols)) +
                ", ?) ON CONFLICT(repo, pr) DO UPDATE SET " +
                ",".join(f"{c}=excluded.{c}" for c in cols if c not in
                          ("repo", "pr")) + ",updated_ts=excluded.updated_ts",
                (*vals, ts))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"checkpoint write failed: {exc}")

    def claim_for_review(self, repo: str, pr: int, *, base: str,
                         head: str, now: float | None = None) -> str:
        """Atomic per-PR claim: 'claimed' | 'queued' | 'already'.

        - no in_progress row -> set in_progress (caller owns the review).
        - in_progress with a different head -> record queued_head, caller
          must NOT run inference.
        - in_progress with the same head -> 'already' (retry of the same
          pinned review; caller must NOT run inference twice).
        """
        ts = time.time() if now is None else now
        try:
            self.cx.execute(_BEGIN)
            cur = self.cx.execute(
                "SELECT status, in_progress_head FROM checkpoints"
                " WHERE repo=? AND pr=?", (repo, pr)).fetchone()
            if cur is None:
                self.cx.execute(
                    "INSERT INTO checkpoints(repo, pr, status,"
                    " in_progress_base, in_progress_head, updated_ts)"
                    " VALUES(?, ?, 'in_progress', ?, ?, ?)",
                    (repo, pr, base, head, ts))
                self.cx.execute(_COMMIT)
                return "claimed"
            status, iph = cur[0], cur[1]
            if status == "in_progress" and iph and iph != head:
                self.cx.execute(
                    "UPDATE checkpoints SET queued_base=?, queued_head=?,"
                    " updated_ts=? WHERE repo=? AND pr=?",
                    (base, head, ts, repo, pr))
                self.cx.execute(_COMMIT)
                return "queued"
            if status == "in_progress":
                self.cx.execute(_COMMIT)
                return "already"
            self.cx.execute(
                "UPDATE checkpoints SET status='in_progress',"
                " in_progress_base=?, in_progress_head=?,"
                " in_progress_run='', updated_ts=? WHERE repo=? AND pr=?",
                (base, head, ts, repo, pr))
            self.cx.execute(_COMMIT)
            return "claimed"
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"claim failed: {exc}")

    def complete_review(self, repo: str, pr: int, *, base: str, head: str,
                        diff_sha: str, files: int, terra_findings: int,
                        luna_added: int, cost_usd: float | None,
                        cost_unknown: bool, run_dir: str, result: str,
                        now: float | None = None) -> dict:
        """Seal a finished review; drain any queued newer head.

        Returns {'queued': (base, head) | None}: a queued delta the
        caller should review next (separate run, separate evidence).
        """
        ts = time.time() if now is None else now
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "UPDATE checkpoints SET status='reviewed',"
                " last_reviewed_base=?, last_reviewed_head=?,"
                " reviewed_diff_sha=?, reviewed_files=?,"
                " terra_findings=?, luna_added=?, last_cost_usd=?,"
                " cost_unknown=?, last_run_dir=?, last_result=?,"
                " in_progress_base='', in_progress_head='',"
                " in_progress_run='', updated_ts=? "
                "WHERE repo=? AND pr=?",
                (base, head, diff_sha, files, terra_findings, luna_added,
                 cost_usd, 1 if cost_unknown else 0, run_dir, result, ts,
                 repo, pr))
            row = self.cx.execute(
                "SELECT queued_base, queued_head FROM checkpoints"
                " WHERE repo=? AND pr=?", (repo, pr)).fetchone()
            qb, qh = (row or ("", ""))
            if qh and qh != head:
                self.cx.execute(
                    "UPDATE checkpoints SET queued_base='', queued_head='',"
                    " updated_ts=? WHERE repo=? AND pr=?", (ts, repo, pr))
                self.cx.execute(_COMMIT)
                return {"queued": (qb, qh)}
            if qh and qh == head:
                # Queue entry already satisfied by this review (PROD-02:
                # a stale queue row equalling the reviewed head must not
                # survive to be drained backwards later). Drop it.
                self.cx.execute(
                    "UPDATE checkpoints SET queued_base='', queued_head='',"
                    " updated_ts=? WHERE repo=? AND pr=?", (ts, repo, pr))
                self.cx.execute(_COMMIT)
                return {"queued": None}
            self.cx.execute(_COMMIT)
            return {"queued": None}
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"complete failed: {exc}")

    def mark_interrupted(self, repo: str, pr: int,
                         note: str) -> None:
        """Stale in_progress (crash/restart): needs-attention, no retry.

        A restarted worker must never silently re-spend on an interrupted
        review; an operator explicitly re-drives it.
        """
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "UPDATE checkpoints SET status='interrupted', note=?,"
                " in_progress_base='', in_progress_head='',"
                " in_progress_run='', updated_ts=? "
                "WHERE repo=? AND pr=? AND status='in_progress'",
                (note, time.time(), repo, pr))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"interrupt mark failed: {exc}")

    def release_claim(self, repo: str, pr: int, note: str, *,
                      base: str, head: str,
                      prior_status: str | None = None) -> bool:
        """Release exactly the held claim matching (base, head).

        Atomic ownership check: the UPDATE only clears a checkpoint
        whose pinned in_progress range is exactly the caller's. An
        expired or replaced worker — including one holding the same
        head from an older base — cannot release a newer claim.
        Returns True when this caller's claim was released, False when
        nothing was held or the pinned claim belongs to someone else.
        """
        try:
            self.cx.execute(_BEGIN)
            row = self.cx.execute(
                "SELECT status, last_reviewed_head, in_progress_base,"
                " in_progress_head FROM checkpoints"
                " WHERE repo=? AND pr=?", (repo, pr)).fetchone()
            if row is None or row[0] != "in_progress":
                self.cx.execute(_COMMIT)
                return False
            if row[2] != base or row[3] != head:
                # A different (newer or older) run owns the pin.
                self.cx.execute(_COMMIT)
                return False
            if prior_status in ("reviewed", "draft", "deferred",
                                "grandfathered", "failed", "interrupted"):
                status = prior_status
            else:
                status = ("reviewed" if row[1] else "deferred")
            changed = self.cx.execute(
                "UPDATE checkpoints SET status=?, note=?,"
                " in_progress_base='', in_progress_head='',"
                " in_progress_run='', updated_ts=? "
                "WHERE repo=? AND pr=? AND status='in_progress' "
                "AND in_progress_base=? AND in_progress_head=?",
                (status, note, time.time(), repo, pr, base,
                 head)).rowcount
            self.cx.execute(_COMMIT)
            return bool(changed)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"claim release failed: {exc}")

    def clear_queued(self, repo: str, pr: int, *,
                     base: str, head: str) -> bool:
        """Drop a queued delta without reviewing it (PROD-02: the queued
        head proved stale against the live GitHub head).

        Atomic identity compare: only the exact captured (base, head)
        is cleared. A newer queued head arriving during stale-head
        verification survives. Returns True when the captured entry
        was cleared.
        """
        try:
            self.cx.execute(_BEGIN)
            changed = self.cx.execute(
                "UPDATE checkpoints SET queued_base='', queued_head='',"
                " updated_ts=? WHERE repo=? AND pr=?"
                " AND queued_base=? AND queued_head=?",
                (time.time(), repo, pr, base, head)).rowcount
            self.cx.execute(_COMMIT)
            return bool(changed)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"queued clear failed: {exc}")

    def list_checkpoints(self) -> list[dict]:
        try:
            rows = self.cx.execute(
                "SELECT repo, pr, head_branch, pr_created_at, status,"
                " baseline_head, last_reviewed_head, last_reviewed_base,"
                " reviewed_diff_sha, reviewed_files, terra_findings,"
                " luna_added, last_cost_usd, cost_unknown, last_run_dir,"
                " last_result, queued_head, queued_base, in_progress_head,"
                " in_progress_base, in_progress_run, note, updated_ts"
                " FROM checkpoints ORDER BY repo, pr").fetchall()
        except Exception as exc:
            raise StoreError(f"checkpoint list failed: {exc}")
        keys = ("repo", "pr", "head_branch", "pr_created_at", "status",
                "baseline_head", "last_reviewed_head",
                "last_reviewed_base", "reviewed_diff_sha",
                "reviewed_files", "terra_findings", "luna_added",
                "last_cost_usd", "cost_unknown", "last_run_dir",
                "last_result", "queued_head", "queued_base",
                "in_progress_head", "in_progress_base", "in_progress_run",
                "note", "updated_ts")
        return [dict(zip(keys, r)) for r in rows]

    # -- reviews ----------------------------------------------------------
    def review_record(self, *, repo: str, pr: int, kind: str,
                      base_sha: str, head_sha: str, diff_sha: str,
                      files: int, pipeline: str, status: str,
                      terra_findings: int, luna_added: int,
                      cost_usd: float | None, cost_unknown: bool,
                      run_dir: str,
                      now: float | None = None) -> int:
        import time as _t
        ts = _t.time() if now is None else now
        try:
            self.cx.execute(_BEGIN)
            cur = self.cx.execute(
                "INSERT INTO reviews(repo, pr, kind, base_sha, head_sha,"
                " diff_sha, files, pipeline, status, terra_findings,"
                " luna_added, cost_usd, cost_unknown, run_dir,"
                " finished_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (repo, pr, kind, base_sha, head_sha, diff_sha, files,
                 pipeline, status, terra_findings, luna_added, cost_usd,
                 1 if cost_unknown else 0, run_dir, ts))
            rowid = cur.lastrowid
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"review record failed: {exc}")
        return int(rowid or 0)

    def reviews_list(self) -> list[dict]:
        try:
            rows = self.cx.execute(
                "SELECT repo, pr, kind, base_sha, head_sha, diff_sha,"
                " files, pipeline, status, terra_findings, luna_added,"
                " cost_usd, cost_unknown, run_dir, finished_ts"
                " FROM reviews ORDER BY id").fetchall()
        except Exception as exc:
            raise StoreError(f"review list failed: {exc}")
        keys = ("repo", "pr", "kind", "base_sha", "head_sha", "diff_sha",
                "files", "pipeline", "status", "terra_findings",
                "luna_added", "cost_usd", "cost_unknown", "run_dir",
                "finished_ts")
        return [dict(zip(keys, r)) for r in rows]

    # -- spend ------------------------------------------------------------
    def spend_get(self, key: str) -> float | None:
        try:
            row = self.cx.execute(
                "SELECT value FROM spend WHERE key=?", (key,)).fetchone()
        except Exception as exc:
            raise StoreError(f"spend read failed: {exc}")
        return row[0] if row else None

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

    def spend_totals(self) -> dict[str, float]:
        return {
            "settled_usd": self.spend_get("settled_usd") or 0.0,
            "reserved_usd": self.spend_get("reserved_usd") or 0.0,
            "unknown_holds": self.spend_get("unknown_holds") or 0.0,
        }

    def reserve(self, amount: float) -> tuple[bool, dict[str, float]]:
        """Hold a per-review reservation; False when it would breach."""
        try:
            self.cx.execute(_BEGIN)
            s = (self.cx.execute(
                "SELECT value FROM spend WHERE key='settled_usd'"
            ).fetchone() or (0.0,))[0]
            r = (self.cx.execute(
                "SELECT value FROM spend WHERE key='reserved_usd'"
            ).fetchone() or (0.0,))[0]
            self.cx.execute(
                "INSERT INTO spend(key, value) VALUES('reserved_usd', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (r + amount,))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"reserve failed: {exc}")
        return True, {"settled_usd": s, "reserved_usd": r + amount}

    def settle(self, reserved: float, actual: float | None) -> dict:
        """Release a reservation; settle actuals (None = unknown: hold)."""
        try:
            self.cx.execute(_BEGIN)
            s = (self.cx.execute(
                "SELECT value FROM spend WHERE key='settled_usd'"
            ).fetchone() or (0.0,))[0]
            r = (self.cx.execute(
                "SELECT value FROM spend WHERE key='reserved_usd'"
            ).fetchone() or (0.0,))[0]
            u = (self.cx.execute(
                "SELECT value FROM spend WHERE key='unknown_holds'"
            ).fetchone() or (0.0,))[0]
            r = max(0.0, r - reserved)
            if actual is None:
                # Unknown true cost: keep the reservation value held as
                # spend so unknown usage keeps blocking the ceiling.
                s += reserved
                u += 1.0
            else:
                s += actual
            self.cx.execute(
                "INSERT INTO spend(key, value) VALUES('settled_usd', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (s,))
            self.cx.execute(
                "INSERT INTO spend(key, value) VALUES('reserved_usd', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (r,))
            self.cx.execute(
                "INSERT INTO spend(key, value) VALUES('unknown_holds', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (u,))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"settle failed: {exc}")
        return {"settled_usd": s, "reserved_usd": r,
                "unknown_holds": u}

    # -- publications (GitHub comment record; PROD-01 Track A) ---------------
    def publication_get(self, repo: str, pr: int, head_sha: str,
                        run_id: str) -> dict | None:
        try:
            row = self.cx.execute(
                "SELECT repo, pr, head_sha, run_id, comment_id, outcome,"
                " reason, created_ts FROM publications"
                " WHERE repo=? AND pr=? AND head_sha=? AND run_id=?",
                (repo, pr, head_sha, run_id)).fetchone()
        except Exception as exc:
            raise StoreError(f"publication read failed: {exc}")
        if not row:
            return None
        keys = ("repo", "pr", "head_sha", "run_id", "comment_id",
                "outcome", "reason", "created_ts")
        return dict(zip(keys, row))

    def publication_record(self, *, repo: str, pr: int, head_sha: str,
                           run_id: str, comment_id: str, outcome: str,
                           reason: str,
                           now: float | None = None) -> dict:
        """Insert-or-keep one publication row per (repo, PR, head, run).

        The first outcome for a review identity stands; a concurrent
        duplicate loses the race and the winner's row is returned, so
        retries observe the original post instead of double-posting.
        """
        ts = time.time() if now is None else now
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "INSERT INTO publications(repo, pr, head_sha, run_id,"
                " comment_id, outcome, reason, created_ts)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(repo, pr, head_sha, run_id) DO NOTHING",
                (repo, pr, head_sha, run_id, comment_id, outcome,
                 reason, ts))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"publication record failed: {exc}")
        row = self.publication_get(repo, pr, head_sha, run_id)
        if row is None:
            raise StoreError("publication record vanished after insert")
        return row

    def publication_update(self, repo: str, pr: int, head_sha: str,
                           run_id: str, comment_id: str, outcome: str,
                           reason: str,
                           now: float | None = None) -> None:
        """Overwrite a publication row (repost after delete, refusal
        re-evaluation). Posted rows are never downgraded by callers —
        post_review_comment reconciles first and only upgrades.
        """
        ts = time.time() if now is None else now
        try:
            self.cx.execute(_BEGIN)
            self.cx.execute(
                "UPDATE publications SET comment_id=?, outcome=?,"
                " reason=?, created_ts=? WHERE repo=? AND pr=?"
                " AND head_sha=? AND run_id=?",
                (comment_id, outcome, reason, ts, repo, pr, head_sha,
                 run_id))
            self.cx.execute(_COMMIT)
        except Exception as exc:
            try:
                self.cx.execute(_ROLLBACK)
            except Exception:
                pass
            raise StoreError(f"publication update failed: {exc}")

    def publications_list(self, repo: str | None = None,
                          pr: int | None = None) -> list[dict]:
        try:
            if repo is not None and pr is not None:
                rows = self.cx.execute(
                    "SELECT repo, pr, head_sha, run_id, comment_id,"
                    " outcome, reason, created_ts FROM publications"
                    " WHERE repo=? AND pr=? ORDER BY created_ts",
                    (repo, pr)).fetchall()
            else:
                rows = self.cx.execute(
                    "SELECT repo, pr, head_sha, run_id, comment_id,"
                    " outcome, reason, created_ts FROM publications"
                    " ORDER BY created_ts").fetchall()
        except Exception as exc:
            raise StoreError(f"publication list failed: {exc}")
        keys = ("repo", "pr", "head_sha", "run_id", "comment_id",
                "outcome", "reason", "created_ts")
        return [dict(zip(keys, r)) for r in rows]

    def deliveries_by_decision(self) -> dict[str, int]:
        try:
            rows = self.cx.execute(
                "SELECT decision, COUNT(*) FROM deliveries GROUP BY decision"
            ).fetchall()
        except Exception as exc:
            raise StoreError(f"delivery stats failed: {exc}")
        return {d: n for d, n in rows}
