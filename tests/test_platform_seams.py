"""Slice 1 seam tests: route-vs-reconcile ordering, retargets, dirty
gates, redrive, migration carries, and validation guards.

Each test pins one reviewer finding so the seam Slice 2 depends on
cannot silently regress.
"""

from __future__ import annotations

import pytest

from codatiel_i2.platform import db as db_mod
from codatiel_i2.platform import queue as q
from codatiel_i2.platform import schema as schema_mod
from codatiel_i2.platform import settings as settings_mod

REPO = "o/r"


def _open(tmp_path, name="t.sqlite"):
    conn = db_mod.connect(tmp_path / name)
    schema_mod.ensure(conn)
    return conn


def _budgets(conn, **caps):
    for scope, cap in caps.items():
        q.set_budget(conn, scope, cap, now=1000.0)


def _verify(conn, pr, head, base="", now=1000.0):
    q.signal(conn, repo=REPO, pr=pr, head=head, lifecycle="open", now=now)
    cl = q.reconcile_claim(conn, repo=REPO, pr=pr, owner="recon", now=now)
    assert cl is not None
    assert q.reconcile_commit(conn, repo=REPO, pr=pr, owner="recon",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head=head, base=base,
                              now=now)


def test_route_newer_head_waits_for_verification(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    # Force-push to C; the webhook is accepted and announced, but the
    # reconcile commit has not run yet: the receipt waits, it is NOT
    # burned as stale.
    q.signal(conn, repo=REPO, pr=1, head="c" * 40, lifecycle="open",
             now=1001.0)
    q.accept(conn, app="gh", guid="gC", body=b"C", repo=REPO,
             repo_id=1, pr=1, head="c" * 40, now=1001.0)
    assert q.route(conn, app="gh", guid="gC", now=1001.0) == "pending"
    # Verification lands; the worker re-routes waiting receipts and
    # the job appears with no redelivery.
    _verify(conn, 1, "c" * 40, now=1002.0)
    assert q.reroute(conn, repo=REPO, pr=1, now=1002.0) == {"gC": "routed"}
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1002.0)
    assert claim is not None
    assert conn.execute("SELECT full_head FROM pl_jobs").fetchone()[0] \
        == "c" * 40
    q.assert_invariants(conn)
    conn.close()


def test_route_two_witness_stale(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "b" * 40)
    # A receipt for an older head arrives AFTER a newer head was both
    # announced and verified: two witnesses agree it is superseded.
    q.signal(conn, repo=REPO, pr=1, head="c" * 40, lifecycle="open",
             now=1001.0)
    _verify(conn, 1, "c" * 40, now=1002.0)
    q.accept(conn, app="gh", guid="gOld", body=b"O", repo=REPO,
             repo_id=1, pr=1, head="b" * 40, now=1003.0)
    assert q.route(conn, app="gh", guid="gOld", now=1003.0) == "stale"
    q.assert_invariants(conn)
    conn.close()


