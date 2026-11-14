"""Slice 1 gate tests: the behaviors the adversarial review demanded.

Close cancels work (not just effects); lifecycle/suppressed/already-
reviewed/needs-redrive routing; multi-destination effects; pause and
email gates at both effect checkpoints; deferred-budget revival;
top-up bounds; overage adjudication; migration signal history;
artifact traversal; single-transaction snapshots.
"""

from __future__ import annotations

import pytest

from codatiel_i2.platform import artifacts as art_mod
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


def _verify(conn, pr, head, now=1000.0):
    q.signal(conn, repo=REPO, pr=pr, head=head, lifecycle="open", now=now)
    cl = q.reconcile_claim(conn, repo=REPO, pr=pr, owner="recon", now=now)
    assert cl is not None
    assert q.reconcile_commit(conn, repo=REPO, pr=pr, owner="recon",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head=head, now=now)


def _job_for(conn, pr, head, now=1000.0):
    guid = f"g-{pr}-{head[:8]}"
    assert q.accept(conn, app="gh", guid=guid, body=b"{}", repo=REPO,
                    repo_id=1, pr=pr, head=head, now=now) \
        == "accepted"
    return guid


def test_close_cancels_pending_jobs_and_effects(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    guid = _job_for(conn, 1, "a" * 40)
    assert q.route(conn, app="gh", guid=guid, now=1000.0) == "routed"
    # A second, over-cap job waits deferred on another PR.
    _verify(conn, 2, "b" * 40, now=1000.0)
    guid2 = _job_for(conn, 2, "b" * 40)
    assert q.route(conn, app="gh", guid=guid2, now=1000.0) == "routed"
    c1 = q.claim(conn, owner="w1", reserve_usd=10.0, now=1000.0)
    assert c1 is not None and c1.job == 1
    assert q.claim(conn, owner="w2", reserve_usd=1.0, now=1000.0) is None
    assert conn.execute("SELECT state FROM pl_jobs WHERE id=2").fetchone()[0] \
        == "deferred_budget"
    # Close cancels the pending work AND the deferred zombie. A leased
    # job is owned by its worker: close does not yank the lease (only
    # the holder or expiry may), but the worker's next gate observes
    # the closed lifecycle and stands down without spending.
    q.signal(conn, repo=REPO, pr=1, lifecycle="closed", now=1001.0)
    q.signal(conn, repo=REPO, pr=2, lifecycle="closed", now=1001.0)
    states = {r[0]: r[1] for r in conn.execute(
        "SELECT id,state FROM pl_jobs").fetchall()}
    assert states == {1: "leased", 2: "cancelled"}, states
    assert q.dispatch_intent(conn, c1, stage="t", call_id="c1",
                             now=1002.0) is False
    assert conn.execute("SELECT state FROM pl_jobs WHERE id=1").fetchone()[0] \
        == "superseded"
    assert q.exposure(conn, "cycle") == 0  # reservation released
    # Reopen at the same head resumes as new, reviewable work.
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="open",
             now=1002.0)
    guid3 = f"g-1-reopen"
    q.accept(conn, app="gh", guid=guid3, body=b"{}", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1002.0)
    _verify(conn, 1, "a" * 40, now=1002.0)
    assert q.route(conn, app="gh", guid=guid3, now=1002.0) == "routed"
    q.assert_invariants(conn)


def test_route_lifecycle_samehead_terminal(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    # Receipt for a never-verified target waits; for a closed target
    # the receipt is terminally suppressed, never a job.
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, lifecycle="closed",
             now=1000.0)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "pending"
    _verify(conn, 2, "b" * 40)
    q.signal(conn, repo=REPO, pr=2, lifecycle="closed", now=1001.0)
    q.accept(conn, app="gh", guid="g2", body=b"2", repo=REPO,
             repo_id=1, pr=2, head="b" * 40, now=1001.0)
    assert q.route(conn, app="gh", guid="g2", now=1001.0) \
        == "suppressed_lifecycle"
    # Reviewed head re-receipt: no empty head-to-same-head job.
    _verify(conn, 3, "c" * 40)
    guid3 = _job_for(conn, 3, "c" * 40)
    assert q.route(conn, app="gh", guid=guid3, now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c",
                             now=1000.0)
    assert q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                      manifest_sha="m", digest="d", now=1000.0) \
        == "succeeded"
    q.accept(conn, app="gh", guid="g3b", body=b"3", repo=REPO,
             repo_id=1, pr=3, head="c" * 40, now=1001.0)
    assert q.route(conn, app="gh", guid="g3b", now=1001.0) \
        == "already_reviewed"
    assert conn.execute("SELECT COUNT(*) FROM pl_jobs WHERE pr=3"
                        ).fetchone()[0] == 1
    q.assert_invariants(conn)


