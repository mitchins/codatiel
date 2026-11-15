"""BUDGET-RETRY-01: bounded re-drive of pure budget refusals.

A ceiling-skipped (repo, PR, head) that is re-ingested under the same
deterministic delivery ID may be reviewed once spend budget is
available again. Bounds (all pinned here):

- same (repo, PR, head) as the refused row;
- refusal carried no spend (cost None or $0);
- no review has since covered the head;
- the live PR head positively confirms currency (unverifiable fails
  closed);
- at most BUDGET_RETRY_MAX_ATTEMPTS re-drives (counter survives
  re-refusals);
- every attempt re-checks all spend caps before any inference.

No paid inference: scripted models through the real runtime, real git
repos, fixture transports. A raising model factory proves the
no-inference paths.
"""

from __future__ import annotations

import json

import pytest

agents = pytest.importorskip("agents")

from tests.test_event_pipeline import (  # noqa: F401
    _ingest,
    _runs,
    _terra_factory,
    cfg_store,
    chain,
    pr_event,
)

from codatiel_i2 import event_pipeline as P  # noqa: E402
from codatiel_i2 import event_store as S  # noqa: E402

REPO = "o/r"


def _refusing_factory():
    def _mk(role: str):
        raise AssertionError("no inference is authorized on this path")
    return _mk


def _live(head: str | None):
    return lambda repo, pr: head


