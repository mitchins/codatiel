"""Review-progression invariants (platform contract, no per-commit system).

Codatiel reviews the accumulated changes between the last
successfully reviewed head and the current live head — 1, 2 or 20
commits alike. Failed, interrupted or superseded attempts never
advance the checkpoint. Duplicate deliveries and unchanged heads
never trigger another paid review. Force-pushes review the full
accumulated range without assuming linear ancestry.
"""

from __future__ import annotations

import pytest

from codatiel_i2.platform import db as db_mod
from codatiel_i2.platform import queue as q
from codatiel_i2.platform import schema as schema_mod

REPO = "o/r"
A = "a" * 40
B = "b" * 40
C = "c" * 40


def _open(tmp_path, name="t.sqlite"):
    conn = db_mod.connect(tmp_path / name)
    schema_mod.ensure(conn)
    return conn


def _verify(conn, head, now):
    q.signal(conn, repo=REPO, pr=1, head=head, lifecycle="open", now=now)
    cl = q.reconcile_claim(conn, repo=REPO, pr=1, owner="recon", now=now)
    assert cl is not None
    assert q.reconcile_commit(conn, repo=REPO, pr=1, owner="recon",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head=head, now=now)


def _receive(conn, guid, head, now):
    assert q.accept(conn, app="gh", guid=guid, body=b"{}", repo=REPO,
                    repo_id=1, pr=1, head=head, now=now) == "accepted"
    return q.route(conn, app="gh", guid=guid, now=now)


def _seal(conn, head, reserve=1.0, actual=0.2, now=1000.0, owner="w",
          guid="g"):
    assert _receive(conn, guid, head, now) == "routed"
    claim = q.claim(conn, owner=owner, reserve_usd=reserve, now=now)
    assert claim is not None
    assert q.dispatch_intent(conn, claim, stage="t",
                             call_id=f"c-{guid}", now=now)
    assert q.complete(conn, claim, actual_usd=actual, run_path="/r",
                      manifest_sha="m", digest=f"d-{guid}",
                      now=now) == "succeeded"
    return claim


def test_first_review_spans_base_to_head(tmp_path):
    conn = _open(tmp_path)
    q.set_budget(conn, "cycle", 10.0, now=1000.0)
    _verify(conn, C, now=1000.0)
    assert _receive(conn, "g1", C, now=1000.0) == "routed"
    job = conn.execute("SELECT * FROM pl_jobs").fetchone()
    assert (job["mode"], job["full_head"]) == ("full", C)
    q.assert_invariants(conn)
    conn.close()


def test_many_commits_coalesce_to_one_accumulated_job(tmp_path):
    conn = _open(tmp_path)
    q.set_budget(conn, "cycle", 10.0, now=1000.0)
    _verify(conn, A, now=1000.0)
    _seal(conn, A, guid="gA", now=1000.0)
    # Three pushes, one receipt for the newest head: exactly one job
    # covering last-reviewed A through live C — never per-commit jobs.
    for head, now in ((A, 1001.0), (B, 1002.0), (C, 1003.0)):
        q.signal(conn, repo=REPO, pr=1, head=head, lifecycle="open",
                 now=now)
    _verify(conn, C, now=1004.0)
    assert _receive(conn, "gC", C, now=1004.0) == "routed"
    jobs = conn.execute("SELECT * FROM pl_jobs ORDER BY id").fetchall()
    assert len(jobs) == 2
    assert (jobs[1]["comparison_base"], jobs[1]["full_head"]) == (A, C)
    q.assert_invariants(conn)
    conn.close()


