"""Slice 1 contract tests: inbox, targets, leases, money, effects, journal.

No network, no inference, no GitHub. Time is scripted. Every major
path ends with assert_invariants.
"""

from __future__ import annotations

import pytest

from codatiel_i2.platform import db as db_mod
from codatiel_i2.platform import money as money_mod
from codatiel_i2.platform import queue as q
from codatiel_i2.platform import schema as schema_mod

REPO = "o/r"


def _open(tmp_path, name="t.sqlite"):
    conn = db_mod.connect(tmp_path / name)
    schema_mod.ensure(conn)
    return conn


def _budgets(conn, **caps):
    for scope, cap in caps.items():
        q.set_budget(conn, scope, cap, now=1000.0)


def _live(conn, pr, head, gen=1, dirty=1, now=1000.0):
    """Signal + fenced verify, the only path that sets live_head."""
    q.signal(conn, repo=REPO, pr=pr, head=head, lifecycle="open",
             now=now)
    claim = q.reconcile_claim(conn, repo=REPO, pr=pr, owner="recon",
                              now=now)
    assert claim is not None
    assert q.reconcile_commit(conn, repo=REPO, pr=pr, owner="recon",
                              fence=claim["fence"],
                              generation=claim["generation"],
                              dirty=claim["dirty"], head=head, now=now)


def _admit(conn, pr, head, reserve_usd=1.0, scopes=("cycle",),
           guid=None, now=1000.0, owner="w1"):
    guid = guid or f"g-{pr}-{head[:8]}"
    assert q.accept(conn, app="gh", guid=guid, body=b"{}", repo=REPO,
                    repo_id=1, pr=pr, head=head, now=now) == "accepted"
    assert q.route(conn, app="gh", guid=guid, now=now) == "routed"
    return q.claim(conn, owner=owner, reserve_usd=reserve_usd,
                   scopes=scopes, fingerprint="fp", now=now)


# -- money -----------------------------------------------------------

def test_money_fixed_point(tmp_path):
    assert money_mod.to_micro(0.390696) == 390696
    assert money_mod.to_micro(1.0) == 1_000_000
    for bad in (-1.0, float("nan"), float("inf"), "1", True):
        with pytest.raises(ValueError):
            money_mod.to_micro(bad)
    assert money_mod.to_usd(390696) == pytest.approx(0.390696)
    assert money_mod.fmt_usd(390696) == "$0.39"
    conn = _open(tmp_path)
    q.assert_invariants(conn)


# -- inbox -----------------------------------------------------------

def test_accept_duplicate_and_conflict(tmp_path):
    conn = _open(tmp_path)
    args = dict(app="gh", guid="d1", repo=REPO, repo_id=7, pr=10,
                head="h" * 40)
    assert q.accept(conn, body=b"{}", now=1000.0, **args) == "accepted"
    assert q.accept(conn, body=b"{}", now=1001.0, **args) == "duplicate"
    with pytest.raises(q.Conflict):
        q.accept(conn, body=b'{"other":1}', now=1002.0, **args)
    rows = conn.execute("SELECT outcome FROM pl_receipt_attempts "
                        "ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["accepted", "duplicate", "conflict"]
    # Original receipt preserved, body never stored.
    row = conn.execute("SELECT * FROM pl_inbox").fetchone()
    assert row["head"] == "h" * 40
    q.assert_invariants(conn)


# -- signals ---------------------------------------------------------

def test_signal_generation_rules(tmp_path):
    conn = _open(tmp_path)
    r = q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
                 now=1000.0)
    assert (r["generation"], r["dirty"], r["scheduled"]) == (1, 1, True)
    r = q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
                 now=1001.0)
    assert (r["generation"], r["dirty"], r["scheduled"]) == (1, 2, False)
    # A new wake-up head is recorded and scheduled, but the generation
    # advances only when the fenced commit verifies the observation:
    # duplicate/out-of-order webhooks never churn generations.
    r = q.signal(conn, repo=REPO, pr=1, head="b" * 40, lifecycle="open",
                 now=1002.0)
    assert (r["generation"], r["scheduled"]) == (1, True)
    cl = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r", now=1002.0)
    assert q.reconcile_commit(conn, repo=REPO, pr=1, owner="r",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head="b" * 40, now=1002.0)
    assert conn.execute(
        "SELECT target_generation FROM pl_pr_targets").fetchone()[0] == 2
    # Draft head changes wait; readiness bumps even with unchanged head.
    q.signal(conn, repo=REPO, pr=2, head="c" * 40, lifecycle="draft",
             now=1003.0)
    r = q.signal(conn, repo=REPO, pr=2, head="d" * 40, lifecycle="draft",
                 now=1004.0)
    assert (r["generation"], r["scheduled"]) == (1, False)
    r = q.signal(conn, repo=REPO, pr=2, head="d" * 40, lifecycle="open",
                 now=1005.0)
    assert (r["generation"], r["scheduled"]) == (2, True)
    # Close cancels; reopen at the same head resumes with a new
    # generation (the cancelled job was never a reviewed head).
    q.signal(conn, repo=REPO, pr=2, lifecycle="closed", now=1006.0)
    r = q.signal(conn, repo=REPO, pr=2, head="d" * 40, lifecycle="open",
                 now=1007.0)
    assert (r["generation"], r["scheduled"]) == (3, True)
    q.assert_invariants(conn)