def test_multi_destination_effects_survive(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    guid = _job_for(conn, 1, "a" * 40)
    assert q.route(conn, app="gh", guid=guid, now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c",
                             now=1000.0)
    state = q.complete(conn, claim, actual_usd=0.2, run_path="/r",
                       manifest_sha="m", digest="d",
                       effects=[{"channel": "email",
                                 "destination": "a@x"},
                                {"channel": "email",
                                 "destination": "b@x"}],
                       now=1000.0)
    assert state == "succeeded"
    dests = sorted(r[0] for r in conn.execute(
        "SELECT destination FROM pl_effects").fetchall())
    assert dests == ["a@x", "b@x"]
    q.assert_invariants(conn)


class _Remote:
    def __init__(self):
        self.marks = {}
        self.sends = []

    def lookup(self, marker):
        return self.marks.get(marker)

    def send(self, marker):
        rid = f"r{len(self.sends)}"
        self.marks[marker] = rid
        self.sends.append(marker)
        return rid


def _succeeded_effect(conn, pr, head, channel, dest="", now=1000.0,
                      effects=None):
    guid = _job_for(conn, pr, head, now=now)
    assert q.route(conn, app="gh", guid=guid, now=now) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=now)
    assert q.dispatch_intent(conn, claim, stage="t",
                             call_id=f"c-{pr}", now=now)
    state = q.complete(conn, claim, actual_usd=0.2, run_path=f"/r{pr}",
                       manifest_sha="m", digest=f"d-{pr}",
                       effects=(effects if effects is not None
                                else [{"channel": channel,
                                       "destination": dest}]),
                       now=now)
    assert state == "succeeded"
    return conn.execute("SELECT * FROM pl_effects WHERE job=?",
                        (claim.job,)).fetchall()


