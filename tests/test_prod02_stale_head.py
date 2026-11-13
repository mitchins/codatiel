"""PROD-02: stale-event and reverse-diff hotfix.

A stale queued head (28595526, residue of an earlier interrupted run)
drained backwards after a newer review (d0a38f86), spending inference
and emailing misleading "regressions". The publisher correctly
refused the comment; the pipeline must never have run it.

These tests pin the freshness contract through the real pipeline
(scripted models through the real SDK runtime, real git repos,
fixture transport — runtime smoke tests, not review results):

- older event arriving after a newer completed review -> dropped
  (skipped:stale_head, $0, checkpoint untouched, no email);
- stale queued drain -> dropped and the queue entry cleared, no
  reverse-diff inference;
- head changing while inference runs -> the run seals nothing,
  emails nothing, publishes nothing; spend recorded;
- legitimate rebase/force-push (head == live, divergent history) ->
  still reviews;
- normal incremental review still completes; replays stay blocked.
"""

from __future__ import annotations

import pytest

agents = pytest.importorskip("agents")

from tests.test_prod01_lifecycle import (  # noqa: F401
    _env,
    _ingest,
    _poll_id,
    _runs,
    _terra_factory,
    cfg_store,
    chain,
    pr_event,
)

from codatiel_i2 import event_pipeline as P  # noqa: E402
from codatiel_i2 import event_store as S  # noqa: E402

REPO = "o/r"