def test_signal_close_cancels_pending_effects_once(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _live(conn, 3, "a" * 40)
    claim = _admit(conn, 3, "a" * 40)
    assert q.dispatch_intent(conn, claim, stage="terra", call_id="c1",
                             now=1000.0)
    st = q.complete(conn, claim, actual_usd=0.5, run_path="/r/1",
                    manifest_sha="m", digest="d",
                    effects=[{"channel": "comment"}],
                    now=1000.0)
    assert st == "succeeded"
    eff = conn.execute("SELECT * FROM pl_effects").fetchone()
    assert eff["state"] == "pending"
    q.signal(conn, repo=REPO, pr=3, lifecycle="closed", now=1001.0)
    assert conn.execute("SELECT state FROM pl_effects").fetchone()[0] \
        == "cancelled"
    v1 = conn.execute("SELECT lifecycle_version FROM pl_pr_targets").fetchone()[0]
    q.signal(conn, repo=REPO, pr=3, lifecycle="closed", now=1002.0)
    assert conn.execute(
        "SELECT lifecycle_version FROM pl_pr_targets").fetchone()[0] == v1
    q.assert_invariants(conn)


# -- reconciliation fencing ------------------------------------------

def test_reconcile_fencing(tmp_path):
    conn = _open(tmp_path)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, now=1000.0)
    c1 = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r1", now=1000.0)
    assert c1 is not None
    assert q.reconcile_claim(conn, repo=REPO, pr=1, owner="r2",
                             now=1000.0) is None
    # A newer signal invalidates the captured dirty: late commit drops.
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, now=1001.0)
    assert not q.reconcile_commit(conn, repo=REPO, pr=1, owner="r1",
                                  fence=c1["fence"],
                                  generation=c1["generation"],
                                  dirty=c1["dirty"], head="a" * 40,
                                  now=1001.0)
    # Fresh claim after expiry commits.
    c2 = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r2", now=1031.0)
    assert c2 is not None and c2["fence"] == c1["fence"] + 1
    assert q.reconcile_commit(conn, repo=REPO, pr=1, owner="r2",
                              fence=c2["fence"],
                              generation=c2["generation"],
                              dirty=c2["dirty"], head="b" * 40, now=1031.0)
    assert conn.execute(
        "SELECT live_head FROM pl_pr_targets").fetchone()[0] == "b" * 40
    q.assert_invariants(conn)


# -- routing ---------------------------------------------------------

