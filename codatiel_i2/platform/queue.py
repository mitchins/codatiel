"""Application command service: inbox, targets, leased work, money, effects.

Adapted from the validated isolated reference model (no Codatiel code
imported there; this is the production implementation of the same
state transitions, extended with dirty revisions, target generations,
fenced reconciliation, lifecycle versions, frozen review specs,
versioned settings policy, and on-disk artifact references).

Money is integer micro-USD throughout (see money.py). Every mutating
command runs in one short BEGIN IMMEDIATE transaction with no network
I/O inside. Callers pass `now` explicitly; production passes host time
consistently, tests pass scripted time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from codatiel_i2.platform import money as money_mod
from codatiel_i2.platform.db import tx

ELIGIBLE_LIFECYCLES = ("open",)
JOB_STATES = ("pending", "leased", "running", "retry_wait",
              "deferred_budget", "awaiting_verification", "succeeded",
              "superseded", "cancelled", "failed", "needs_attention")


class Conflict(Exception):
    """Logical conflict: duplicate identity misuse, double settlement."""


class LeaseLost(Exception):
    """The claim no longer owns the job (expiry, fence, takeover)."""


@dataclass(frozen=True)
class Claim:
    job: int
    attempt: int
    owner: str
    fence: int


@dataclass
class Policy:
    """Resolved operator controls enforced at admission/dispatch gates.

    Must be built via settings.to_policy, never constructed ad hoc:
    the None-vs-empty allowlist distinction (unconfigured vs deny-all)
    is load-bearing. allowed_repos None = no constraint configured;
    () = explicitly nothing allowlisted (deny all, fail closed).
    """
    pause_reviews: bool = False
    pause_publication: bool = False
    email_allowed: bool = True
    allowed_repos: tuple[str, ...] | None = None
    lease_s: float = 60.0


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def event(db: sqlite3.Connection, kind: str, ref: str = "",
          now: float | None = None, **detail) -> None:
    db.execute(
        "INSERT INTO pl_events(kind,ref,detail,ts) VALUES(?,?,?,?)",
        (kind, str(ref), json.dumps(detail, sort_keys=True), _now(now)))


def _row(db: sqlite3.Connection, query: str,
         args: tuple = ()) -> sqlite3.Row | None:
    return db.execute(query, args).fetchone()


def set_budget(db: sqlite3.Connection, name: str, cap_usd: float,
               now: float | None = None) -> None:
    cap = money_mod.to_micro(cap_usd)
    with tx(db):
        db.execute("INSERT INTO pl_budgets(name,cap_micro) VALUES(?,?) "
                   "ON CONFLICT(name) DO UPDATE SET cap_micro=excluded.cap_micro",
                   (name, cap))
        event(db, "budget", name, now=now, cap_micro=cap)


def exposure(db: sqlite3.Connection, scope: str) -> int:
    """Committed headroom consumption in micro-USD for one scope.

    Settled attempts count actuals; reserved attempts count the
    reservation; unknown-charge holds keep their full reservation until
    reconciled (they keep blocking headroom across boundaries).
    """
    row = _row(db,
               "SELECT COALESCE(SUM(CASE WHEN a.state='settled' "
               "THEN a.actual_micro ELSE a.reserve_micro END),0) "
               "FROM pl_attempts a JOIN pl_scopes s ON s.attempt=a.id "
               "WHERE s.scope=?", (scope,))
    opening = _row(db, "SELECT micro FROM pl_openings WHERE scope=?",
                   (scope,))
    return int(row[0]) + (int(opening[0]) if opening else 0)


# -- inbox ---------------------------------------------------------------

def accept(db: sqlite3.Connection, *, app: str, guid: str, body: bytes,
           repo: str, repo_id: int | None, pr: int, head: str,
           lifecycle: str = "", now: float | None = None) -> str:
    """Durably accept one delivery. Returns 'accepted' or 'duplicate'.

    Same GUID + same content returns the original receipt without new
    work (a duplicate attempt row is still recorded). Same GUID +
    different content is quarantined and raises Conflict: it is never
    silently overwritten, and the body is never stored.
    """
    ts = _now(now)
    digest = hashlib.sha256(body).hexdigest()
    conflict = False
    with tx(db):
        old = _row(db, "SELECT * FROM pl_inbox WHERE app=? AND guid=?",
                   (app, guid))
        if old is not None:
            same = (old["digest"], old["repo"], old["pr"],
                    old["head"]) == (digest, repo, pr, head)
            db.execute("INSERT INTO pl_receipt_attempts(app,guid,outcome,"
                       "detail,ts) VALUES(?,?,?,?,?)",
                       (app, guid, "duplicate" if same else "conflict",
                        json.dumps({"digest": digest}, sort_keys=True), ts))
            event(db, "receipt", guid, now=ts,
                  outcome="duplicate" if same else "conflict")
            # A conflict commits its quarantine record first; the raise
            # below happens after commit so the evidence survives.
            conflict = not same
        else:
            db.execute("INSERT INTO pl_inbox(app,guid,digest,repo,repo_id,"
                       "pr,head,lifecycle,state,received_ts) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (app, guid, digest, repo, repo_id, pr, head,
                        lifecycle, "pending", ts))
            db.execute("INSERT INTO pl_receipt_attempts(app,guid,outcome,"
                       "detail,ts) VALUES(?,?,?,?,?)",
                       (app, guid, "accepted",
                        json.dumps({"digest": digest}, sort_keys=True), ts))
            event(db, "receipt", guid, now=ts, outcome="accepted")
    if conflict:
        raise Conflict("delivery GUID reused for different content; "
                       "quarantined, original receipt preserved")
    return "accepted" if old is None else "duplicate"


# -- signals, dirty revisions, target generations --------------------------

def _target(db: sqlite3.Connection, repo: str,
            pr: int) -> sqlite3.Row | None:
    return _row(db, "SELECT * FROM pl_pr_targets WHERE repo=? AND pr=?",
                (repo, pr))


def _job_by_id(db: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return _row(db, "SELECT * FROM pl_jobs WHERE id=?", (job_id,))


def _attempt_by_id(db: sqlite3.Connection, attempt_id: int) -> sqlite3.Row | None:
    return _row(db, "SELECT * FROM pl_attempts WHERE id=?", (attempt_id,))



# Lifecycle vocabulary is closed: ingress maps webhook actions to
# exactly these values, and unknown actions never overwrite a known
# lifecycle (Slice 2 enforces the mapping; the contract refuses
# anything else here).
LIFECYCLES = ("open", "draft", "closed", "merged")


def signal(db: sqlite3.Connection, *, repo: str, pr: int,
           repo_id: int | None = None, app: str = "",
           head: str = "", base: str = "", lifecycle: str = "open",
           base_retarget: bool = False,
           now: float | None = None) -> dict:
    """Record one wake-up for a PR. Always bumps the dirty revision.

    The target generation bumps only on a policy-relevant transition
    the verified commit cannot detect as a head change: readiness or
    resumption (ineligible -> eligible lifecycle, even with an
    unchanged head), or a genuine base retarget. New wake-up heads are
    recorded (last_signal_head) and reported as scheduled, but the
    generation advances only when the fenced reconcile_commit verifies
    the observation — duplicate/out-of-order webhooks never churn
    generations. Any transition to an ineligible lifecycle (draft /
    closed / merged) bumps the lifecycle version and cancels pending
    automatic work AND effects instead of scheduling: draft-removal of
    readiness durably invalidates unsent effects like close/merge do.
    A first signal for an ineligible lifecycle is recorded but not
    scheduled.
    """
    if lifecycle not in LIFECYCLES:
        raise ValueError(f"unknown lifecycle {lifecycle!r}; ingress must "
                         f"map to one of {LIFECYCLES}")
    ts = _now(now)
    with tx(db):
        tgt = _target(db, repo, pr)
        if tgt is None:
            eligible = lifecycle in ELIGIBLE_LIFECYCLES
            db.execute(
                "INSERT INTO pl_pr_targets(repo,pr,repo_id,app,lifecycle,"
                "lifecycle_version,target_generation,dirty_rev,"
                "last_signal_head,last_signal_base,updated_ts) "
                "VALUES(?,?,?,?,?,1,1,1,?,?,?)",
                (repo, pr, repo_id, app, lifecycle, head, base, ts))
            event(db, "signal", f"{repo}#{pr}", now=ts,
                  generation=1, dirty=1, lifecycle=lifecycle)
            return {"generation": 1, "dirty": 1, "scheduled": eligible}
        if (repo_id is not None and tgt["repo_id"] is not None
                and int(tgt["repo_id"]) != int(repo_id)):
            raise Conflict(
                f"repo {repo}#{pr} already bound to id {tgt['repo_id']}; "
                f"incoming id {repo_id} suggests a rename/reuse — "
                f"refusing to merge histories")
        if (app and tgt["app"] and tgt["app"] != app):
            raise Conflict(
                f"repo {repo}#{pr} already bound to app {tgt['app']!r}; "
                f"incoming app {app!r} suggests a second App — refusing")
        dirty = int(tgt["dirty_rev"]) + 1
        generation = int(tgt["target_generation"])
        prev_lifecycle = tgt["lifecycle"]
        scheduled = False
        cancel_effects = False
        cancel_jobs: list[int] = []
        signal_head = tgt["last_signal_head"]
        signal_base = tgt["last_signal_base"]
        if lifecycle not in ELIGIBLE_LIFECYCLES:
            lifecycle_version = int(tgt["lifecycle_version"]) + (
                0 if prev_lifecycle == lifecycle else 1)
            cancel_effects = True
            cancel_jobs = [r[0] for r in db.execute(
                "SELECT id FROM pl_jobs WHERE repo=? AND pr=? AND state IN "
                "('pending','retry_wait','deferred_budget',"
                "'awaiting_verification')", (repo, pr)).fetchall()]
        else:
            lifecycle_version = int(tgt["lifecycle_version"]) + (
                0 if prev_lifecycle == lifecycle else 1)
            resumed = prev_lifecycle not in ELIGIBLE_LIFECYCLES
            head_changed = bool(head) and head != signal_head
            # Generation bumps only on transitions the verified commit
            # cannot detect as a head change: resumption/readiness into
            # an eligible lifecycle (even with an unchanged head) and
            # genuine base retargets. New wake-up heads do NOT bump
            # here; the fenced reconcile_commit bumps once the
            # observation is verified, so duplicate/out-of-order
            # webhooks never churn generations or schedule empty work.
            if ((resumed and lifecycle in ELIGIBLE_LIFECYCLES)
                    or (base_retarget and lifecycle in ELIGIBLE_LIFECYCLES)):
                generation += 1
                scheduled = True
            elif head_changed and lifecycle in ELIGIBLE_LIFECYCLES:
                scheduled = True
            if head:
                signal_head = head
            if base:
                signal_base = base
        db.execute(
            "UPDATE pl_pr_targets SET repo_id=COALESCE(?,repo_id),"
            "app=CASE WHEN ?='' THEN app ELSE ? END,"
            "lifecycle=?,lifecycle_version=?,target_generation=?,"
            "dirty_rev=?,last_signal_head=?,last_signal_base=?,"
            "updated_ts=? "
            "WHERE repo=? AND pr=?",
            (repo_id, app, app, lifecycle, lifecycle_version, generation,
             dirty, signal_head, signal_base, ts, repo, pr))
        if cancel_effects:
            db.execute(
                "UPDATE pl_effects SET state='cancelled' "
                "WHERE job IN (SELECT id FROM pl_jobs WHERE repo=? AND pr=?) "
                "AND state IN ('pending','leased')", (repo, pr))
        if cancel_jobs:
            db.execute(
                f"UPDATE pl_jobs SET state='cancelled' WHERE id IN "
                f"({','.join('?' * len(cancel_jobs))})", cancel_jobs)
            # Cancelled jobs hold no leases by construction (only
            # unclaimed states are selected), but never strand a
            # reservation if that ever changes: settle reserved
            # attempts at zero.
            for jid in cancel_jobs:
                abandoned = _row(db, "SELECT id FROM pl_attempts "
                                     "WHERE job=? AND state='reserved'",
                                 (jid,))
                if abandoned is not None:
                    _settle(db, abandoned["id"], 0)
        event(db, "signal", f"{repo}#{pr}", now=ts, generation=generation,
              dirty=dirty, lifecycle=lifecycle,
              cancelled_effects=bool(cancel_effects),
              cancelled_jobs=len(cancel_jobs))
    return {"generation": generation, "dirty": dirty,
            "scheduled": scheduled}


# -- fenced reconciliation --------------------------------------------------

def reconcile_claim(db: sqlite3.Connection, *, repo: str, pr: int,
                    owner: str, ttl_s: float = 30.0,
                    now: float | None = None) -> dict | None:
    """Take the short-lived per-PR refresh claim. One owner at a time.

    Returns the claim (fence, captured generation, captured dirty) or
    None when another live owner holds it. Re-entrant for the same
    owner: a failed commit releases or re-claims without waiting out
    the TTL, so the reconcile loop retries instead of stalling.
    """
    ts = _now(now)
    with tx(db):
        tgt = _target(db, repo, pr)
        if tgt is None:
            return None
        if tgt["recon_owner"] and float(tgt["recon_expires"]) > ts:
            if tgt["recon_owner"] != owner:
                return None
            db.execute("UPDATE pl_pr_targets SET recon_expires=? "
                       "WHERE repo=? AND pr=?", (ts + ttl_s, repo, pr))
            return {"owner": owner, "fence": int(tgt["recon_fence"]),
                    "generation": int(tgt["target_generation"]),
                    "dirty": int(tgt["dirty_rev"])}
        fence = int(tgt["recon_fence"]) + 1
        db.execute("UPDATE pl_pr_targets SET recon_owner=?,recon_fence=?,"
                   "recon_expires=? WHERE repo=? AND pr=?",
                   (owner, fence, ts + ttl_s, repo, pr))
        event(db, "reconcile_claim", f"{repo}#{pr}", now=ts,
              owner=owner, fence=fence)
        return {"owner": owner, "fence": fence,
                "generation": int(tgt["target_generation"]),
                "dirty": int(tgt["dirty_rev"])}


def reconcile_commit(db: sqlite3.Connection, *, repo: str, pr: int,
                     owner: str, fence: int, generation: int, dirty: int,
                     head: str, base: str = "",
                     now: float | None = None) -> bool:
    """Commit an out-of-transaction live observation, or discard it.

    Commits only when the claim still owns the fence AND the captured
    generation/dirty still match: a late owner's response never
    overwrites a newer observation, and two simultaneous reads of the
    same revision cannot commit in reverse order. An empty observation
    carries no information and never clears verified state.

    A verified head OR base change is an authoritative policy-relevant
    transition (e.g. the slow sweep observing a head/base no webhook
    reported): the generation bumps so the verified observation
    becomes routable work. The verified base is recorded alongside
    the head so base retargets are verifiable, not caller-asserted;
    an omitted base never erases a verified one. A confirming
    observation (no change) leaves the announcement witness alone, so
    a newer announced-but-unverified head is never burned by an older
    read arriving late. No TTL on live state by design: live_observed
    records provenance for the console; refresh cadence is the
    reconciler's policy (Slice 2).

    The worker re-runs reroute() after every commit that changes live
    state, so receipts that arrived before verification get their job
    without relying on a redelivery GitHub will never send.
    """
    if not head:
        return False
    ts = _now(now)
    with tx(db):
        tgt = _target(db, repo, pr)
        if tgt is None:
            return False
        if not (tgt["recon_owner"] == owner
                and int(tgt["recon_fence"]) == fence
                and int(tgt["target_generation"]) == generation
                and int(tgt["dirty_rev"]) == dirty):
            return False
        head_changed = head != tgt["live_head"]
        base_changed = bool(base) and base != tgt["live_base"]
        gen = generation + (1 if (head_changed or base_changed) else 0)
        db.execute("UPDATE pl_pr_targets SET live_head=?,live_observed=?,"
                   "live_base=?,"
                   "target_generation=?,last_signal_head=?,"
                   "last_signal_base=?,"
                   "recon_owner='',recon_expires=0,updated_ts=? "
                   "WHERE repo=? AND pr=?",
                   (head, ts, base or tgt["live_base"], gen,
                    head if head_changed else tgt["last_signal_head"],
                    base if base_changed else tgt["last_signal_base"],
                    ts, repo, pr))
        event(db, "reconcile_commit", f"{repo}#{pr}", now=ts, head=head,
              base=base or tgt["live_base"], generation=gen)
    return True


def reconcile_release(db: sqlite3.Connection, *, repo: str, pr: int,
                      owner: str, fence: int,
                      now: float | None = None) -> bool:
    """Release a reconcile claim after a discarded commit.

    A failed commit (dirty moved mid-read) must not lock the PR out
    until TTL: the owner releases, then claims again for a fresh
    observation. Only the fence holder may release.
    """
    ts = _now(now)
    with tx(db):
        tgt = _target(db, repo, pr)
        if tgt is None:
            return False
        if not (tgt["recon_owner"] == owner
                and int(tgt["recon_fence"]) == fence):
            return False
        db.execute("UPDATE pl_pr_targets SET recon_owner='',"
                   "recon_expires=0,updated_ts=? WHERE repo=? AND pr=?",
                   (ts, repo, pr))
        event(db, "reconcile_release", f"{repo}#{pr}", now=ts,
              owner=owner)
    return True


# -- routing: wake-ups become at most one logical job -----------------------

def route(db: sqlite3.Connection, *, app: str, guid: str,
          rerun_generation: int = 0,
          now: float | None = None) -> str:
    """Route one accepted receipt to the current verified target.

    Seam contract (Slice 2 ingress must follow it): routing is
    meaningful only after a reconcile commit covering the receipt.
    A receipt for an announced-but-unverified head waits as
    'pending'; the worker re-routes pending receipts after every
    commit that changes live state (see reroute()).

    Returns 'routed', 'pending' (no verifiable live observation yet,
    head announced but not yet verified, or draft target awaiting
    readiness — never infer, never burn), 'stale'
    (two witnesses agree the head is superseded: verified live AND
    the last announcement both differ), 'suppressed_lifecycle'
    (target not eligible; never schedules),
    'already_reviewed' (live head AND base both match the accepted
    review, and no explicit rerun — never an empty head-to-same-head
    job), 'needs_redrive' (a terminal job already holds this intent;
    re-drive explicitly with a higher rerun_generation, never
    automatically), or the existing inbox state.

    Concurrent receipts coalesce: the UNIQUE intent means duplicates
    create no second job, and the inbox is marked 'routed' only when
    the intent holds live work. A real reset (live moves, generation
    bumped by the verified commit) routes as a new job. A base
    retarget at an unchanged head forces mode='full': a re-root
    against the newly verified base, never an empty incremental diff.
    """
    ts = _now(now)
    with tx(db):
        row = _row(db, "SELECT * FROM pl_inbox WHERE app=? AND guid=?",
                   (app, guid))
        if row is None:
            raise KeyError(guid)
        if row["state"] != "pending" and rerun_generation == 0:
            return row["state"]
        tgt = _target(db, row["repo"], row["pr"])
        if tgt is None or not tgt["live_head"]:
            return "pending"
        if tgt["lifecycle"] not in ELIGIBLE_LIFECYCLES:
            if tgt["lifecycle"] == "draft":
                # Readiness may still come: wait, don't burn the
                # receipt and never schedule.
                return "pending"
            db.execute("UPDATE pl_inbox SET state='suppressed_lifecycle' "
                       "WHERE app=? AND guid=?", (app, guid))
            event(db, "route", guid, now=ts, state="suppressed_lifecycle")
            return "suppressed_lifecycle"
        if row["head"] != tgt["live_head"]:
            if row["head"] == tgt["last_signal_head"]:
                # Announced but not yet verified: wait, don't burn the
                # receipt. The worker re-routes after the next commit.
                return "pending"
            db.execute("UPDATE pl_inbox SET state='stale' "
                       "WHERE app=? AND guid=?", (app, guid))
            event(db, "route", guid, now=ts, state="stale")
            return "stale"
        base_known = bool(tgt["live_base"])
        base_changed = base_known and bool(tgt["reviewed_base"]) and (
            tgt["live_base"] != tgt["reviewed_base"])
        if (rerun_generation == 0
                and tgt["last_signal_base"]
                and tgt["last_signal_base"] != tgt["live_base"]):
            # A base retarget was announced but the new base is not
            # yet verified: wait for the reconciler, don't burn the
            # receipt as already-reviewed and don't schedule a re-root
            # against the old base.
            return "pending"
        if (rerun_generation == 0 and tgt["reviewed_head"]
                and tgt["reviewed_head"] == tgt["live_head"]
                and not base_changed):
            db.execute("UPDATE pl_inbox SET state='already_reviewed' "
                       "WHERE app=? AND guid=?", (app, guid))
            event(db, "route", guid, now=ts, state="already_reviewed")
            return "already_reviewed"
        if not tgt["reviewed_head"] or base_changed:
            mode = "full"
        else:
            mode = "incremental"
        cur = db.execute(
            "INSERT INTO pl_jobs(repo,pr,target_generation,rerun_generation,"
            "mode,full_base,comparison_base,full_head,dirty_seen,"
            "created_ts) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo,pr,target_generation,rerun_generation) "
            "DO NOTHING",
            (row["repo"], row["pr"], int(tgt["target_generation"]),
             rerun_generation, mode,
             tgt["live_base"] or tgt["reviewed_base"],
             tgt["reviewed_head"], tgt["live_head"],
             int(tgt["dirty_rev"]), ts))
        if int(cur.rowcount or 0):
            db.execute("UPDATE pl_inbox SET state='routed' "
                       "WHERE app=? AND guid=?", (app, guid))
            event(db, "route", guid, now=ts, state="routed",
                  generation=int(tgt["target_generation"]))
            return "routed"
        existing = _row(db, "SELECT state FROM pl_jobs WHERE repo=? AND pr=?"
                            " AND target_generation=? AND rerun_generation=?",
                        (row["repo"], row["pr"],
                         int(tgt["target_generation"]), rerun_generation))
        state = (existing["state"] if existing else "pending")
        if state == "succeeded":
            outcome = "already_reviewed"
        elif state in ("superseded", "cancelled", "failed",
                       "needs_attention"):
            outcome = "needs_redrive"
        else:
            outcome = "routed"
        db.execute("UPDATE pl_inbox SET state=? WHERE app=? AND guid=?",
                   (outcome, app, guid))
        event(db, "route", guid, now=ts, state=outcome,
              generation=int(tgt["target_generation"]))
    return outcome


def release_quarantine(db: sqlite3.Connection, *, repo: str, pr: int,
                       actor: str = "operator", note: str = "",
                       now: float | None = None) -> bool:
    """Explicit, audited release of an imported quarantine.

    Crashed or interrupted work never auto-resumes: the operator
    releases it after reconciliation (or re-drives explicitly), and
    the release itself is journaled. Returns False when nothing was
    quarantined. Never clears silently, never via signal/sweep.
    """
    ts = _now(now)
    with tx(db):
        tgt = _target(db, repo, pr)
        if tgt is None or not int(tgt["quarantined"]):
            return False
        db.execute("UPDATE pl_pr_targets SET quarantined=0,"
                   "quarantine_note=?,updated_ts=? "
                   "WHERE repo=? AND pr=?",
                   (f"released by {actor}: {note[:200]}", ts, repo, pr))
        event(db, "quarantine_release", f"{repo}#{pr}", now=ts,
              actor=actor, note=note[:200])
    return True


def reroute(db: sqlite3.Connection, *, repo: str, pr: int,
            now: float | None = None) -> dict[str, str]:
    """Re-route every waiting receipt for a PR. The worker calls this
    after each reconcile commit that changes live state, so receipts
    that arrived before verification get their job without relying on
    a redelivery GitHub will never send."""
    ts = _now(now)
    out: dict[str, str] = {}
    for row in db.execute("SELECT app,guid FROM pl_inbox WHERE repo=? "
                          "AND pr=? AND state='pending'",
                          (repo, pr)).fetchall():
        out[row["guid"]] = route(db, app=row["app"], guid=row["guid"],
                                 now=ts)
    return out


def pending_receipts(db: sqlite3.Connection, repo: str | None = None,
                     pr: int | None = None) -> list[dict]:
    """Waiting receipts (never verified or not yet routed). Read-only."""
    if repo is not None and pr is not None:
        rows = db.execute("SELECT * FROM pl_inbox WHERE repo=? AND pr=? "
                          "AND state='pending' ORDER BY received_ts",
                          (repo, pr)).fetchall()
    else:
        rows = db.execute("SELECT * FROM pl_inbox WHERE state='pending' "
                          "ORDER BY received_ts").fetchall()
    return [dict(r) for r in rows]


def accept_synthetic(db: sqlite3.Connection, *, repo: str, pr: int,
                     head: str, repo_id: int | None = None,
                     lifecycle: str = "open",
                     now: float | None = None) -> str:
    """Accept a worker-originated receipt (sweep, redrive, migration).

    Deterministic GUID per (repo, PR, head): acceptance is idempotent,
    so the slow sweep can ensure work for a verified changed head
    without parsing journals or risking duplicates. The receipt still
    routes through the same signal/verify/route gates — this creates
    no job by itself.
    """
    return accept(db, app="synthetic",
                  guid=f"synthetic:{repo}:{pr}:{head}",
                  body=f"synthetic:{repo}:{pr}:{head}".encode(),
                  repo=repo, repo_id=repo_id, pr=pr, head=head,
                  lifecycle=lifecycle, now=now)


# -- admission: one lease, one reservation, all scopes, one txn --------------

def claim(db: sqlite3.Connection, *, owner: str, reserve_usd: float,
          scopes: tuple[str, ...] = ("cycle",), fingerprint: str = "",
          now: float | None = None,
          policy: Policy | None = None) -> Claim | None:
    """Admit the oldest pending or budget-deferred job whose target is
    still current.

    Ownership + fencing + attempt-keyed reservation + every selected
    budget scope commit atomically. Deferred jobs are re-examined on
    every admission pass, so freed headroom revives them without a new
    webhook. A stale target (head, generation or lifecycle moved since
    routing) supersedes the job instead of admitting it. Over any cap
    the job waits as deferred_budget. pause_reviews admits nothing
    new; a repo outside the allowlist never admits. Returns None when
    no job can be admitted right now.
    """
    pol = policy or Policy()
    ts = _now(now)
    reserve = money_mod.to_micro(reserve_usd)
    if reserve <= 0:
        raise ValueError("reservation must be positive")
    if not scopes or len(set(scopes)) != len(scopes):
        raise ValueError("scopes must be a non-empty unique set")
    with tx(db):
        if pol.pause_reviews:
            return None
        # Scan candidates in order. Foreign-repo jobs are skipped, not
        # superseded: removing a repo from the allowlist parks its
        # jobs (an explicit purge reaps them), it never spends on
        # them and never starves allowlisted work behind them. PRs
        # with a parked (awaiting_verification) job admit nothing
        # new: one review authority per PR; the newer head waits for
        # the park to resolve (reverify or lease-bounded expiry).
        # Quarantined targets never admit: crashed or interrupted
        # work needs an explicit, audited release first.
        row = None
        for cand in db.execute(
                "SELECT j.* FROM pl_jobs j "
                "WHERE j.state IN ('pending','deferred_budget') "
                "AND NOT EXISTS(SELECT 1 FROM pl_jobs x WHERE x.repo=j.repo "
                "AND x.pr=j.pr AND x.state IN ('leased','running',"
                "'awaiting_verification')) "
                "ORDER BY j.id"):
            if (pol.allowed_repos is not None
                    and cand["repo"] not in pol.allowed_repos):
                continue
            tgt = _target(db, cand["repo"], cand["pr"])
            if tgt is not None and int(tgt["quarantined"]):
                continue
            if (tgt is None or tgt["live_head"] != cand["full_head"]
                    or int(tgt["target_generation"]) !=
                    cand["target_generation"]
                    or tgt["lifecycle"] not in ELIGIBLE_LIFECYCLES):
                db.execute("UPDATE pl_jobs SET state='superseded' "
                           "WHERE id=?", (cand["id"],))
                event(db, "claim_stale", cand["id"], now=ts)
                continue
            row = cand
            break
        if row is None:
            return None
        for scope in scopes:
            cap = _row(db, "SELECT cap_micro FROM pl_budgets WHERE name=?",
                       (scope,))
            if cap is None:
                raise KeyError(f"unknown budget scope {scope!r}")
            if exposure(db, scope) + reserve > int(cap["cap_micro"]):
                db.execute("UPDATE pl_jobs SET state='deferred_budget' "
                           "WHERE id=?", (row["id"],))
                event(db, "deferred_budget", row["id"], now=ts,
                      scope=scope)
                return None
        fence = int(row["fence"]) + 1
        cur = db.execute("INSERT INTO pl_attempts(job,fence,reserve_micro) "
                         "VALUES(?,?,?)", (row["id"], fence, reserve))
        attempt = int(cur.lastrowid or 0)
        db.executemany("INSERT INTO pl_scopes(attempt,scope) VALUES(?,?)",
                       [(attempt, s) for s in scopes])
        db.execute("UPDATE pl_jobs SET state='leased',owner=?,fence=?,"
                   "lease_expires=?,attempt=?,fingerprint=? WHERE id=?",
                   (owner, fence, ts + pol.lease_s, attempt, fingerprint,
                    row["id"]))
        event(db, "claim", row["id"], now=ts, attempt=attempt,
              fence=fence)
        return Claim(row["id"], attempt, owner, fence)


def owned(db: sqlite3.Connection, claim: Claim,
          now: float | None = None) -> sqlite3.Row:
    ts = _now(now)
    row = _row(db, "SELECT * FROM pl_jobs WHERE id=? AND attempt=? "
                   "AND owner=? AND fence=? AND lease_expires>? "
                   "AND state IN ('leased','running',"
                   "'awaiting_verification')",
               (claim.job, claim.attempt, claim.owner, claim.fence, ts))
    if row is None:
        raise LeaseLost(f"claim {claim!r} no longer owns its job")
    return row


def heartbeat(db: sqlite3.Connection, claim: Claim,
              now: float | None = None,
              lease_s: float = 60.0) -> None:
    ts = _now(now)
    with tx(db):
        owned(db, claim, ts)
        db.execute("UPDATE pl_jobs SET lease_expires=? WHERE id=?",
                   (ts + lease_s, claim.job))


def _current(db: sqlite3.Connection, job: sqlite3.Row) -> bool:
    """Live-target verification shared by all paid-effect gates.

    Quarantine fails closed here (single choke point): crashed or
    interrupted work holds no lease that can authorize new spend or
    seal results, even if the lease predates the quarantine.
    """
    tgt = _target(db, job["repo"], job["pr"])
    return bool(tgt is not None
                and tgt["live_head"] == job["full_head"]
                and int(tgt["target_generation"]) == job["target_generation"]
                and tgt["lifecycle"] in ELIGIBLE_LIFECYCLES
                and not int(tgt["quarantined"]))


def dispatch_intent(db: sqlite3.Connection, claim: Claim, *,
                    stage: str, call_id: str, diff_hash: str = "",
                    now: float | None = None) -> bool:
    """Persist one paid-call intent. Only the lease holder may authorize.

    Every paid stage records its own intent row (unique call_id): the
    two-model pipeline authorizes terra and luna separately, each
    against the same attempt reservation (topped up between stages via
    top_up_reservation). A repeated call_id raises Conflict (never a
    silent second authorization for the same call).

    A crash before this commit is safely retryable. Once committed, a
    crash before network I/O is indistinguishable from a sent request
    with a lost response: expiry from here keeps the charge hold and
    holds for the operator (never auto-retries). Returns False (job
    superseded, reservation settled at zero) when the target moved.
    """
    ts = _now(now)
    with tx(db):
        job = owned(db, claim, ts)
        if job["state"] not in ("leased", "running"):
            raise Conflict("job is not dispatched-dispatchable")
        if not _current(db, job):
            calls = db.execute("SELECT COUNT(*) FROM pl_provider_calls "
                               "WHERE attempt=?",
                               (claim.attempt,)).fetchone()[0]
            if calls:
                # A paid intent already committed: the call may be on
                # the wire. Never zero-settle committed spend — hold
                # unknown for the operator instead.
                _settle(db, claim.attempt, None)
                db.execute("UPDATE pl_jobs SET state='needs_attention' "
                           "WHERE id=?", (job["id"],))
                event(db, "dispatch_stale", job["id"], now=ts,
                      held="unknown")
            else:
                _settle(db, claim.attempt, 0)
                db.execute("UPDATE pl_jobs SET state='superseded' "
                           "WHERE id=?", (job["id"],))
                event(db, "dispatch_stale", job["id"], now=ts)
            return False
        try:
            db.execute("INSERT INTO pl_provider_calls(attempt,stage,"
                       "call_id,phase,ts) VALUES(?,?,?,'intent',?)",
                       (claim.attempt, stage, call_id, ts))
        except sqlite3.IntegrityError:
            raise Conflict(f"dispatch intent {call_id!r} already exists")
        db.execute("UPDATE pl_attempts SET phase='intent' WHERE id=?",
                   (claim.attempt,))
        if diff_hash:
            db.execute("UPDATE pl_jobs SET diff_hash=? WHERE id=?",
                       (diff_hash, job["id"]))
        db.execute("UPDATE pl_jobs SET state='running' WHERE id=?",
                   (job["id"],))
        event(db, "dispatch_intent", job["id"], now=ts,
              attempt=claim.attempt, stage=stage)
    return True


def top_up_reservation(db: sqlite3.Connection, claim: Claim, *,
                       additional_usd: float,
                       now: float | None = None) -> bool:
    """Enlarge an attempt reservation before a further paid stage.

    Bounded top-ups keep multi-stage runs under an enforced bound even
    when a single upfront reservation cannot cover every stage. All
    scopes re-checked atomically; False when any cap would break (the
    worker must stop before spending, not after). Only the lease
    holder may top up.
    """
    ts = _now(now)
    additional = money_mod.to_micro(additional_usd)
    if additional <= 0:
        raise ValueError("top-up must be positive")
    with tx(db):
        job = owned(db, claim, ts)
        tgt = _target(db, job["repo"], job["pr"])
        if tgt is not None and int(tgt["quarantined"]):
            event(db, "topup_refused", job["id"], now=ts,
                  scope="quarantined")
            return False
        attempt = _attempt_by_id(db, claim.attempt)
        if attempt["state"] != "reserved":
            raise Conflict("only a reserved attempt can be topped up")
        scopes = [r[0] for r in db.execute(
            "SELECT scope FROM pl_scopes WHERE attempt=?", (claim.attempt,))]
        for scope in scopes:
            cap = _row(db, "SELECT cap_micro FROM pl_budgets WHERE name=?",
                       (scope,))
            if exposure(db, scope) + additional > int(cap["cap_micro"]):
                event(db, "topup_refused", job["id"], now=ts, scope=scope)
                return False
        db.execute("UPDATE pl_attempts SET reserve_micro=reserve_micro+? "
                   "WHERE id=?", (additional, claim.attempt))
        event(db, "topup", job["id"], now=ts, additional_micro=additional)
    return True


# -- keyed settlement ---------------------------------------------------------

def _settle(db: sqlite3.Connection, attempt: int,
            actual_micro: int | None, adjudicated: bool = False) -> None:
    """Keyed settlement. None actual = charge_unknown hold.

    Repeating a settlement is idempotent; a differing repeat or an
    over-reservation actual raises Conflict. An unknown hold converts
    to settled money only through resolve_unknown (adjudicated=True);
    completion replays must never silently release it.
    """
    row = _attempt_by_id(db, attempt)
    if row is None:
        raise Conflict("unknown reservation")
    if actual_micro is not None and not 0 <= actual_micro <= int(
            row["reserve_micro"]):
        raise Conflict("actual above enforced reservation")
    if row["state"] == "settled":
        if row["actual_micro"] != actual_micro:
            raise Conflict("settlement differs from previous settlement")
        return
    if row["state"] == "unknown" and actual_micro is not None \
            and not adjudicated:
        raise Conflict("unknown hold resolves only via resolve_unknown")
    if actual_micro is None:
        if row["state"] == "unknown":
            return  # already held: read-only, no write
        db.execute("UPDATE pl_attempts SET state='unknown' WHERE id=?",
                   (attempt,))
    else:
        db.execute("UPDATE pl_attempts SET state='settled',actual_micro=? "
                   "WHERE id=?", (actual_micro, attempt))


def complete(db: sqlite3.Connection, claim: Claim, *,
             actual_usd: float | None, run_path: str, manifest_sha: str,
             digest: str, summary: dict | None = None,
             effects: list[dict] | None = None,
             now: float | None = None) -> str:
    """Seal one run: result + settlement + checkpoint + effect intents.

    One transaction. actual_usd None = charge_unknown (hold kept).
    Stale completions keep real cost and history but create no effects
    and never advance the checkpoint. A provider-reported actual above
    the enforced reservation is never silently discarded and never
    auto-settled: the result is kept, the reservation settles, and the
    job holds for the operator with the overage recorded.

    Finalization compares the captured target generation AND dirty
    revision: if another signal arrived while inference ran, the job
    parks in awaiting_verification instead of sealing — the worker
    reconciles, then reverify() either resumes (duplicate signals
    only) or stands the run down. Effect intents are created by the
    sealing call, so a parked completion must repeat them on resume.

    Ownership: every mutating path requires an exact, current,
    unexpired owned claim first — including the replay/resume branch.
    The sole exception is an already-terminal identical completion,
    which answers read-only (no checkpoint, effect, or settlement
    write) without demanding a live lease. Returns the terminal state.
    """
    ts = _now(now)
    actual = (None if actual_usd is None
              else money_mod.to_micro(actual_usd))
    with tx(db):
        previous = _row(db, "SELECT * FROM pl_results WHERE attempt=?",
                        (claim.attempt,))
        if previous is not None:
            if previous["job"] != claim.job or previous["digest"] != digest:
                raise Conflict("conflicting replay of completion")
            job_row = _job_by_id(db, claim.job)
            if job_row is None:
                raise Conflict("result without a job; refusing to seal")
            if job_row["state"] not in ("leased", "running",
                                        "awaiting_verification"):
                # Already terminal and identical: read-only answer. Only
                # proven no-op inputs pass; anything else raises instead
                # of writing (unknown holds resolve via adjudication).
                attempt = _attempt_by_id(db, claim.attempt)
                capped = (actual if actual is None else min(
                    actual, int(attempt["reserve_micro"])))
                if attempt["state"] == "settled":
                    if attempt["actual_micro"] != capped:
                        raise Conflict("settlement differs from previous "
                                       "settlement")
                    return "already_complete"
                if attempt["state"] == "unknown" and actual is None:
                    return "already_complete"
                raise Conflict("terminal replay with new money; resolve "
                               "via resolve_unknown")
            # Job still live: the resumer must hold the exact current
            # claim — an expired, fenced, or foreign claim seals
            # nothing.
            job = owned(db, claim, ts)
            if job["state"] == "awaiting_verification":
                return _finalize_parked(db, claim, job, previous, digest,
                                        actual, effects, ts)
            if job["state"] == "running":
                # Resume after reverify (or a crash between the result
                # insert and the seal): the sealed evidence exists, the
                # job is live — finalize through the normal gates
                # instead of short-circuiting.
                attempt = _attempt_by_id(db, claim.attempt)
                capped = (actual if actual is None else min(
                    actual, int(attempt["reserve_micro"])))
                _settle(db, claim.attempt, capped)
                return _seal(db, job, effects, ts)
            raise Conflict("completion without dispatch intent")
        job = owned(db, claim, ts)
        if job["state"] != "running":
            raise Conflict("completion without dispatch intent")
        attempt = _attempt_by_id(db, claim.attempt)
        overage = (0 if actual is None else max(
            0, actual - int(attempt["reserve_micro"])))
        _settle(db, claim.attempt,
                actual if not overage else int(attempt["reserve_micro"]))
        merged = dict(summary or {})
        if overage:
            merged["overage_micro"] = overage
            merged["overage_note"] = (
                "provider-reported actual above the enforced "
                "reservation; settled at the reservation, overage held "
                "for operator adjudication")
        db.execute("INSERT INTO pl_results(job,attempt,run_path,"
                   "manifest_sha,digest,summary) VALUES(?,?,?,?,?,?)",
                   (claim.job, claim.attempt, run_path, manifest_sha,
                    digest, json.dumps(merged, sort_keys=True)))
        if overage:
            db.execute("UPDATE pl_jobs SET state='needs_attention' "
                       "WHERE id=?", (claim.job,))
            event(db, "complete", claim.job, now=ts,
                  state="needs_attention", overage_micro=overage)
            return "needs_attention"
        return _seal(db, job, effects, ts)


def _seal(db: sqlite3.Connection, job: sqlite3.Row,
          effects: list[dict] | None, ts: float) -> str:
    """Finalize a settled run: checkpoint + effect intents, or park.

    Seals only when the captured target generation AND dirty revision
    still verify. Fresh duplicate signals park the job in
    awaiting_verification (reconcile, then reverify — never seal
    blind); a moved target seals as superseded with cost kept and no
    effects.
    """
    if _current(db, job):
        tgt = _target(db, job["repo"], job["pr"])
        if tgt is not None and int(tgt["dirty_rev"]) != int(
                job["dirty_seen"]):
            db.execute("UPDATE pl_jobs SET state='awaiting_verification' "
                       "WHERE id=?", (job["id"],))
            event(db, "complete_deferred", job["id"], now=ts,
                  dirty_seen=int(job["dirty_seen"]),
                  dirty_now=int(tgt["dirty_rev"]))
            return "awaiting_verification"
        state = "succeeded"
    else:
        state = "superseded"
    db.execute("UPDATE pl_jobs SET state=? WHERE id=?",
               (state, job["id"]))
    if state == "succeeded":
        db.execute("UPDATE pl_pr_targets SET reviewed_head=?,"
                   "reviewed_base=?,updated_ts=? "
                   "WHERE repo=? AND pr=?",
                   (job["full_head"], job["full_base"], ts,
                    job["repo"], job["pr"]))
        for eff in effects or []:
            db.execute(
                "INSERT INTO pl_effects(job,channel,destination,"
                "content_revision,target_generation,lifecycle_version) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(job,channel,destination,content_revision) "
                "DO NOTHING",
                (job["id"], eff["channel"], eff.get("destination", ""),
                 eff.get("content_revision", ""),
                 int(job["target_generation"]),
                 int((_target(db, job["repo"],
                             job["pr"]) or {"lifecycle_version": 0})
                     ["lifecycle_version"])))
    event(db, "complete", job["id"], now=ts, state=state)
    return state


def _finalize_parked(db: sqlite3.Connection, claim: Claim,
                     job: sqlite3.Row, previous: sqlite3.Row | None,
                     digest: str, actual: int | None,
                     effects: list[dict] | None, ts: float) -> str:
    """Resume-finalize a parked job against the reconciled observation.

    Requires the previously parked evidence (same digest): the sealed
    result is authoritative, caller input is verified, never
    re-applied. Idempotent settlement first, then the same seal gates
    as a fresh completion.
    """
    if previous is None or previous["job"] != job["id"] \
            or previous["digest"] != digest:
        raise Conflict("parked job resumes only with its parked evidence")
    attempt = _attempt_by_id(db, claim.attempt)
    capped = (actual if actual is None else min(
        actual, int(attempt["reserve_micro"])))
    _settle(db, claim.attempt, capped)
    return _seal(db, job, effects, ts)


def resolve_unknown(db: sqlite3.Connection, *, attempt: int,
                    actual_usd: float | None, actor: str = "operator",
                    note: str = "", now: float | None = None) -> str:
    """Operator adjudication of an unknown-charge hold.

    actual_usd None keeps the hold (acknowledged, still blocking
    headroom). A value within the reservation settles it. A value
    above the reservation settles the ledger at the bound and records
    the overage in the journal; the Slice-3 adjudication UI turns that
    record into a true-up. Returns the attempt state.
    """
    ts = _now(now)
    actual = (None if actual_usd is None
              else money_mod.to_micro(actual_usd))
    with tx(db):
        row = _attempt_by_id(db, attempt)
        if row is None:
            raise Conflict("unknown reservation")
        if row["state"] != "unknown":
            return row["state"]
        if actual is None:
            event(db, "adjudicate", attempt, now=ts, actor=actor,
                  outcome="hold_kept", note=note[:200])
            return "unknown"
        if actual <= int(row["reserve_micro"]):
            _settle(db, attempt, actual, adjudicated=True)
            event(db, "adjudicate", attempt, now=ts, actor=actor,
                  outcome="settled", actual_micro=actual)
            return "settled"
        _settle(db, attempt, int(row["reserve_micro"]), adjudicated=True)
        event(db, "adjudicate", attempt, now=ts, actor=actor,
              outcome="overage_held",
              overage_micro=actual - int(row["reserve_micro"]),
              note=note[:200])
        return "settled"


# -- recovery -------------------------------------------------------------------

def reverify(db: sqlite3.Connection, claim: Claim,
             now: float | None = None) -> bool:
    """Resume or stand down a job parked in awaiting_verification.

    The worker calls this after reconciling: if the target still
    verifies (the intervening signals were duplicates), the job
    returns to running with a refreshed dirty capture and the same
    claim may complete. Otherwise the run stands down through the
    intent-aware path (post-intent keeps an unknown hold and holds
    for the operator; pre-intent settles at zero). Only the lease
    holder may reverify.
    """
    ts = _now(now)
    with tx(db):
        job = _row(db, "SELECT * FROM pl_jobs WHERE id=? AND attempt=? "
                       "AND owner=? AND fence=? AND lease_expires>? "
                       "AND state='awaiting_verification'",
                   (claim.job, claim.attempt, claim.owner, claim.fence,
                    ts))
        if job is None:
            raise LeaseLost(f"claim {claim!r} has no parked job")
        tgt = _target(db, job["repo"], job["pr"])
        if (tgt is not None
                and tgt["live_head"] == job["full_head"]
                and int(tgt["target_generation"]) == job["target_generation"]
                and tgt["lifecycle"] in ELIGIBLE_LIFECYCLES):
            db.execute("UPDATE pl_jobs SET state='running',dirty_seen=? "
                       "WHERE id=?", (int(tgt["dirty_rev"]), job["id"]))
            event(db, "reverify", job["id"], now=ts, resumed=True)
            return True
        # Stood down with cost kept: parked completions already
        # settled (unknown holds stay held for the operator).
        att = _attempt_by_id(db, claim.attempt)
        if att["state"] == "reserved":
            _settle(db, claim.attempt, 0)
            db.execute("UPDATE pl_jobs SET state='superseded' WHERE id=?",
                       (job["id"],))
        elif att["state"] == "unknown":
            db.execute("UPDATE pl_jobs SET state='needs_attention' "
                       "WHERE id=?", (job["id"],))
        else:
            db.execute("UPDATE pl_jobs SET state='superseded' WHERE id=?",
                       (job["id"],))
        event(db, "reverify", job["id"], now=ts, resumed=False)
        return False

def recover(db: sqlite3.Connection, now: float | None = None) -> dict:
    """Reap expired leases. Never re-spends, never seals for the dead.

    Pre-dispatch expiry: safely retryable when still current (pending),
    else superseded. Post-intent expiry: the external call may have
    happened — charge_unknown hold, needs_attention, operator decides.
    Expired sending effects: ambiguous when a POST may have gone out,
    else back to pending. Fences advance so a resumed old worker can
    neither seal nor release another attempt's reservation.
    """
    ts = _now(now)
    out: dict[str, list] = {"recovered": [], "effects": []}
    with tx(db):
        for job in db.execute("SELECT * FROM pl_jobs WHERE state IN "
                              "('leased','running','awaiting_verification') "
                              "AND lease_expires<=?",
                              (ts,)).fetchall():
            attempt = _attempt_by_id(db, job["attempt"])
            if attempt is not None and attempt["state"] == "settled":
                # Sealed money is never un-settled by a crash: a parked
                # completion already settled its known actual, so the
                # run stands down with cost kept (never re-held,
                # never re-spent).
                db.execute("UPDATE pl_jobs SET state='superseded',"
                           "fence=fence+1,owner='' WHERE id=?",
                           (job["id"],))
                event(db, "recover", job["id"], now=ts,
                      state="superseded", settled_kept=True)
                out["recovered"].append({"job": job["id"],
                                         "state": "superseded"})
                continue
            if attempt is not None and attempt["phase"] == "intent":
                db.execute("UPDATE pl_attempts SET state='unknown' "
                           "WHERE id=?", (attempt["id"],))
                state = "needs_attention"
            else:
                if attempt is not None:
                    _settle(db, attempt["id"], 0)
                state = ("pending" if _current(db, job) else "superseded")
            db.execute("UPDATE pl_jobs SET state=?,fence=fence+1,owner='' "
                       "WHERE id=?", (state, job["id"]))
            event(db, "recover", job["id"], now=ts, state=state)
            out["recovered"].append({"job": job["id"], "state": state})
        for eff in db.execute("SELECT * FROM pl_effects WHERE state='sending'"
                              " AND lease_expires<=?", (ts,)).fetchall():
            attempts = db.execute(
                "SELECT phase FROM pl_effect_attempts WHERE effect=? "
                "ORDER BY id DESC LIMIT 1", (eff["id"],)).fetchone()
            last_phase = attempts["phase"] if attempts else "lookup"
            if int(eff["uncertain"]) or last_phase == "post_intent":
                db.execute("UPDATE pl_effects SET state='unknown',"
                           "uncertain=1,token='' WHERE id=?", (eff["id"],))
                state = "unknown"
            else:
                db.execute("UPDATE pl_effects SET state='pending',token='' "
                           "WHERE id=?", (eff["id"],))
                state = "pending"
            event(db, "recover_effect", eff["id"], now=ts, state=state)
            out["effects"].append({"effect": eff["id"], "state": state})
    return out


# -- effects outbox ---------------------------------------------------------------

def begin_effect(db: sqlite3.Connection, effect_id: int,
                 now: float | None = None,
                 policy: Policy | None = None) -> dict | None:
    """Lease one effect for delivery. Returns the leased row or None.

    None (held) covers: already finished, ambiguous email (never
    auto-resent), email when digests are disabled, paused publication,
    or a target that is no longer current — stale/closed/draft effects
    suppress instead of sending. Email follows the same currentness
    policy as comments.
    """
    pol = policy or Policy()
    ts = _now(now)
    with tx(db):
        if pol.pause_publication:
            return None
        eff = _row(db, "SELECT * FROM pl_effects WHERE id=?", (effect_id,))
        if eff is None or eff["state"] not in ("pending", "unknown"):
            return None
        if eff["channel"] == "email" and (eff["state"] == "unknown"
                                          or not pol.email_allowed):
            return None
        job = _job_by_id(db, eff["job"])
        tgt = (_target(db, job["repo"], job["pr"]) if job else None)
        current = bool(
            job is not None and tgt is not None
            and tgt["live_head"] == job["full_head"]
            and int(tgt["target_generation"]) == job["target_generation"]
            and int(tgt["lifecycle_version"]) == eff["lifecycle_version"]
            and tgt["lifecycle"] in ELIGIBLE_LIFECYCLES
            and not int(tgt["quarantined"]))
        if not current and eff["state"] == "pending":
            db.execute("UPDATE pl_effects SET state='suppressed' "
                       "WHERE id=?", (effect_id,))
            event(db, "effect_suppressed", effect_id, now=ts)
            return None
        if not current:
            return None
        token = uuid.uuid4().hex
        db.execute("UPDATE pl_effects SET state='sending',token=?,"
                   "lease_expires=? WHERE id=?",
                   (token, ts + pol.lease_s, effect_id))
        db.execute("INSERT INTO pl_effect_attempts(effect,phase,ts) "
                   "VALUES(?,'lookup',?)", (effect_id, ts))
        event(db, "effect_claim", effect_id, now=ts,
              channel=eff["channel"])
        row = dict(eff)
        row["token"] = token
        return row


def authorize_send(db: sqlite3.Connection, effect: dict,
                   now: float | None = None,
                   policy: Policy | None = None) -> bool:
    """Second gate after read-only lookup: commit the POST intent.

    Re-verifies ownership, lease, phase, currentness AND the live
    operator pause in the same transaction: a pause that lands while
    the read-only lookup is in flight holds the send instead of
    racing it. A lookup-only crash before this point stays safely
    retryable; from here the send may already be on the wire.
    """
    pol = policy or Policy()
    ts = _now(now)
    with tx(db):
        if pol.pause_publication:
            return False
        row = _row(db, "SELECT * FROM pl_effects WHERE id=? AND token=? "
                       "AND state='sending' AND uncertain=0 AND "
                       "lease_expires>?", (effect["id"], effect["token"], ts))
        if row is None:
            raise LeaseLost("no current safe authority to send")
        if row["channel"] == "email" and not pol.email_allowed:
            return False
        last = db.execute(
            "SELECT phase FROM pl_effect_attempts WHERE effect=? "
            "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        if last is not None and last["phase"] == "post_intent":
            # Already authorized (same token, still valid): idempotent,
            # no duplicate intent row, no second send permission minted.
            return True
        job = _job_by_id(db, row["job"])
        tgt = (_target(db, job["repo"], job["pr"]) if job else None)
        current = bool(
            job is not None and tgt is not None
            and tgt["live_head"] == job["full_head"]
            and int(tgt["target_generation"]) == job["target_generation"]
            and int(tgt["lifecycle_version"]) == row["lifecycle_version"]
            and tgt["lifecycle"] in ELIGIBLE_LIFECYCLES
            and not int(tgt["quarantined"]))
        if not current:
            db.execute("UPDATE pl_effects SET state='suppressed' "
                       "WHERE id=?", (row["id"],))
            return False
        db.execute("INSERT INTO pl_effect_attempts(effect,phase,ts) "
                   "VALUES(?,'post_intent',?)", (row["id"], ts))
        event(db, "effect_dispatch_intent", row["id"], now=ts)
    return True


def finish_effect(db: sqlite3.Connection, effect: dict, outcome: str,
                  remote_id: str = "",
                  now: float | None = None) -> None:
    """Record the delivery outcome. Only the token holder commits.

    'pending' after a post_intent is refused: once the send may be on
    the wire, only confirmed/unknown/failed describe the outcome —
    never quietly re-arm a possibly-sent effect.
    """
    if outcome not in ("confirmed", "unknown", "pending", "failed"):
        raise ValueError(f"bad effect outcome {outcome!r}")
    ts = _now(now)
    with tx(db):
        last = db.execute(
            "SELECT phase FROM pl_effect_attempts WHERE effect=? "
            "ORDER BY id DESC LIMIT 1", (effect["id"],)).fetchone()
        if outcome == "pending" and last is not None and (
                last["phase"] in ("post_intent", "receipt")):
            raise Conflict("cannot re-arm an effect after POST intent")
        changed = int(db.execute(
            "UPDATE pl_effects SET state=?,remote_id=?,"
            "uncertain=? WHERE id=? AND token=? AND state='sending'",
            (outcome, remote_id, 1 if outcome == "unknown" else 0,
             effect["id"], effect["token"])).rowcount or 0)
        if not changed:
            raise LeaseLost("effect token no longer owns delivery")
        db.execute("INSERT INTO pl_effect_attempts(effect,phase,outcome,"
                   "remote_id,ts) VALUES(?,'receipt',?,?,?)",
                   (effect["id"], outcome, remote_id, ts))
        event(db, "effect_result", effect["id"], now=ts, outcome=outcome)


def deliver_effect(db: sqlite3.Connection, effect_id: int, remote,
                   now: float | None = None,
                   policy: Policy | None = None) -> str:
    """Deliver one effect through lookup/post separation. No auto-dupe.

    A failed lookup before any POST intent is safely retryable
    ('pending'). An uncertain POST reconciles a positive marker only:
    absence, lookup failure or incomplete pagination never authorizes
    a second POST. Ambiguous email stays held.
    """
    ts = _now(now)
    effect = begin_effect(db, effect_id, ts, policy)
    if effect is None:
        return "held_or_finished"
    if effect["channel"] == "comment":
        try:
            existing = remote.lookup(effect_job_marker(effect))
        except (TimeoutError, ConnectionError):
            finish_effect(db, effect,
                          "unknown" if effect["uncertain"] else "pending",
                          now=ts)
            return "unknown" if effect["uncertain"] else "pending"
        if existing:
            # Historical confirmation stays valid after head changes.
            finish_effect(db, effect, "confirmed", existing, ts)
            return "confirmed"
        if effect["uncertain"]:
            finish_effect(db, effect, "unknown", now=ts)
            return "unknown"
    if not authorize_send(db, effect, ts, policy):
        return "suppressed"
    try:
        remote_id = remote.send(effect_job_marker(effect))
    except (TimeoutError, ConnectionError):
        finish_effect(db, effect, "unknown", now=ts)
        return "unknown"
    finish_effect(db, effect, "confirmed", remote_id, ts)
    return "confirmed"


def effect_job_marker(effect: dict) -> str:
    """Stable remote identity for reconciliation.

    (job, channel, destination, content_revision): two emails to
    different destinations never share a marker, so reconciling one
    can never confirm the other.
    """
    return (f"job:{effect['job']}:{effect['channel']}:"
            f"{effect.get('destination') or ''}:"
            f"{effect['content_revision']}")


def find_posted_marker(db: sqlite3.Connection, repo: str, pr: int,
                       head_sha: str) -> dict | None:
    """New-publisher reconciliation against the migration import.

    Returns the newest posted v1 publication marker for (repo, PR,
    head), if any, so the Slice-2 publisher confirms instead of
    double-posting after cutover, rerun or re-root. Read-only.
    """
    row = _row(db, "SELECT * FROM pl_publications_imported WHERE repo=? "
                   "AND pr=? AND head_sha=?",
               (repo, pr, head_sha))
    return dict(row) if row else None


def reconcile_effect(db: sqlite3.Connection, effect_id: int, remote,
                     now: float | None = None) -> str:
    """Read-only discovery of an earlier send. Never posts.

    Permitted after close/head-change/pause: those gates forbid a new
    send, not learning that an earlier send already happened. Positive
    marker -> confirmed; anything else leaves 'unknown' undisturbed.
    """
    ts = _now(now)
    with tx(db):
        eff = _row(db, "SELECT * FROM pl_effects WHERE id=?", (effect_id,))
        if eff is None or eff["state"] != "unknown":
            return "held_or_finished"
        marker = effect_job_marker(dict(eff))
    try:
        existing = remote.lookup(marker)
    except (TimeoutError, ConnectionError):
        return "unknown"
    if not existing:
        return "unknown"
    with tx(db):
        db.execute("UPDATE pl_effects SET state='confirmed',remote_id=?,"
                   "uncertain=0 WHERE id=? AND state='unknown'",
                   (existing, effect_id))
        event(db, "effect_result", effect_id, now=ts,
              outcome="confirmed", via="reconcile")
    return "confirmed"


# -- journal reads ------------------------------------------------------------------

def events_after(db: sqlite3.Connection, cursor: int = 0,
                 limit: int = 100) -> list[dict]:
    rows = db.execute("SELECT * FROM pl_events WHERE seq>? ORDER BY seq "
                      "LIMIT ?", (cursor, limit)).fetchall()
    return [dict(r) for r in rows]


# Event kinds whose ref is a job id. Effect, attempt, reconcile,
# route, signal and receipt events share the same unqualified numeric
# namespace (effect 1, attempt 1 and job 1 coexist), so any
# job-scoped lookup must filter by kind — never by ref alone.
# Adjudications are attempt-scoped (resolve_unknown journals against
# the attempt), so the hold query includes them by attempt id.
JOB_EVENT_KINDS = frozenset({
    "claim", "claim_stale", "complete", "complete_deferred",
    "deferred_budget", "dispatch_intent", "dispatch_stale", "recover",
    "reverify", "topup", "topup_refused",
})


def unknown_holds(db: sqlite3.Connection) -> list[dict]:
    """Operator attention query: every unresolved unknown-charge hold.

    Finds holds regardless of job lifecycle (leased, running, parked,
    superseded, cancelled, needs_attention): an unknown attempt is
    money the ledger cannot account for until adjudicated, and no
    state transition may silently settle or discard it. Each row
    carries the job, target, reservation, last event, and whether a
    result artifact exists, so the operator can reconcile in full.
    Read-only.
    """
    kinds = sorted(JOB_EVENT_KINDS)
    rows = db.execute(
        "SELECT a.id AS attempt,a.job,a.fence,a.reserve_micro,a.phase,"
        "j.repo,j.pr,j.state AS job_state,j.owner,j.full_head,"
        "j.created_ts AS job_created,"
        "t.live_head,t.lifecycle AS target_lifecycle,"
        "t.quarantined,"
        "(SELECT COUNT(*) FROM pl_results r WHERE r.attempt=a.id) "
        "AS has_result,"
        "(SELECT MAX(seq) FROM pl_events e WHERE "
        "(e.ref=CAST(a.job AS TEXT)"
        f" AND e.kind IN ({','.join('?' * len(kinds))})) "
        "OR (e.kind='adjudicate' AND e.ref=CAST(a.id AS TEXT))) "
        "AS last_event_seq "
        "FROM pl_attempts a JOIN pl_jobs j ON j.id=a.job "
        "LEFT JOIN pl_pr_targets t ON t.repo=j.repo AND t.pr=j.pr "
        "WHERE a.state='unknown' ORDER BY a.id", kinds).fetchall()
    return [dict(r) for r in rows]


def snapshot(db: sqlite3.Connection, cursor: int = 0,
             limit: int = 100) -> dict:
    """Read journal state and position in one read transaction.

    Returns {"position", "events"}: subscribe after `position` to
    avoid the load/subscribe race. Browser subscribers never mutate.
    Requires a quiet connection (raises if the caller holds a write
    transaction — snapshotting mid-transaction would observe a
    private, uncommitted position).
    """
    if db.in_transaction:
        raise RuntimeError("snapshot requires a quiet connection")
    db.execute("BEGIN")
    try:
        position = int(_row(
            db, "SELECT COALESCE(MAX(seq),0) FROM pl_events")[0])
        events = [dict(r) for r in db.execute(
            "SELECT * FROM pl_events WHERE seq>? ORDER BY seq LIMIT ?",
            (cursor, limit)).fetchall()]
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return {"position": position, "events": events}


def snapshot_position(db: sqlite3.Connection) -> int:
    row = _row(db, "SELECT COALESCE(MAX(seq),0) FROM pl_events")
    return int(row[0])


class InvariantError(RuntimeError):
    """A platform invariant is violated: refuse, never proceed."""


def assert_invariants(db: sqlite3.Connection) -> None:
    """Test/dev consistency check. Raises InvariantError (not assert:
    it must fire under python -O too). An operator cap lowered below
    current exposure trips this deliberately — surface, don't spend."""
    for b in db.execute("SELECT * FROM pl_budgets").fetchall():
        exp = exposure(db, b["name"])
        if not 0 <= exp <= int(b["cap_micro"]):
            raise InvariantError(f"budget {b['name']}: exposure {exp} "
                                 f"outside [0, {b['cap_micro']}]")
    checks = {
        "multiple leases": (
            "SELECT repo,pr FROM pl_jobs WHERE state IN ('leased',"
            "'running','awaiting_verification') "
            "GROUP BY repo,pr HAVING COUNT(*)>1"),
        "succeeded without result": (
            "SELECT j.id FROM pl_jobs j LEFT JOIN pl_results r "
            "ON r.job=j.id WHERE j.state='succeeded' AND r.job IS NULL"),
        "effect on undecided job": (
            "SELECT e.id FROM pl_effects e JOIN pl_jobs j ON j.id=e.job "
            "WHERE j.state!='succeeded'"),
        "lease/attempt mismatch": (
            "SELECT j.id FROM pl_jobs j JOIN pl_attempts a ON a.id=j.attempt "
            "WHERE j.state IN ('leased','running') "
            "AND (a.fence!=j.fence OR a.state!='reserved')"),
        "unexplained needs_attention": (
            "SELECT j.id FROM pl_jobs j JOIN pl_attempts a ON a.id=j.attempt "
            "WHERE j.state='needs_attention' AND a.state='reserved'"),
    }
    for name, sql in checks.items():
        if db.execute(sql).fetchall():
            raise InvariantError(f"invariant violated: {name}")