class _NotifySpy:
    """Records _notify calls; default stub never sends."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, cfg, run_dir, status, transport, context=None):
        self.calls.append({"run_dir": run_dir, "status": status,
                           "transport": transport})
        return False, "spy: no email sent"


def _no_publication(store) -> None:
    assert store.publications_list() == []


def _live(*heads):
    """Scripted get_live_head: yields each head in order, then repeats."""
    calls = {"n": 0}

    def _fn(repo: str, pr_number: int):
        idx = min(calls["n"], len(heads) - 1)
        calls["n"] += 1
        return heads[idx]

    return _fn


def test_old_event_after_newer_review_is_stale(cfg_store, chain,
                                               monkeypatch):
    cfg, store = cfg_store
    monkeypatch.setattr(P, "get_live_head",
                        _live(chain["B"], chain["B"]))
    spy = _NotifySpy()
    monkeypatch.setattr(P, "_notify", spy)
    kw = {"model_factory": _terra_factory()}
    row = _ingest(cfg, store,
                   pr_event("opened", 23, chain["A"], chain["B"]),
                   "e1", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 1
    # An older event for the superseded head arrives late.
    spy.calls.clear()
    row = _ingest(cfg, store,
                   pr_event("synchronize", 23, chain["A"], chain["A"]),
                   "e-old", **kw)
    assert row["decision"] == "skipped:stale_head", row
    assert row["cost_usd"] == 0.0
    assert len(_runs(cfg)) == 1
    cp = store.checkpoint_get(REPO, 23)
    assert cp["last_reviewed_head"] == chain["B"]
    assert "no email" in row["reason"]
    assert spy.calls == []
    _no_publication(store)


def test_stale_event_dropped_when_gh_unreachable(cfg_store, chain,
                                                monkeypatch):
    """Local ancestry fallback (PROD-02 review D1): a backwards head is
    dropped even when the live head cannot be confirmed; a forward
    head still reviews."""
    cfg, store = cfg_store
    monkeypatch.setattr(P, "get_live_head", lambda repo, pr: None)
    spy = _NotifySpy()
    monkeypatch.setattr(P, "_notify", spy)
    kw = {"model_factory": _terra_factory()}
    row = _ingest(cfg, store,
                   pr_event("opened", 23, chain["A"], chain["B"]),
                   "e1", **kw)
    assert row["decision"] == "completed", row
    # Older head, gh down: dropped by ancestry, $0.
    spy.calls.clear()
    row = _ingest(cfg, store,
                   pr_event("synchronize", 23, chain["A"], chain["A"]),
                   "e-old", **kw)
    assert row["decision"] == "skipped:stale_head", row
    assert row["cost_usd"] == 0.0
    assert spy.calls == []
    # Forward head, gh down: still reviews (availability preserved).
    row = _ingest(cfg, store,
                   pr_event("synchronize", 23, chain["B"], chain["C"]),
                   "e2", **kw)
    assert row["decision"] == "completed", row
    assert len(spy.calls) == 1
    _no_publication(store)


def test_stale_queued_drain_dropped_and_cleared(cfg_store, chain,
                                                 monkeypatch):
    cfg, store = cfg_store
    monkeypatch.setattr(P, "get_live_head",
                        _live(chain["B"], chain["B"], chain["C"],
                              chain["C"], chain["C"]))
    kw = {"model_factory": _terra_factory()}
    row = _ingest(cfg, store,
                   pr_event("opened", 23, chain["A"], chain["B"]),
                   "e1", **kw)
    assert row["decision"] == "completed", row
    # Plant a stale queue residue (older head, never re-pushed).
    store.checkpoint_upsert({
        "repo": REPO, "pr": 23,
        "status": store.checkpoint_get(REPO, 23)["status"],
        "last_reviewed_head": chain["B"],
        "queued_base": chain["B"], "queued_head": chain["A"]})
    # A newer head completes; the drain must drop the stale queue.
    row = _ingest(cfg, store,
                   pr_event("synchronize", 23, chain["B"], chain["C"]),
                   "e2", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 2
    drain = store.delivery_get("e2:drain")
    assert drain is not None, "stale drain must be recorded"
    assert drain["decision"] == "skipped:stale_head", drain
    assert drain["cost_usd"] == 0.0
    cp = store.checkpoint_get(REPO, 23)
    assert cp["last_reviewed_head"] == chain["C"]
    assert not cp["queued_head"]
    assert len(_runs(cfg)) == 2


def test_head_changes_mid_inference_seals_nothing(cfg_store, chain,
                                                  monkeypatch):
    cfg, store = cfg_store
    # Pre-check sees B (proceed); post-check sees C (superseded).
    monkeypatch.setattr(P, "get_live_head",
                        _live(chain["B"], chain["C"]))
    spy = _NotifySpy()
    monkeypatch.setattr(P, "_notify", spy)
    kw = {"model_factory": _terra_factory()}
    spent_before = store.spend_totals()
    row = _ingest(cfg, store,
                   pr_event("opened", 23, chain["A"], chain["B"]),
                   "e1", **kw)
    assert row["decision"] == "skipped:stale_superseded", row
    cp = store.checkpoint_get(REPO, 23)
    assert cp["status"] != "in_progress"
    assert cp["in_progress_head"] == ""
    assert cp["last_reviewed_head"] != chain["B"]
    assert "no email" in row["reason"]
    assert "no checkpoint change" in row["reason"]
    assert spy.calls == []
    _no_publication(store)
    # Spend is real and recorded; the hold is released, not stuck.
    spent_after = store.spend_totals()
    assert spent_after["reserved_usd"] == 0.0
    assert (spent_after["settled_usd"] >= spent_before["settled_usd"])
    assert row["cost_usd"] is not None


def test_legitimate_rebase_still_reviews(cfg_store, chain, tmp_path,
                                         monkeypatch):
    """Genuine history rewrite: after A->B is reviewed, a divergent
    head D (branched from A) becomes live. Equality with live is all
    that matters; ancestry divergence must not block the review."""
    import subprocess

    from tests.test_prod01_lifecycle import _git  # noqa: E402
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    # Fake oracle first: review A->B with live==B throughout.
    monkeypatch.setattr(P, "get_live_head",
                        _live(chain["B"], chain["B"]))
    row = _ingest(cfg, store,
                   pr_event("opened", 23, chain["A"], chain["B"]),
                   "e1", **kw)
    assert row["decision"] == "completed", row
    root = chain["root"]
    _git(root, "checkout", "-qb", "feat", chain["A"])
    (root / "other.py").write_text("Y = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "D-divergent")
    d = _git(root, "rev-parse", "HEAD")
    # Now D is live and diverges from the reviewed B.
    monkeypatch.setattr(P, "get_live_head", _live(d))
    row = _ingest(cfg, store,
                   pr_event("synchronize", 23, chain["B"], d),
                   "e-rebase", **kw)
    assert row["decision"] == "completed", row
    cp = store.checkpoint_get(REPO, 23)
    assert cp["last_reviewed_head"] == d


def test_normal_incremental_and_replay(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    monkeypatch.setattr(P, "get_live_head",
                        _live(chain["B"], chain["B"], chain["C"],
                              chain["C"]))
    kw = {"model_factory": _terra_factory()}
    row = _ingest(cfg, store,
                   pr_event("opened", 23, chain["A"], chain["B"]),
                   "e1", **kw)
    assert row["decision"] == "completed", row
    row = _ingest(cfg, store,
                   pr_event("synchronize", 23, chain["B"], chain["C"]),
                   "e2", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 2
    row = _ingest(cfg, store,
                   pr_event("synchronize", 23, chain["B"], chain["C"]),
                   "e2", **kw)
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == 2


def test_release_claim_restores_prior_status(tmp_path):
    """PROD-02 review D2: supersede release restores draft/reviewed,
    never guesses; nothing stays pinned."""
    st = S.Store(tmp_path / "events.sqlite")
    try:
        st.checkpoint_upsert({"repo": REPO, "pr": 23,
                              "status": "draft",
                              "baseline_head": "b" * 40})
        assert st.claim_for_review(REPO, 23, base="b" * 40,
                                   head="c" * 40) == "claimed"
        cp = st.checkpoint_get(REPO, 23)
        assert cp["status"] == "in_progress"
        prior = st.release_claim(REPO, 23, "superseded in test",
                                 prior_status="draft")
        assert prior == "in_progress"
        cp = st.checkpoint_get(REPO, 23)
        assert cp["status"] == "draft"
        assert cp["in_progress_head"] == ""
        # Unknown prior falls back without pinning.
        assert st.claim_for_review(REPO, 23, base="b" * 40,
                                   head="c" * 40) == "claimed"
        st.release_claim(REPO, 23, "superseded in test")
        cp = st.checkpoint_get(REPO, 23)
        assert cp["status"] in ("reviewed", "deferred")
        assert cp["in_progress_head"] == ""
    finally:
        st.close()


def test_complete_review_clears_satisfied_queue(tmp_path):
    """PROD-02: a queue entry equalling the just-reviewed head is
    dropped, never drained backwards later."""
    st = S.Store(tmp_path / "events.sqlite")
    try:
        st.checkpoint_upsert({"repo": REPO, "pr": 23,
                              "status": "in_progress",
                              "last_reviewed_head": "b" * 40,
                              "queued_base": "b" * 40,
                              "queued_head": "c" * 40})
        out = st.complete_review(
            REPO, 23, base="b" * 40, head="c" * 40,
            diff_sha="d" * 40, files=1, terra_findings=1,
            luna_added=0, cost_usd=0.01, cost_unknown=False,
            run_dir="/tmp/x", result="complete")
        assert out == {"queued": None}
        cp = st.checkpoint_get(REPO, 23)
        assert cp["last_reviewed_head"] == "c" * 40
        assert cp["queued_head"] == ""
    finally:
        st.close()


def test_superseded_run_drains_stranded_live_queue(cfg_store, chain,
                                                   monkeypatch):
    """PROD-02 review: a newer head queued during a superseded run is
    still reviewed (from its queued base, not the unsealed head), so
    no segment is silently skipped and nothing strands."""
    from tests.test_prod01_lifecycle import _git  # noqa: E402
    cfg, store = cfg_store
    root = chain["root"]
    (root / "app.py").write_text("LIMIT = 1\n")
    _git(root, "commit", "-qam", "D")
    d = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(P, "get_live_head",
                        _live(chain["B"], chain["B"], chain["C"],
                              d, d, d))
    spy = _NotifySpy()
    monkeypatch.setattr(P, "_notify", spy)
    kw = {"model_factory": _terra_factory()}
    row = _ingest(cfg, store,
                   pr_event("opened", 23, chain["A"], chain["B"]),
                   "e1", **kw)
    assert row["decision"] == "completed", row
    # A newer head queued while the next run is in flight.
    store.checkpoint_upsert({
        "repo": REPO, "pr": 23,
        "status": store.checkpoint_get(REPO, 23)["status"],
        "last_reviewed_head": chain["B"],
        "queued_base": chain["B"], "queued_head": d})
    row = _ingest(cfg, store,
                   pr_event("synchronize", 23, chain["B"], chain["C"]),
                   "e2", **kw)
    assert row["decision"] == "skipped:stale_superseded", row
    assert row.get("drained") == "completed", row
    cp = store.checkpoint_get(REPO, 23)
    assert cp["last_reviewed_head"] == d
    assert cp["status"] == "reviewed"
    assert len(_runs(cfg)) == 3