def test_pause_between_begin_and_authorize_holds(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    (eff,) = _succeeded_effect(conn, 1, "a" * 40, "comment")
    leased = q.begin_effect(conn, eff["id"], now=1000.0)
    assert leased is not None
    # The operator pauses while the read-only lookup is in flight: the
    # POST intent gate holds instead of racing the send.
    assert q.authorize_send(conn, leased, now=1000.0,
                            policy=q.Policy(
                                pause_publication=True)) is False
    assert conn.execute("SELECT state FROM pl_effects WHERE id=?",
                        (eff["id"],)).fetchone()[0] == "sending"
    # Unpausing re-authorizes through the normal gate.
    assert q.authorize_send(conn, leased, now=1000.0,
                            policy=q.Policy()) is True
    q.finish_effect(conn, leased, "confirmed", "r0", now=1000.0)
    q.assert_invariants(conn)


def test_email_disabled_holds_email_only(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    (mail,) = _succeeded_effect(conn, 1, "a" * 40, "email", dest="op@x")
    _verify(conn, 2, "b" * 40, now=1001.0)
    (comment,) = _succeeded_effect(conn, 2, "b" * 40, "comment", now=1001.0)
    remote = _Remote()
    pol = q.Policy(email_allowed=False)
    assert q.deliver_effect(conn, mail["id"], remote, now=1001.0,
                            policy=pol) == "held_or_finished"
    assert q.deliver_effect(conn, comment["id"], remote, now=1001.0,
                            policy=pol) == "confirmed"
    assert remote.sends != []
    q.assert_invariants(conn)


def test_deferred_budget_revives_without_new_webhook(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=1.0)
    _verify(conn, 1, "a" * 40)
    guid = _job_for(conn, 1, "a" * 40)
    assert q.route(conn, app="gh", guid=guid, now=1000.0) == "routed"
    _verify(conn, 2, "b" * 40, now=1000.0)
    guid2 = _job_for(conn, 2, "b" * 40)
    assert q.route(conn, app="gh", guid=guid2, now=1000.0) == "routed"
    c1 = q.claim(conn, owner="w1", reserve_usd=1.0, now=1000.0)
    assert c1 is not None
    assert q.claim(conn, owner="w2", reserve_usd=1.0, now=1000.0) is None
    assert conn.execute("SELECT state FROM pl_jobs WHERE id=2").fetchone()[0] \
        == "deferred_budget"
    # The first run settles cheap; the deferred job is re-examined on
    # the next admission pass with no new webhook.
    assert q.dispatch_intent(conn, c1, stage="t", call_id="c1",
                             now=1000.0)
    assert q.complete(conn, c1, actual_usd=0.1, run_path="/r1",
                      manifest_sha="m", digest="d1", now=1000.0) \
        == "succeeded"
    c2 = q.claim(conn, owner="w2", reserve_usd=0.8, now=1001.0)
    assert c2 is not None and c2.job == 2
    q.assert_invariants(conn)


def test_topup_refused_over_cap(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=1.0)
    _verify(conn, 1, "a" * 40)
    guid = _job_for(conn, 1, "a" * 40)
    assert q.route(conn, app="gh", guid=guid, now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=0.9, now=1000.0)
    assert claim is not None
    assert q.top_up_reservation(conn, claim, additional_usd=0.2,
                                now=1000.0) is False
    assert q.top_up_reservation(conn, claim, additional_usd=0.1,
                                now=1000.0) is True
    with pytest.raises(ValueError):
        q.top_up_reservation(conn, claim, additional_usd=0.0, now=1000.0)
    q.assert_invariants(conn)


def test_resolve_unknown_paths(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    guid = _job_for(conn, 1, "a" * 40)
    assert q.route(conn, app="gh", guid=guid, now=1000.0) == "routed"
    claim = q.claim(conn, owner="w", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="t", call_id="c1",
                             now=1000.0)
    q.recover(conn, now=1000.0 + 61.0)
    assert q.resolve_unknown(conn, attempt=claim.attempt, actual_usd=None,
                             note="provider silent", now=1002.0) == "unknown"
    assert q.exposure(conn, "cycle") == 1_000_000  # hold kept
    assert q.resolve_unknown(conn, attempt=claim.attempt, actual_usd=0.4,
                             now=1003.0) == "settled"
    assert q.exposure(conn, "cycle") == 400000
    # Above-reservation truth settles at the bound and records the
    # overage; it never invents the extra settlement.
    _verify(conn, 2, "b" * 40, now=1004.0)
    guid2 = _job_for(conn, 2, "b" * 40)
    assert q.route(conn, app="gh", guid=guid2, now=1004.0) == "routed"
    c2 = q.claim(conn, owner="w", reserve_usd=0.5, now=1004.0)
    assert q.dispatch_intent(conn, c2, stage="t", call_id="c2",
                             now=1004.0)
    q.recover(conn, now=1004.0 + 61.0)
    assert q.resolve_unknown(conn, attempt=c2.attempt, actual_usd=0.9,
                             now=1005.0) == "settled"
    assert q.exposure(conn, "cycle") == 400000 + 500000
    evs = q.events_after(conn, cursor=0, limit=1000)
    assert any(e["kind"] == "adjudicate" and "overage_micro" in e["detail"]
               for e in evs)
    q.assert_invariants(conn)


def test_settings_pause_publication_reachable_and_policy(tmp_path):
    conn = _open(tmp_path)
    doc = {
        "schema_version": "codatiel_i2.operator.v1",
        "repos": {"o/r": True},
        "pause_reviews": False,
        "pause_github_comments": False,
        "notify_to": ["op@example.com"],
        "notify_sender": "c@example.com",
    }
    settings_mod.import_operator_json(conn, doc, actor="op", now=2000.0)
    # Legacy doc has no pause_publication; the key is still settable
    # through the versioned store afterwards.
    rev = settings_mod.update(conn, {"pause_publication": True},
                              actor="op", now=2001.0)
    assert rev == 2
    pol = settings_mod.to_policy(settings_mod.resolve_policy(
        settings_mod.current_revision(conn)["doc"], env_monthly_usd=25.0,
        env_repos=["o/r", "evil/x"]))
    assert pol.pause_publication is True and pol.pause_reviews is False
    assert pol.allowed_repos == ("o/r",)
    # A job for a repo outside the allowlist is parked, never
    # admitted: no spend, no starvation of allowlisted work behind it.
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    guid = _job_for(conn, 1, "a" * 40)
    assert q.route(conn, app="gh", guid=guid, now=2000.0) == "routed"
    q.signal(conn, repo="evil/x", pr=1, head="z" * 40, lifecycle="open",
             now=2000.0)
    cl = q.reconcile_claim(conn, repo="evil/x", pr=1, owner="recon",
                           now=2000.0)
    assert q.reconcile_commit(conn, repo="evil/x", pr=1, owner="recon",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head="z" * 40, now=2000.0)
    q.accept(conn, app="gh", guid="foreign", body=b"f", repo="evil/x",
             repo_id=9, pr=1, head="z" * 40, now=2000.0)
    assert q.route(conn, app="gh", guid="foreign", now=2000.0) == "routed"
    got = q.claim(conn, owner="w", reserve_usd=0.5, now=2000.0,
                  policy=pol)
    assert got is not None and got.job == 1
    assert conn.execute(
        "SELECT state FROM pl_jobs WHERE repo='evil/x'").fetchone()[0] \
        == "pending"
    assert q.exposure(conn, "cycle") == 500000
    conn.close()


def test_migration_seeds_signal_head(tmp_path):
    from codatiel_i2 import event_store as v1_mod
    from codatiel_i2.platform import migrate as migrate_mod
    v1p = tmp_path / "v1.sqlite"
    v1 = v1_mod.Store(v1p)
    v1.meta_set("enabled_at", "2026-11-14T00:00:00Z")
    v1.checkpoint_upsert({"repo": REPO, "pr": 1, "status": "reviewed",
                          "last_reviewed_head": "h" * 40,
                          "last_reviewed_base": "b" * 40})
    v1.close()
    conn = _open(tmp_path, "p.sqlite")
    migrate_mod.migrate(conn, v1_db_path=str(v1p), operator_doc={},
                        budgets_usd={"cycle": 25.0}, now=2000.0)
    tgt = conn.execute("SELECT * FROM pl_pr_targets").fetchone()
    assert tgt["last_signal_head"] == "h" * 40
    # First post-cutover signal at the unchanged reviewed head:
    # recorded, no generation churn, nothing scheduled.
    r = q.signal(conn, repo=REPO, pr=1, head="h" * 40, lifecycle="open",
                 now=2001.0)
    assert (r["generation"], r["scheduled"]) == (1, False)
    q.assert_invariants(conn)
    conn.close()


def test_artifact_run_id_traversal_rejected(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    for bad in ("../evil", "a/b", "", ".", "..", "x\x00y"):
        with pytest.raises(ValueError):
            art_mod.stage(staging, bad, {"f": b"x"})
    st = art_mod.stage(staging, "ok-run_1", {"f": b"x"})
    assert art_mod.validate(st)
    q.assert_invariants(_open(tmp_path, "q.sqlite"))


def test_snapshot_single_read_transaction(tmp_path):
    conn = _open(tmp_path)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, now=1000.0)
    snap = q.snapshot(conn, cursor=0)
    assert snap["position"] >= 1
    assert [e["seq"] for e in snap["events"]] == list(
        range(1, snap["position"] + 1))[:len(snap["events"])]
    later = q.snapshot(conn, cursor=snap["position"])
    assert later["events"] == [] and later["position"] == snap["position"]
    conn.close()


def test_finish_pending_after_post_refused(tmp_path):
    conn = _open(tmp_path)
    _budgets(conn, cycle=10.0)
    _verify(conn, 1, "a" * 40)
    (eff,) = _succeeded_effect(conn, 1, "a" * 40, "comment")
    leased = q.begin_effect(conn, eff["id"], now=1000.0)
    assert leased is not None
    assert q.authorize_send(conn, leased, now=1000.0) is True
    with pytest.raises(q.Conflict):
        q.finish_effect(conn, leased, "pending", now=1000.0)
    q.finish_effect(conn, leased, "unknown", now=1000.0)
    q.assert_invariants(conn)