def _ceiling_refusal(cfg, store, monkeypatch, number: int, base: str,
                     head: str, did: str):
    """Drive one pure skipped:ceiling refusal (no inference)."""
    cfg.spend_cap_usd = 0.5  # reserve ($1) no longer fits
    monkeypatch.setattr(P, "get_live_head", _live(head))
    row = _ingest(cfg, store, pr_event("opened", number, base, head), did,
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:ceiling", row
    assert row["cost_usd"] == 0.0, row
    return row


# -- happy path ---------------------------------------------------------------
def test_ceiling_refusal_retried_when_budget_opens(cfg_store, chain,
                                                   monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 1, a, b, "br0")
    assert _runs(cfg) == []
    # Budget opens; the same head re-ingested under the same delivery
    # ID reviews exactly once.
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, pr_event("synchronize", 1, a, b), "br0",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 1
    final = store.delivery_get("br0")
    assert final["decision"] == "completed", final
    detail = json.loads(final["detail"] or "{}")
    assert detail.get("budget_retry_attempts") == 1, detail
    assert detail.get("budget_retry_from_decision") == "skipped:ceiling"
    cp = store.checkpoint_get(REPO, 1)
    assert cp["last_reviewed_head"] == b
    # A further duplicate is replay-blocked: no second review, no new
    # delivery row, dedup intact.
    row = _ingest(cfg, store, pr_event("synchronize", 1, a, b), "br0",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == 1
    n = store.cx.execute(
        "SELECT COUNT(*) FROM deliveries WHERE delivery_id='br0'").fetchone()
    assert n[0] == 1


# -- bounds -------------------------------------------------------------------
def test_no_retry_while_budget_closed_and_attempts_bounded(cfg_store, chain,
                                                           monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 2, a, b, "br1")
    # Budget still closed: each re-ingest re-drives (live, same head)
    # but the spend cap refuses again with no inference; the attempt
    # counter advances exactly once per cycle.
    for n in range(1, P.BUDGET_RETRY_MAX_ATTEMPTS + 1):
        row = _ingest(cfg, store, pr_event("synchronize", 2, a, b), "br1",
                      model_factory=_refusing_factory())
        assert row["decision"] == "skipped:ceiling", (n, row)
        assert _runs(cfg) == []
        got = store.delivery_get("br1")
        detail = json.loads(got["detail"] or "{}")
        assert detail.get("budget_retry_attempts") == n, (n, detail)
    # Attempts exhausted: further duplicates are replay-blocked and the
    # counter never moves again.
    row = _ingest(cfg, store, pr_event("synchronize", 2, a, b), "br1",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    got = store.delivery_get("br1")
    assert "ceiling" in (json.loads(got["detail"] or "{}").get(
        "original_decision") or ""), got
    assert P._prior_retry_attempts(got) == P.BUDGET_RETRY_MAX_ATTEMPTS
    assert _runs(cfg) == []


def test_no_retry_when_head_moved(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b, c = chain["A"], chain["B"], chain["C"]
    _ceiling_refusal(cfg, store, monkeypatch, 3, a, b, "br2")
    # The refused head is no longer live: replay-blocked, no inference.
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(c))
    row = _ingest(cfg, store, pr_event("synchronize", 3, a, b), "br2",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert _runs(cfg) == []
    # The replay wrapper preserves the ceiling refusal as original —
    # and the retry still fires later if the refused head is live
    # again (wrapper unwrap path) with budget open.
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, pr_event("synchronize", 3, a, b), "br2",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 1


def test_no_retry_when_head_already_reviewed(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 4, a, b, "br3")
    # The head gets reviewed through a different delivery (explicit
    # re-drive shape): the stale ceiling row must never cause a second
    # review of the same head.
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, pr_event("synchronize", 4, a, b), "br3-explicit",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    row = _ingest(cfg, store, pr_event("synchronize", 4, a, b), "br3",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == 1


def test_no_retry_when_live_head_unverifiable(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 5, a, b, "br4")
    # gh unreachable: fail closed, replay-blocked, no inference — the
    # refusal is retried on a later tick, not on a guess.
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(None))
    row = _ingest(cfg, store, pr_event("synchronize", 5, a, b), "br4",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert _runs(cfg) == []
    assert P._prior_retry_attempts(store.delivery_get("br4")) == 0


def test_no_retry_when_refusal_carried_spend(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    # A spend-carrying ceiling row is not a pure refusal (defensive:
    # the pipeline never writes one, but the guard must hold).
    store.delivery_record(
        delivery_id="br5", event_type="pull_request", action="synchronize",
        repo=REPO, pr=6, head_sha=b, decision="skipped:ceiling",
        reason="forged spend-carrying refusal", cost_usd=5.0,
        detail={"operator_gate": True})
    monkeypatch.setattr(P, "get_live_head", _live(b))
    ev, err = P.normalize_event(pr_event("synchronize", 6, a, b), "br5")
    assert err == "", err
    row = P.process_event(cfg=cfg, store=store, ev=ev,
                          model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert _runs(cfg) == []
    assert store.delivery_get("br5")["cost_usd"] == 5.0


def test_no_retry_for_non_ceiling_terminal_rows(cfg_store, chain,
                                                monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, pr_event("opened", 7, a, b), "br6",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    # Completed rows are never re-driven, even with the head live.
    row = _ingest(cfg, store, pr_event("synchronize", 7, a, b), "br6",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == 1


# -- atomic reclaim: only the winner may start work ---------------------------
# CodeRabbit BUDGET-RETRY-01: the store must report whether THIS call
# won the reclaim; a loser must fall back to replay, never process.


def test_reclaim_reports_winner_and_loser(cfg_store, chain):
    cfg, store = cfg_store
    b = chain["B"]
    store.delivery_record(
        delivery_id="br8", event_type="pull_request", action="synchronize",
        repo=REPO, pr=8, head_sha=b, decision="skipped:ceiling",
        reason="cap", cost_usd=0.0, detail={})
    row, won = store.delivery_reopen_budget(
        "br8", attempt=1, max_attempts=5)
    assert won is True
    assert row["decision"] == "in_progress", row
    # A stale second claimant loses: the row is untouched and the loss
    # is reported (no silent double-drive of the same delivery).
    row2, won2 = store.delivery_reopen_budget(
        "br8", attempt=1, max_attempts=5)
    assert won2 is False
    assert row2["decision"] == "in_progress", row2
    assert P._prior_retry_attempts(row2) == 1


def test_pipeline_loser_falls_back_to_replay(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 9, a, b, "br9")
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(b))
    # Interleaving: this caller reads the ceiling row, then another
    # worker wins the atomic reclaim first.
    stale = store.delivery_get("br9")
    row, won = store.delivery_reopen_budget(
        "br9", attempt=1, max_attempts=P.BUDGET_RETRY_MAX_ATTEMPTS)
    assert won is True
    real_get = store.delivery_get
    calls = {"n": 0}

    def _flaky_get(did):
        # Only the loser's initial prior read observes the stale
        # ceiling snapshot; every later read sees current state.
        calls["n"] += 1
        if did == "br9" and calls["n"] == 1:
            return dict(stale)
        return real_get(did)

    monkeypatch.setattr(store, "delivery_get", _flaky_get)
    # The loser's reclaim loses: it must not process as new work and
    # must not touch the winner's active claim — the current row is
    # reported unchanged with no inference.
    monkeypatch.setattr(
        store, "delivery_reopen_budget",
        lambda *a, **k: (dict(stale), False))
    ev, err = P.normalize_event(pr_event("synchronize", 9, a, b), "br9")
    assert err == "", err
    out = P.process_event(cfg=cfg, store=store, ev=ev,
                          model_factory=_refusing_factory())
    assert out["decision"] == "in_progress", out
    assert store.delivery_get("br9")["decision"] == "in_progress"
    assert _runs(cfg) == []


def test_replay_mark_never_overwrites_active_redrive(cfg_store, chain):
    cfg, store = cfg_store
    b = chain["B"]
    store.delivery_record(
        delivery_id="br10", event_type="pull_request",
        action="synchronize", repo=REPO, pr=10, head_sha=b,
        decision="skipped:ceiling", reason="cap", cost_usd=0.0,
        detail={})
    row, won = store.delivery_reopen_budget(
        "br10", attempt=1, max_attempts=5)
    assert won is True
    # A late duplicate arriving after the reclaim must not clobber the
    # active in_progress claim back to skipped:replay (that rollback
    # would restore the old attempt counter and break the bound).
    after = store.delivery_mark_replay(
        "br10", {"decision": "skipped:ceiling", "reason": "cap",
                 "detail": "{}"})
    assert after["decision"] == "in_progress", after
    assert P._prior_retry_attempts(after) == 1


# -- completed history beats the checkpoint pointer ---------------------------
# CodeRabbit BUDGET-RETRY-01: a head already successfully reviewed must
# never become eligible again, even after a later review advances the
# checkpoint pointer and the live head resets to the old head.


def test_reviewed_head_never_retried_after_pointer_advances(
        cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b, c = chain["A"], chain["B"], chain["C"]
    _ceiling_refusal(cfg, store, monkeypatch, 11, a, b, "br11")
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(b))
    # Head B is reviewed through another delivery, then C advances the
    # checkpoint pointer past B.
    row = _ingest(cfg, store, pr_event("synchronize", 11, a, b),
                  "br11-cover", model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    monkeypatch.setattr(P, "get_live_head", _live(c))
    row = _ingest(cfg, store, pr_event("synchronize", 11, b, c),
                  "br11-cover2", model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    assert store.checkpoint_get(REPO, 11)["last_reviewed_head"] == c
    # The live head resets to the old refused head B: the stale ceiling
    # row must stay a blocked replay — B was already reviewed.
    n_runs = len(_runs(cfg))
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, pr_event("synchronize", 11, a, b), "br11",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == n_runs
def test_reopen_budget_is_compare_and_set(cfg_store, chain):
    cfg, store = cfg_store
    b = chain["B"]
    store.delivery_record(
        delivery_id="br7", event_type="pull_request", action="synchronize",
        repo=REPO, pr=8, head_sha=b, decision="skipped:ceiling",
        reason="cap", cost_usd=0.0, detail={})
    # Stale attempt (recorded counter is 0, caller claims 3): the row
    # is left untouched and the loss is reported so the caller falls
    # back to replay.
    row, won = store.delivery_reopen_budget(
        "br7", attempt=3, max_attempts=5)
    assert won is False
    assert row["decision"] == "skipped:ceiling", row
    # Fresh attempt reclaims exactly once.
    row, won = store.delivery_reopen_budget(
        "br7", attempt=1, max_attempts=5)
    assert won is True
    assert row["decision"] == "in_progress", row
    detail = json.loads(row["detail"] or "{}")
    assert detail["budget_retry_attempts"] == 1
    assert detail["budget_retry_from_decision"] == "skipped:ceiling"
    # A second reclaim with the same attempt loses the race: the row
    # keeps the winner's claim and reports the loss.
    row, won = store.delivery_reopen_budget(
        "br7", attempt=1, max_attempts=5)
    assert won is False
    assert row["decision"] == "in_progress", row
    assert json.loads(row["detail"] or "{}")[
        "budget_retry_attempts"] == 1