def test_route_waits_stale_and_coalesces(tmp_path):
    conn = _open(tmp_path)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, now=1000.0)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "pending"
    _live(conn, 1, "b" * 40, now=1001.0)
    # Receipt head is authoritatively older than verified live: stale.
    assert q.route(conn, app="gh", guid="g1", now=1001.0) == "stale"
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=1, head="b" * 40, now=1002.0)
    assert q.route(conn, app="gh", guid="g2", now=1002.0) == "routed"
    q.accept(conn, app="gh", guid="g3", body=b"3", repo=REPO,
             repo_id=1, pr=1, head="b" * 40, now=1003.0)
    assert q.route(conn, app="gh", guid="g3", now=1003.0) == "routed"
    jobs = conn.execute("SELECT COUNT(*) FROM pl_jobs").fetchone()[0]
    assert jobs == 1  # concurrent signals coalesce to one logical job
    assert q.route(conn, app="gh", guid="g2", now=1004.0) == "routed"
    q.assert_invariants(conn)


# -- admission -------------------------------------------------------

def test_claim_atomic_budget_pause_and_single_lease(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0, day=1.5)
    _live(conn, 1, "a" * 40)
    c = _admit(conn, 1, "a" * 40, reserve_usd=1.0,
               scopes=("cycle", "day"))
    assert isinstance(c, q.Claim)
    # Second claimant for the same PR gets nothing while leased.
    _live(conn, 1, "a" * 40, now=1001.0)
    assert q.claim(conn, owner="w2", reserve_usd=1.0, now=1001.0) is None
    # Over the day cap while under cycle: deferred, atomically.
    _live(conn, 2, "b" * 40, now=1002.0)
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=2, head="b" * 40, now=1002.0)
    assert q.route(conn, app="gh", guid="g2", now=1002.0) == "routed"
    assert q.claim(conn, owner="w2", reserve_usd=1.0,
                   scopes=("cycle", "day"), now=1002.0) is None
    assert conn.execute("SELECT state FROM pl_jobs WHERE pr=2").fetchone()[0] \
        == "deferred_budget"
    # pause_reviews admits nothing new.
    _live(conn, 3, "c" * 40, now=1003.0)
    q.accept(conn, app="gh", guid="g3", body=b"3", repo=REPO,
             repo_id=1, pr=3, head="c" * 40, now=1003.0)
    assert q.route(conn, app="gh", guid="g3", now=1003.0) == "routed"
    assert q.claim(conn, owner="w3", reserve_usd=0.5, now=1003.0,
                   policy=q.Policy(pause_reviews=True)) is None
    assert conn.execute("SELECT state FROM pl_jobs WHERE pr=3").fetchone()[0] \
        == "pending"
    with pytest.raises(KeyError):
        q.claim(conn, owner="w3", reserve_usd=0.5, scopes=("nope",),
                now=1003.0)
    with pytest.raises(ValueError):
        q.claim(conn, owner="w3", reserve_usd=0.0, now=1003.0)
    q.assert_invariants(conn)