def test_base_retarget_reroots_full(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "h" * 40, base="b1" * 10)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="h" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d", now=1000.0) \
        == "succeeded"
    # Retarget to B2 at the same head: a new generation, and the
    # receipt is NOT already-reviewed — it routes a full re-root
    # against the newly verified base.
    q.signal(conn, repo=REPO, pr=1, head="h" * 40, lifecycle="open",
             base_retarget=True, now=1001.0)
    _verify(conn, 1, "h" * 40, base="b2" * 10, now=1001.0)
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=1, head="h" * 40, now=1001.0)
    assert q.route(conn, app="gh", guid="g2", now=1001.0) == "routed"
    job = conn.execute("SELECT mode,full_base,comparison_base FROM pl_jobs "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    assert (job["mode"], job["full_base"], job["comparison_base"]) == (
        "full", "b2" * 10, "h" * 40)
    q.assert_invariants(conn)
    conn.close()


def test_stale_dispatch_after_committed_intent_holds_unknown(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="terra", call_id="c1",
                             now=1000.0)
    # Head moves before the second paid stage: the committed intent
    # may already be on the wire, so the reservation is held unknown —
    # never zero-settled — and the job holds for the operator.
    _verify(conn, 1, "b" * 40, now=1001.0)
    assert q.dispatch_intent(conn, claim, stage="luna", call_id="c2",
                             now=1001.0) is False
    assert conn.execute("SELECT state FROM pl_jobs").fetchone()[0] \
        == "needs_attention"
    assert conn.execute("SELECT state FROM pl_attempts").fetchone()[0] \
        == "unknown"
    assert q.exposure(conn, "cycle") == 1_000_000
    q.assert_invariants(conn)
    conn.close()


def test_complete_dirty_gate_and_reverify(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    # A duplicate-signal arrives mid-flight (same head, new dirty):
    # sealing blind is forbidden; the job parks.
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
             now=1001.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d", now=1001.0) \
        == "awaiting_verification"
    # Evidence and money are kept; only the seal waits: one result,
    # settled cost, no checkpoint advance, no effects.
    assert conn.execute("SELECT COUNT(*) FROM pl_results").fetchone()[0] == 1
    assert q.exposure(conn, "cycle") == 200000
    assert conn.execute(
        "SELECT reviewed_head FROM pl_pr_targets").fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM pl_effects").fetchone()[0] == 0
    # Reconcile shows duplicates only: resume with the same claim,
    # same evidence, and the effect intents (created by the sealing
    # call) — then seal.
    assert q.reverify(conn, claim, now=1002.0) is True
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d",
                      effects=[{"channel": "comment"}], now=1002.0) \
        == "succeeded"
    assert conn.execute("SELECT COUNT(*) FROM pl_results").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pl_effects").fetchone()[0] == 1
    q.assert_invariants(conn)
    conn.close()


def test_reverify_stands_down_on_moved_head(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
             now=1001.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d", now=1001.0) \
        == "awaiting_verification"
    _verify(conn, 1, "b" * 40, now=1002.0)
    assert q.reverify(conn, claim, now=1002.0) is False
    # Stood down with cost kept: the parked completion already settled
    # the known actual, so the run seals as superseded — no effects,
    # no checkpoint, no operator hold (nothing left to adjudicate).
    assert conn.execute("SELECT state FROM pl_jobs").fetchone()[0] \
        == "superseded"
    assert q.exposure(conn, "cycle") == 200000
    q.assert_invariants(conn)
    conn.close()


def test_explicit_redrive_after_terminal_intent(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d", now=1000.0) \
        == "succeeded"
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1001.0)
    # Terminal intent on re-receipt: needs_redrive, never automatic.
    assert q.route(conn, app="gh", guid="g2", now=1001.0) == "already_reviewed"
    assert conn.execute("SELECT COUNT(*) FROM pl_jobs").fetchone()[0] == 1
    # The explicit operator re-drive opens a new intent generation.
    assert q.route(conn, app="gh", guid="g2", rerun_generation=1,
                   now=1001.0) == "routed"
    assert conn.execute("SELECT COUNT(*) FROM pl_jobs").fetchone()[0] == 2
    q.assert_invariants(conn)
    conn.close()


def test_redrive_after_unresolved_terminal_intent(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=0.1, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    # Over-reserve parks the job in needs_attention with no checkpoint.
    assert q.complete(conn, claim, actual_usd=0.5, run_path="/r",
                      manifest_sha="m", digest="d", now=1000.0) \
        == "needs_attention"
    # Same-head re-receipt hits the terminal intent: needs_redrive,
    # never automatic, never already-reviewed (nothing sealed).
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1001.0)
    assert q.route(conn, app="gh", guid="g2", now=1001.0) == "needs_redrive"
    assert q.route(conn, app="gh", guid="g2", rerun_generation=1,
                   now=1001.0) == "routed"
    q.assert_invariants(conn)
    conn.close()


def test_migration_unknown_holds_carry_into_day(tmp_path):
    from codatiel_i2 import event_store as v1_mod
    from codatiel_i2.platform import migrate as migrate_mod
    v1p = tmp_path / "v1.sqlite"
    v1 = v1_mod.Store(v1p)
    v1.meta_set("enabled_at", "2026-11-14T00:00:00Z")
    v1.spend_set("settled_usd", 5.0)
    v1.spend_set("reserved_usd", 2.0)
    v1.spend_set("unknown_holds", 2.0)
    v1.close()
    conn = _open(tmp_path, "p.sqlite")
    summary = migrate_mod.migrate(conn, v1_db_path=str(v1p),
                                  operator_doc={},
                                  budgets_usd={"cycle": 50.0, "day": 5.0},
                                  unknown_hold_usd=1.0, now=2000.0)
    assert summary["unknown_holds"] == 2
    assert summary["day_opening_micro"] == 4_000_000  # 2.0 + 2 x 1.0
    assert q.exposure(conn, "day") == 4_000_000
    assert q.exposure(conn, "cycle") == 7_000_000
    q.assert_invariants(conn)
    conn.close()


def test_migration_grandfathered_anchor_is_already_reviewed(tmp_path):
    from codatiel_i2 import event_store as v1_mod
    from codatiel_i2.platform import migrate as migrate_mod
    v1p = tmp_path / "v1.sqlite"
    v1 = v1_mod.Store(v1p)
    v1.meta_set("enabled_at", "2026-11-14T00:00:00Z")
    v1.checkpoint_upsert({"repo": REPO, "pr": 7, "status": "grandfathered",
                          "baseline_head": "g" * 40,
                          "last_reviewed_base": "b" * 40})
    v1.close()
    conn = _open(tmp_path, "p.sqlite")
    migrate_mod.migrate(conn, v1_db_path=str(v1p), operator_doc={},
                        budgets_usd={"cycle": 25.0}, now=2000.0)
    tgt = conn.execute("SELECT * FROM pl_pr_targets").fetchone()
    assert tgt["reviewed_head"] == "g" * 40
    # Slow sweep verifies the untouched baseline: a receipt at that
    # head is already-reviewed, never a paid empty self-review.
    _verify(conn, 7, "g" * 40, now=2001.0)
    q.accept(conn, app="gh", guid="gg", body=b"g", repo=REPO,
             repo_id=1, pr=7, head="g" * 40, now=2001.0)
    assert q.route(conn, app="gh", guid="gg", now=2001.0) \
        == "already_reviewed"
    q.assert_invariants(conn)
    conn.close()


def test_guards_lifecycle_repo_empty_commit(tmp_path):
    conn = _open(tmp_path)
    with pytest.raises(ValueError):
        q.signal(conn, repo=REPO, pr=1, head="a" * 40,
                 lifecycle="bogus", now=1000.0)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
             repo_id=5, app="myapp", now=1000.0)
    with pytest.raises(q.Conflict):
        q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
                 repo_id=6, app="myapp", now=1001.0)
    with pytest.raises(q.Conflict):
        q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
                 repo_id=5, app="otherapp", now=1001.0)
    cl = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r", now=1001.0)
    assert q.reconcile_commit(conn, repo=REPO, pr=1, owner="r",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head="", now=1001.0) \
        is False
    assert conn.execute("SELECT live_head FROM pl_pr_targets").fetchone()[0] \
        == ""
    q.assert_invariants(conn)
    conn.close()