def test_superseded_first_review_next_covers_original_base(tmp_path):
    conn = _open(tmp_path)
    q.set_budget(conn, "cycle", 10.0, now=1000.0)
    _verify(conn, A, now=1000.0)
    _seal(conn, A, guid="gA", now=1000.0)
    # An attempted review of B is superseded mid-flight by C: B never
    # becomes the checkpoint, and the next review covers A -> C.
    q.signal(conn, repo=REPO, pr=1, head=B, lifecycle="open", now=1001.0)
    _verify(conn, B, now=1001.0)
    assert _receive(conn, "gB", B, now=1001.0) == "routed"
    claim_b = q.claim(conn, owner="w", reserve_usd=1.0, now=1001.0)
    assert q.dispatch_intent(conn, claim_b, stage="t", call_id="cB",
                             now=1001.0)
    _verify(conn, C, now=1002.0)
    assert q.complete(conn, claim_b, actual_usd=0.3, run_path="/rB",
                      manifest_sha="m", digest="dB", now=1002.0) \
        == "superseded"
    assert conn.execute(
        "SELECT reviewed_head FROM pl_pr_targets").fetchone()[0] == A
    assert _receive(conn, "gC", C, now=1003.0) == "routed"
    job = conn.execute("SELECT * FROM pl_jobs ORDER BY id DESC LIMIT 1"
                       ).fetchone()
    assert (job["comparison_base"], job["full_head"]) == (A, C)
    assert q.exposure(conn, "cycle") == 300000 + 200000
    q.assert_invariants(conn)
    conn.close()


def test_stale_claim_leaves_base_anchored(tmp_path):
    conn = _open(tmp_path)
    q.set_budget(conn, "cycle", 10.0, now=1000.0)
    _verify(conn, A, now=1000.0)
    _seal(conn, A, guid="gA", now=1000.0)
    # B routes while live, then the head moves before admission: the
    # claim stands down with $0 instead of reviewing a dead range, and
    # C still reviews from A.
    q.signal(conn, repo=REPO, pr=1, head=B, lifecycle="open", now=1001.0)
    _verify(conn, B, now=1001.0)
    assert _receive(conn, "gB", B, now=1001.0) == "routed"
    _verify(conn, C, now=1002.0)
    assert q.claim(conn, owner="w", reserve_usd=1.0, now=1002.0) is None
    assert conn.execute(
        "SELECT state FROM pl_jobs ORDER BY id DESC LIMIT 1").fetchone()[0] \
        == "superseded"
    assert _receive(conn, "gC", C, now=1003.0) == "routed"
    job = conn.execute("SELECT * FROM pl_jobs ORDER BY id DESC LIMIT 1"
                       ).fetchone()
    assert (job["comparison_base"], job["full_head"]) == (A, C)
    assert q.exposure(conn, "cycle") == 200000  # only A's review spent
    q.assert_invariants(conn)
    conn.close()


def test_force_push_reviews_full_accumulated_range(tmp_path):
    conn = _open(tmp_path)
    q.set_budget(conn, "cycle", 10.0, now=1000.0)
    _verify(conn, A, now=1000.0)
    _seal(conn, A, guid="gA", now=1000.0)
    # Divergent head with no linear path through old commits: the job
    # still spans reviewed A to live C (the worker diffs content, the
    # contract never assumes ancestry).
    _verify(conn, C, now=1001.0)
    assert _receive(conn, "gC", C, now=1001.0) == "routed"
    job = conn.execute("SELECT * FROM pl_jobs ORDER BY id DESC LIMIT 1"
                       ).fetchone()
    assert (job["comparison_base"], job["full_head"]) == (A, C)
    q.assert_invariants(conn)
    conn.close()


def test_duplicates_and_unchanged_heads_never_repay(tmp_path):
    conn = _open(tmp_path)
    q.set_budget(conn, "cycle", 10.0, now=1000.0)
    _verify(conn, A, now=1000.0)
    _seal(conn, A, guid="gA", now=1000.0)
    # Same head re-received: no job, no spend.
    assert _receive(conn, "gA2", A, now=1001.0) == "already_reviewed"
    # Same delivery retried: duplicate, original decision stands.
    assert q.accept(conn, app="gh", guid="gA", body=b"{}", repo=REPO,
                    repo_id=1, pr=1, head=A, now=1002.0) == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM pl_jobs").fetchone()[0] == 1
    assert q.exposure(conn, "cycle") == 200000
    q.assert_invariants(conn)
    conn.close()