def test_claim_supersedes_stale_target(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _live(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    _live(conn, 1, "b" * 40, now=1001.0)  # head moved before admission
    assert q.claim(conn, owner="w1", reserve_usd=1.0, now=1001.0) is None
    assert conn.execute("SELECT state FROM pl_jobs").fetchone()[0] \
        == "superseded"
    q.assert_invariants(conn)


# -- dispatch / complete ---------------------------------------------

def test_dispatch_and_complete_single_transaction(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _live(conn, 1, "a" * 40)
    claim = _admit(conn, 1, "a" * 40, reserve_usd=1.0)
    assert q.dispatch_intent(conn, claim, stage="terra", call_id="c1",
                             diff_hash="diff", now=1000.0)
    # Same call_id never authorizes twice; a second paid stage gets its
    # own intent row (bounded by top-ups, not by a second reservation).
    with pytest.raises(q.Conflict):
        q.dispatch_intent(conn, claim, stage="terra", call_id="c1",
                          now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="luna", call_id="c2",
                             now=1000.0)
    assert q.top_up_reservation(conn, claim, additional_usd=0.5,
                                now=1000.0)
    assert q.exposure(conn, "cycle") == 1_500_000
    state = q.complete(conn, claim, actual_usd=0.390696, run_path="/r/1",
                       manifest_sha="m", digest="d",
                       summary={"findings": 2},
                       effects=[{"channel": "comment"},
                                {"channel": "email",
                                 "destination": "ops@x"}],
                       now=1000.0)
    assert state == "succeeded"
    assert conn.execute(
        "SELECT reviewed_head FROM pl_pr_targets").fetchone()[0] == "a" * 40
    assert conn.execute("SELECT COUNT(*) FROM pl_results").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pl_effects").fetchone()[0] == 2
    assert q.exposure(conn, "cycle") == 390696
    # Replay is idempotent; a conflicting replay raises.
    assert q.complete(conn, claim, actual_usd=0.390696, run_path="/r/1",
                      manifest_sha="m", digest="d", now=1000.0) \
        == "already_complete"
    assert q.exposure(conn, "cycle") == 390696
    with pytest.raises(q.Conflict):
        q.complete(conn, claim, actual_usd=0.1, run_path="/r/1",
                   manifest_sha="m", digest="other", now=1000.0)
    # An over-reservation actual keeps its evidence, settles at the
    # reservation, and holds the job for the operator — never silently
    # discarded, never auto-settled above the bound.
    _live(conn, 9, "z" * 40, now=1001.0)
    small = _admit(conn, 9, "z" * 40, reserve_usd=0.1, guid="gz",
                   now=1001.0)
    assert q.dispatch_intent(conn, small, stage="terra", call_id="cz",
                             now=1001.0)
    assert q.complete(conn, small, actual_usd=0.5, run_path="/r/9",
                      manifest_sha="m", digest="d9", now=1001.0) \
        == "needs_attention"
    assert conn.execute("SELECT COUNT(*) FROM pl_results").fetchone()[0] == 2
    assert q.exposure(conn, "cycle") == 390696 + 100000
    row = conn.execute("SELECT summary FROM pl_results WHERE attempt=?",
                       (small.attempt,)).fetchone()
    assert '"overage_micro": 400000' in row[0]
    # The overage replay stays idempotent.
    assert q.complete(conn, small, actual_usd=0.5, run_path="/r/9",
                      manifest_sha="m", digest="d9", now=1001.0) \
        == "already_complete"
    q.assert_invariants(conn)


def test_complete_stale_keeps_cost_no_effects_no_checkpoint(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _live(conn, 1, "a" * 40)
    claim = _admit(conn, 1, "a" * 40)
    assert q.dispatch_intent(conn, claim, stage="terra", call_id="c1",
                             now=1000.0)
    _live(conn, 1, "b" * 40, now=1001.0)  # head moved mid-flight
    state = q.complete(conn, claim, actual_usd=0.5, run_path="/r/1",
                       manifest_sha="m", digest="d", now=1001.0)
    assert state == "superseded"
    assert conn.execute(
        "SELECT reviewed_head FROM pl_pr_targets").fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM pl_effects").fetchone()[0] == 0
    assert q.exposure(conn, "cycle") == 500000  # real cost preserved
    q.assert_invariants(conn)


def test_unknown_hold_blocks_headroom(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=1.0)
    _live(conn, 1, "a" * 40)
    claim = _admit(conn, 1, "a" * 40, reserve_usd=1.0)
    assert q.dispatch_intent(conn, claim, stage="terra", call_id="c1",
                             now=1000.0)
    assert q.complete(conn, claim, actual_usd=None, run_path="/r/1",
                      manifest_sha="m", digest="d", now=1000.0) \
        == "succeeded"
    assert q.exposure(conn, "cycle") == 1_000_000
    _live(conn, 2, "b" * 40, now=1001.0)
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=2, head="b" * 40, now=1001.0)
    assert q.route(conn, app="gh", guid="g2", now=1001.0) == "routed"
    assert q.claim(conn, owner="w2", reserve_usd=0.1, now=1001.0) is None
    q.assert_invariants(conn)


# -- recovery ----------------------------------------------------------

def test_recover_predispatch_vs_postintent(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _live(conn, 1, "a" * 40)
    c1 = _admit(conn, 1, "a" * 40, owner="w1", now=1000.0)
    out = q.recover(conn, now=1000.0 + 61.0)
    assert out["recovered"] == [{"job": c1.job, "state": "pending"}]
    c2 = q.claim(conn, owner="w2", reserve_usd=1.0, fingerprint="fp",
                 now=1000.0 + 62.0)
    # Recover advanced the fence once (killing the dead owner); the new
    # claim advances it again.
    assert c2 is not None and c2.fence == c1.fence + 2
    with pytest.raises(q.LeaseLost):
        q.heartbeat(conn, c1, now=1000.0 + 63.0)
    assert q.dispatch_intent(conn, c2, stage="terra", call_id="c9",
                             now=1000.0 + 63.0)
    out = q.recover(conn, now=1000.0 + 200.0)
    assert out["recovered"] == [{"job": c2.job,
                                 "state": "needs_attention"}]
    assert q.exposure(conn, "cycle") == 1_000_000  # hold kept
    with pytest.raises(q.LeaseLost):
        q.complete(conn, c2, actual_usd=0.1, run_path="/r/1",
                   manifest_sha="m", digest="d", now=1000.0 + 201.0)
    q.assert_invariants(conn)


# -- effects -------------------------------------------------------------

class FakeRemote:
    def __init__(self):
        self.marks = {}
        self.sends = []
        self.lookups = []
        self.fail_lookup = False
        self.fail_send = False

    def lookup(self, marker):
        self.lookups.append(marker)
        if self.fail_lookup:
            raise TimeoutError("down")
        return self.marks.get(marker)

    def send(self, marker):
        if self.fail_send:
            raise ConnectionError("post lost?")
        rid = f"remote-{len(self.sends)}"
        self.marks[marker] = rid
        self.sends.append(marker)
        return rid


def _effect_job(conn, pr, head, effects, now=1000.0):
    if conn.execute("SELECT COUNT(*) FROM pl_budgets").fetchone()[0] == 0:
        _budgets(conn, cycle=10.0)
    _live(conn, pr, head, now=now)
    claim = _admit(conn, pr, head, guid=f"ge-{pr}", now=now)
    assert q.dispatch_intent(conn, claim, stage="terra",
                             call_id=f"c-{pr}", now=now)
    st = q.complete(conn, claim, actual_usd=0.2, run_path=f"/r/{pr}",
                    manifest_sha="m", digest=f"d-{pr}", effects=effects,
                    now=now)
    assert st == "succeeded"
    return conn.execute("SELECT * FROM pl_effects WHERE job=?",
                        (claim.job,)).fetchall()


def test_effect_comment_happy_path_and_reconcile_after_move(tmp_path):
    conn = _open(tmp_path)
    (eff,) = _effect_job(conn, 1, "a" * 40, [{"channel": "comment"}])
    remote = FakeRemote()
    assert q.deliver_effect(conn, eff["id"], remote, now=1000.0) \
        == "confirmed"
    assert remote.sends != []
    # Second delivery observes the marker, never re-posts.
    sends_before = list(remote.sends)
    assert q.deliver_effect(conn, eff["id"], remote, now=1001.0) \
        == "held_or_finished"
    assert remote.sends == sends_before
    # Historical confirmation stays valid after the head moves.
    _live(conn, 1, "b" * 40, now=1002.0)
    assert q.reconcile_effect(conn, eff["id"], remote,
                              now=1002.0) == "held_or_finished"
    q.assert_invariants(conn)


def test_effect_absent_marker_never_autoresends(tmp_path):
    conn = _open(tmp_path)
    (eff,) = _effect_job(conn, 1, "a" * 40, [{"channel": "comment"}])
    remote = FakeRemote()
    remote.fail_send = True
    assert q.deliver_effect(conn, eff["id"], remote, now=1000.0) == "unknown"
    # Absence after a possible send is not permission: a later lookup
    # still finds nothing, still no POST, still unknown.
    remote.fail_send = False
    assert q.deliver_effect(conn, eff["id"], remote, now=1001.0) \
        == "unknown"
    assert remote.sends == []
    # Read-only reconcile can still confirm a late-arriving send.
    marker = q.effect_job_marker(
        dict(conn.execute("SELECT * FROM pl_effects WHERE id=?",
                          (eff["id"],)).fetchone()))
    remote.marks[marker] = "late-0"
    assert q.reconcile_effect(conn, eff["id"], remote, now=1002.0) \
        == "confirmed"
    q.assert_invariants(conn)


def test_effect_lookup_only_crash_retries_safely(tmp_path):
    conn = _open(tmp_path)
    (eff,) = _effect_job(conn, 1, "a" * 40, [{"channel": "comment"}])
    remote = FakeRemote()
    remote.fail_lookup = True  # fails BEFORE any POST intent
    assert q.deliver_effect(conn, eff["id"], remote, now=1000.0) \
        == "pending"
    row = conn.execute("SELECT state FROM pl_effects WHERE id=?",
                       (eff["id"],)).fetchone()
    assert row[0] == "pending"
    q.assert_invariants(conn)


def test_effect_email_currentness_and_ambiguous_hold(tmp_path):
    conn = _open(tmp_path)
    (eff,) = _effect_job(conn, 1, "a" * 40,
                         [{"channel": "email", "destination": "op@x"}])
    remote = FakeRemote()
    assert q.deliver_effect(conn, eff["id"], remote, now=1000.0) \
        == "confirmed"
    # Email follows the same currentness as comments: a stale target
    # suppresses a pending email instead of sending.
    (eff2,) = _effect_job(conn, 2, "b" * 40,
                          [{"channel": "email", "destination": "op@x"}],
                          now=1001.0)
    _live(conn, 2, "c" * 40, now=1002.0)
    assert q.deliver_effect(conn, eff2["id"], remote, now=1002.0) \
        == "held_or_finished"
    assert conn.execute("SELECT state FROM pl_effects WHERE id=?",
                        (eff2["id"],)).fetchone()[0] == "suppressed"
    # Ambiguous email is never auto-resent.
    (eff3,) = _effect_job(conn, 3, "d" * 40,
                          [{"channel": "email", "destination": "op@x"}],
                          now=1003.0)
    conn.execute("UPDATE pl_effects SET state='unknown',uncertain=1 "
                 "WHERE id=?", (eff3["id"],))
    assert q.deliver_effect(conn, eff3["id"], remote, now=1003.0) \
        == "held_or_finished"
    assert len(remote.sends) == 1
    q.assert_invariants(conn)


def test_effect_paused_publication_holds(tmp_path):
    conn = _open(tmp_path)
    (eff,) = _effect_job(conn, 1, "a" * 40, [{"channel": "comment"}])
    remote = FakeRemote()
    assert q.deliver_effect(conn, eff["id"], remote, now=1000.0,
                            policy=q.Policy(
                                pause_publication=True)) == "held_or_finished"
    assert remote.sends == [] and remote.lookups == []
    assert q.deliver_effect(conn, eff["id"], remote, now=1001.0) \
        == "confirmed"
    q.assert_invariants(conn)


def test_effect_token_theft_and_expiry(tmp_path):
    conn = _open(tmp_path)
    (eff,) = _effect_job(conn, 1, "a" * 40, [{"channel": "comment"}])
    remote = FakeRemote()
    leased = q.begin_effect(conn, eff["id"], now=1000.0)
    assert leased is not None
    with pytest.raises(q.LeaseLost):
        q.finish_effect(conn, {**leased, "token": "forged"}, "confirmed",
                        now=1000.0)
    # Expired pre-POST lease reaps to pending with no charge ambiguity.
    out = q.recover(conn, now=1000.0 + 61.0)
    assert out["effects"] == [{"effect": eff["id"], "state": "pending"}]
    with pytest.raises(q.LeaseLost):
        q.finish_effect(conn, leased, "confirmed", now=1000.0 + 62.0)
    q.assert_invariants(conn)


# -- journal -----------------------------------------------------------

def test_journal_cursor_never_consumes(tmp_path):
    conn = _open(tmp_path)
    pos0 = q.snapshot_position(conn)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, now=1000.0)
    pos1 = q.snapshot_position(conn)
    assert pos1 > pos0
    evs = q.events_after(conn, cursor=pos0)
    assert evs and all(e["seq"] > pos0 for e in evs)
    assert q.events_after(conn, cursor=pos1) == []
    # Re-reading the same cursor returns the same rows: subscribers
    # observe, never acknowledge or consume.
    assert q.events_after(conn, cursor=pos0) == evs
    q.assert_invariants(conn)