def test_snapshot_refuses_active_transaction(tmp_path):
    conn = _open(tmp_path)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, now=1000.0)
    snap = q.snapshot(conn, cursor=0)
    assert snap["position"] >= 1
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError):
            q.snapshot(conn, cursor=0)
    finally:
        conn.execute("ROLLBACK")
    conn.close()


def test_authorize_idempotent_and_marker_lookup(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d",
                      effects=[{"channel": "email", "destination": "a@x"},
                               {"channel": "email", "destination": "b@x"}],
                      now=1000.0) == "succeeded"
    effs = conn.execute("SELECT * FROM pl_effects ORDER BY id").fetchall()
    assert len(effs) == 2
    leased = q.begin_effect(conn, effs[0]["id"], now=1000.0)
    assert leased is not None
    assert q.authorize_send(conn, leased, now=1000.0) is True
    assert q.authorize_send(conn, leased, now=1000.0) is True
    n = conn.execute("SELECT COUNT(*) FROM pl_effect_attempts WHERE "
                     "phase='post_intent'").fetchone()[0]
    assert n == 1  # no duplicate intent row
    q.finish_effect(conn, leased, "confirmed", "r0", now=1000.0)
    q.assert_invariants(conn)
    conn.close()


def test_retarget_receipt_waits_for_base_verification(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "h" * 40, base="b1" * 10)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="h" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d", now=1000.0) \
        == "succeeded"
    # Retarget announced (base B2) but not yet verified: the receipt
    # waits, it is NOT burned as already-reviewed.
    q.signal(conn, repo=REPO, pr=1, head="h" * 40, base="b2" * 10,
             lifecycle="open", base_retarget=True, now=1001.0)
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=1, head="h" * 40, now=1001.0)
    assert q.route(conn, app="gh", guid="g2", now=1001.0) == "pending"
    # Base verified (head unchanged): generation bumps on the base
    # change alone, reroute schedules the full re-root.
    _verify(conn, 1, "h" * 40, base="b2" * 10, now=1002.0)
    assert q.reroute(conn, repo=REPO, pr=1, now=1002.0) == {"g2": "routed"}
    job = conn.execute("SELECT mode,full_base FROM pl_jobs "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    assert (job["mode"], job["full_base"]) == ("full", "b2" * 10)
    q.assert_invariants(conn)
    conn.close()


def test_confirming_commit_preserves_newer_announcement(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.signal(conn, repo=REPO, pr=1, head="c" * 40, lifecycle="open",
             now=1001.0)
    q.accept(conn, app="gh", guid="gC", body=b"C", repo=REPO,
             repo_id=1, pr=1, head="c" * 40, now=1001.0)
    # A confirming read of the OLD live head must not clobber the
    # newer announcement witness: the C receipt waits, not burns.
    cl = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r", now=1001.0)
    assert q.reconcile_commit(conn, repo=REPO, pr=1, owner="r",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head="a" * 40, now=1001.0)
    assert q.route(conn, app="gh", guid="gC", now=1001.0) == "pending"
    assert conn.execute(
        "SELECT last_signal_head FROM pl_pr_targets").fetchone()[0] \
        == "c" * 40
    q.assert_invariants(conn)
    conn.close()


def test_commit_omitted_base_preserves_verified_base(tmp_path):
    conn = _open(tmp_path)
    _verify(conn, 1, "h" * 40, base="b1" * 10)
    cl = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r", now=1001.0)
    assert q.reconcile_commit(conn, repo=REPO, pr=1, owner="r",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head="h" * 40, now=1001.0)
    assert conn.execute(
        "SELECT live_base FROM pl_pr_targets").fetchone()[0] == "b1" * 10
    q.assert_invariants(conn)
    conn.close()


def test_reconcile_release_and_reentrant_claim(tmp_path):
    conn = _open(tmp_path)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, now=1000.0)
    c1 = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r", now=1000.0)
    assert c1 is not None
    assert q.reconcile_claim(conn, repo=REPO, pr=1, owner="other",
                             now=1000.0) is None
    # Same owner re-claims without waiting out the TTL.
    c2 = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r", now=1001.0)
    assert c2 is not None and c2["fence"] == c1["fence"]
    # A discarded commit releases explicitly; a stranger cannot.
    q.signal(conn, repo=REPO, pr=1, head="b" * 40, now=1002.0)
    assert q.reconcile_release(conn, repo=REPO, pr=1, owner="other",
                               fence=c2["fence"], now=1002.0) is False
    assert q.reconcile_release(conn, repo=REPO, pr=1, owner="r",
                               fence=c2["fence"], now=1002.0) is True
    c3 = q.reconcile_claim(conn, repo=REPO, pr=1, owner="other",
                           now=1002.0)
    assert c3 is not None and c3["fence"] == c2["fence"] + 1
    q.assert_invariants(conn)
    conn.close()


def test_unknown_replay_needs_adjudication(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    assert q.complete(conn, claim, actual_usd=None, run_path="/r",
                      manifest_sha="m", digest="d", now=1000.0) \
        == "succeeded"
    # Replaying the unknown completion with a value is NOT a quiet
    # settlement: it must go through adjudication.
    with pytest.raises(q.Conflict):
        q.complete(conn, claim, actual_usd=0.4, run_path="/r",
                   manifest_sha="m", digest="d", now=1001.0)
    assert q.complete(conn, claim, actual_usd=None, run_path="/r",
                      manifest_sha="m", digest="d", now=1001.0) \
        == "already_complete"
    assert q.exposure(conn, "cycle") == 1_000_000
    q.assert_invariants(conn)
    conn.close()


def test_recover_settled_parked_keeps_money(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
             now=1001.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d", now=1001.0) \
        == "awaiting_verification"
    # Worker dies while parked: sealed money is never re-held.
    out = q.recover(conn, now=1000.0 + 61.0)
    assert out["recovered"] == [{"job": claim.job, "state": "superseded"}]
    assert q.exposure(conn, "cycle") == 200000
    q.assert_invariants(conn)
    conn.close()


def test_parked_job_blocks_second_authority(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w1", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
             now=1001.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d", now=1001.0) \
        == "awaiting_verification"
    # A newer head verifies while the old run is parked: the new job
    # waits — one review authority per PR, no invariant trip.
    _verify(conn, 1, "b" * 40, now=1002.0)
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=1, head="b" * 40, now=1002.0)
    assert q.route(conn, app="gh", guid="g2", now=1002.0) == "routed"
    assert q.claim(conn, owner="w2", reserve_usd=1.0, now=1002.0) is None
    q.assert_invariants(conn)
    conn.close()


def test_migration_queued_delta_becomes_routable_receipt(tmp_path):
    from codatiel_i2 import event_store as v1_mod
    from codatiel_i2.platform import migrate as migrate_mod
    v1p = tmp_path / "v1.sqlite"
    v1 = v1_mod.Store(v1p)
    v1.meta_set("enabled_at", "2026-11-14T00:00:00Z")
    v1.checkpoint_upsert({"repo": REPO, "pr": 5, "status": "reviewed",
                          "last_reviewed_head": "q" * 40,
                          "queued_head": "w" * 40,
                          "queued_base": "q" * 40})
    v1.close()
    conn = _open(tmp_path, "p.sqlite")
    summary = migrate_mod.migrate(conn, v1_db_path=str(v1p),
                                  operator_doc={},
                                  budgets_usd={"cycle": 25.0}, now=2000.0)
    assert summary["synthetic_queued_receipts"] == 1
    pend = q.pending_receipts(conn, REPO, 5)
    assert len(pend) == 1 and pend[0]["head"] == "w" * 40
    # Unverified: waits, never burns.
    assert q.route(conn, app="migration", guid=pend[0]["guid"],
                   now=2001.0) == "pending"
    # Sweep verifies the queued head: the delta routes as its own job.
    _verify(conn, 5, "w" * 40, now=2002.0)
    assert q.reroute(conn, repo=REPO, pr=5, now=2002.0) \
        == {pend[0]["guid"]: "routed"}
    job = conn.execute("SELECT mode,full_base,comparison_base,full_head "
                       "FROM pl_jobs").fetchone()
    assert (job["mode"], job["full_head"]) == ("incremental", "w" * 40)
    assert job["comparison_base"] == "q" * 40  # anchor kept, not skipped
    q.assert_invariants(conn)
    conn.close()


def test_migration_keeps_newest_posted_marker(tmp_path):
    from codatiel_i2 import event_store as v1_mod
    from codatiel_i2.platform import migrate as migrate_mod
    v1p = tmp_path / "v1.sqlite"
    v1 = v1_mod.Store(v1p)
    v1.meta_set("enabled_at", "2026-11-14T00:00:00Z")
    v1.publication_record(repo=REPO, pr=1, head_sha="h" * 40,
                          run_id="old", comment_id="c-old",
                          outcome="posted", reason="ok", now=1000.0)
    v1.publication_record(repo=REPO, pr=1, head_sha="h" * 40,
                          run_id="new", comment_id="c-new",
                          outcome="posted", reason="repost", now=2000.0)
    v1.close()
    conn = _open(tmp_path, "p.sqlite")
    migrate_mod.migrate(conn, v1_db_path=str(v1p), operator_doc={},
                        budgets_usd={"cycle": 25.0}, now=3000.0)
    mark = q.find_posted_marker(conn, REPO, 1, "h" * 40)
    assert mark is not None and mark["comment_id"] == "c-new"
    assert q.find_posted_marker(conn, REPO, 9, "z" * 40) is None
    conn.close()


def test_draft_transition_cancels_pending_work(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    # Draft-removal of readiness invalidates pending work like close.
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="draft",
             now=1001.0)
    assert conn.execute("SELECT state FROM pl_jobs").fetchone()[0] \
        == "cancelled"
    q.assert_invariants(conn)
    conn.close()


def test_allowlist_empty_effective_set_denies_all(tmp_path):
    conn = _open(tmp_path)
    doc = {
        "schema_version": "codatiel_i2.operator.v1",
        "repos": {"o/r": False},
        "notify_to": ["op@example.com"],
        "notify_sender": "c@example.com",
    }
    settings_mod.import_operator_json(conn, doc, actor="op", now=2000.0)
    pol = settings_mod.to_policy(settings_mod.resolve_policy(
        settings_mod.current_revision(conn)["doc"], env_monthly_usd=25.0,
        env_repos=["o/r"]))
    assert pol.allowed_repos == ()
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=2000.0)
    assert q.route(conn, app="gh", guid="g1", now=2000.0) == "routed"
    assert q.claim(conn, owner="w", reserve_usd=0.5, now=2000.0,
                   policy=pol) is None
    assert conn.execute("SELECT state FROM pl_jobs").fetchone()[0] \
        == "pending"
    q.assert_invariants(conn)
    conn.close()


def test_accept_synthetic_is_idempotent(tmp_path):
    conn = _open(tmp_path)
    assert q.accept_synthetic(conn, repo=REPO, pr=1, head="h" * 40,
                              now=1000.0) == "accepted"
    assert q.accept_synthetic(conn, repo=REPO, pr=1, head="h" * 40,
                              now=1001.0) == "duplicate"
    assert len(q.pending_receipts(conn, REPO, 1)) == 1
    conn.close()
