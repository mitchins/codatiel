"""STABILIZE-02 SETTLE-01: review settled heads, not every push.

Deterministic scheduler tests with a fake clock: quiet period, maximum
deferral, restart persistence, head moves, lifecycle/pause cancellation,
explicit override, and per-SHA idempotency. No inference.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest

from codatiel_i2 import watcher_manager as M
from codatiel_i2 import watcher_store as S

T0 = 1_730_000_000.0
OLD = "2026-10-01T00:00:00Z"
NEW = "2026-10-24T12:00:00Z"


def raw_pr(number=1, base="aa" * 20, head="bb" * 20, created=OLD,
           state="OPEN", draft=False, merged="",
           url=None, repo="o/r"):
    return {"number": number, "title": "t", "headRefOid": head,
            "baseRefOid": base, "headRefName": "f", "baseRefName": "m",
            "url": url or f"https://github.com/{repo}/pull/{number}",
            "createdAt": created, "updatedAt": created, "state": state,
            "isDraft": draft, "mergedAt": merged}


class Harness:
    """Fake GitHub + fake review worker + fake mailer (mirrors the
    v0.1.1 acceptance harness)."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.last_run_dir: str = ""
        self.prs: dict[str, list[dict]] = {}
        self.reviews = 0
        self.sent: list[dict] = []
        self.receipt: dict = {"succeeded": 1, "failed": 0,
                              "email_id": "e1"}

    def list_repos(self):
        return sorted(self.prs.keys()), ""

    def discover(self, repo: str, limit: int = 100):
        return list(self.prs.get(repo, [])), ""

    def fetch(self, repo: str, number: int):
        for raw in self.prs.get(repo, []):
            if int(raw["number"]) == number:
                return dict(raw), ""
        return None, "not found"

    def review(self, backend: str, vpr, ceiling: float):
        import json as _json
        self.reviews += 1
        d = self.tmp / "runs" / f"settle-{self.reviews}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(_json.dumps(
            {"run_id": f"settle-{self.reviews}"}))
        (d / "metrics.json").write_text(_json.dumps(
            {"status": "complete", "model": "luna:x",
             "spend": {"usd_estimate": 0.05,
                       "accounting_status": "CONFIRMED_USAGE"}}))
        (d / "shadow_result.json").write_text(_json.dumps(
            {"status": "complete", "advisory": []}))
        (d / "findings.jsonl").write_text("")
        (d / "evidence.jsonl").write_text("")
        self.last_run_dir = str(d)
        return {"status": "complete", "stage": "done",
                "run_dir": self.last_run_dir, "model": backend}

    def sender(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body,
                          "run_dir": self.last_run_dir})
        return dict(self.receipt)


def wire(mgr: M.Manager, h: Harness):
    mgr.run_review = h.review  # type: ignore[method-assign]
    mgr.sender = h.sender  # type: ignore[method-assign]
    mgr.list_repos = h.list_repos  # type: ignore[method-assign]
    mgr.discover = h.discover  # type: ignore[method-assign]
    mgr.fetch = h.fetch  # type: ignore[method-assign]
    return mgr


def make_settle_mgr(tmp_path: Path, **kw) -> tuple[M.ManagerConfig,
                                                   S.Store, M.Manager,
                                                   list]:
    now = kw.pop("now", [T0])
    cfg = M.ManagerConfig(
        repos=None, recipients=["ops@example.com"],
        sender="codatiel@example.com", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", spend_total_cap_usd=5.0,
        spend_seed_usd=0.0, spend_daily_cap_usd=2.0,
        per_poll_cap=kw.pop("per_poll_cap", 3),
        settle_quiet_s=kw.pop("settle_quiet_s", 900.0),
        settle_max_deferral_s=kw.pop("settle_max_deferral_s", 3600.0))
    for k, v in kw.items():
        setattr(cfg, k, v)
    store = S.Store(tmp_path / "watch.sqlite")
    mgr = M.Manager(cfg, store, now=lambda: now[0])
    return cfg, store, mgr, now


def activate(store: S.Store) -> None:
    # Fixed boundary before NEW so eligibility never depends on the wall
    # clock (the v0.1.1 acceptance dates are wall-clock sensitive).
    store.meta_set("activated_at", "2026-10-24T00:00:00Z")


def heads(h: Harness, *shas: str):
    h.prs = {"o/r": [raw_pr(number=1, head=s, created=NEW) for s in shas[-1:]]}


# ------------------------------------------------------------- gate: one

def test_single_push_settles_then_dispatches(tmp_path):
    _cfg, store, mgr, now = make_settle_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    heads(h, "bb" * 20)
    wire(mgr, h)
    out = mgr.cycle()
    assert h.reviews == 0  # quiet period: no same-cycle dispatch
    assert out["admitted"] == 1
    pend = store.get_pending("o/r", 1)
    assert pend is not None and pend["head"] == "bb" * 20
    assert pend["first_seen"] == pend["last_change"] == T0
    now[0] += 899
    mgr.cycle()
    assert h.reviews == 0
    now[0] += 1
    mgr.cycle()
    assert h.reviews == 1
    assert store.get_pending("o/r", 1) is None  # terminal: episode ends
    assert store.get_pair("o/r", 1, "aa" * 20, "bb" * 20)["status"] == "delivered"


# ----------------------------------------------------------- gate: burst

def test_burst_pushes_dispatch_latest_head_once(tmp_path):
    _cfg, store, mgr, now = make_settle_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    wire(mgr, h)
    ha, hb, hc = "b1" * 20, "b2" * 20, "b3" * 20
    heads(h, ha)
    mgr.cycle()
    now[0] += 300
    heads(h, hb)
    mgr.cycle()
    now[0] += 300
    heads(h, hc)
    mgr.cycle()
    assert h.reviews == 0
    pend = store.get_pending("o/r", 1)
    assert pend["head"] == hc  # followed each move
    assert pend["first_seen"] == T0  # new pushes never reset it
    assert pend["last_change"] == T0 + 600
    now[0] += 900  # quiet after the last push
    mgr.cycle()
    assert h.reviews == 1
    pair = store.get_pair("o/r", 1, "aa" * 20, hc)
    assert pair is not None and pair["status"] == "delivered"


# -------------------------------------------------- gate: max deferral

def test_max_deferral_fires_no_starvation(tmp_path):
    _cfg, store, mgr, now = make_settle_mgr(
        tmp_path, settle_quiet_s=900.0, settle_max_deferral_s=1500.0)
    activate(store)
    h = Harness(tmp_path)
    wire(mgr, h)
    # A push every 5 minutes: the quiet period never elapses on its own.
    pushed = [f"{i:040d}" for i in range(5)]
    heads(h, pushed[0])
    mgr.cycle()
    for sha in pushed[1:]:
        now[0] += 300
        heads(h, sha)
        mgr.cycle()
    assert h.reviews == 0
    # first_seen + 1500 fires even though the quiet never elapsed.
    now[0] = T0 + 1500
    heads(h, pushed[-1])
    out = mgr.cycle()
    assert h.reviews == 1
    pair = store.get_pair("o/r", 1, "aa" * 20, pushed[-1])
    assert pair is not None and pair["status"] == "delivered"
    assert out.get("settle_waiting") == 0


# ---------------------------------------------------- gate: restart

def test_pending_survives_daemon_restart(tmp_path):
    cfg, store, mgr, now = make_settle_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    heads(h, "bb" * 20)
    wire(mgr, h)
    mgr.cycle()
    assert h.reviews == 0
    mgr.check_in()
    store.release_lease(mgr.instance_id)
    # New manager on the same store: the episode continues, first_seen
    # intact, no duplicate pending row.
    now[0] += 900
    mgr2 = M.Manager(cfg, store, now=lambda: now[0])
    wire(mgr2, h)
    mgr2.check_in()
    mgr2.cycle()
    assert h.reviews == 1
    assert len(store.list_pending()) == 0


# -------------------------------------- gate: head moves at dispatch

def test_head_moving_at_dispatch_time_is_not_dispatched(tmp_path):
    _cfg, store, mgr, now = make_settle_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    wire(mgr, h)
    ha, hb = "b1" * 20, "b2" * 20
    heads(h, ha)
    mgr.cycle()
    now[0] += 900  # quiet elapsed for ha...
    # ...but a push lands between discovery (still listing ha) and the
    # pre-dispatch fetch: the sweep sees hb, not ha.
    hb_raw = raw_pr(number=1, head=hb, created=NEW)
    mgr.fetch = lambda repo, number: (dict(hb_raw), "")
    out = mgr.cycle()
    assert h.reviews == 0  # ha never dispatched
    assert out.get("settle_moved") == 1
    pend = store.get_pending("o/r", 1)
    assert pend["head"] == hb and pend["first_seen"] == T0
    # Settle the race: discovery and fetch agree on hb from here on.
    heads(h, hb)
    mgr.fetch = h.fetch
    now[0] += 900
    mgr.cycle()
    assert h.reviews == 1
    assert store.get_pair("o/r", 1, "aa" * 20, hb)["status"] == "delivered"
    assert store.get_pair("o/r", 1, "aa" * 20, ha)["status"] == "eligible"


# ------------------------------------------- gate: close/merge cancels

def test_close_while_pending_cancels(tmp_path):
    _cfg, store, mgr, now = make_settle_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    heads(h, "bb" * 20)
    wire(mgr, h)
    mgr.cycle()
    assert store.get_pending("o/r", 1) is not None
    h.prs = {"o/r": [dict(raw_pr(number=1, head="bb" * 20, created=NEW),
                           state="CLOSED")]}
    out = mgr.cycle()
    assert h.reviews == 0
    assert store.get_pending("o/r", 1) is None
    assert out.get("settle_cancelled") == 1


# ---------------------------------------------- gate: pause cancels

def test_pause_while_pending_cancels(tmp_path):
    _cfg, store, mgr, now = make_settle_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    heads(h, "bb" * 20)
    wire(mgr, h)
    mgr.cycle()
    assert store.get_pending("o/r", 1) is not None
    store.cx.execute("INSERT INTO repos_paused(repo) VALUES('o/r')")
    store.cx.commit()
    out = mgr.cycle()
    assert h.reviews == 0
    assert store.get_pending("o/r", 1) is None
    assert out.get("settle_cancelled") == 1


# ------------------------------------------------ gate: the override

def test_override_dispatches_immediately_and_logs_who(tmp_path):
    _cfg, store, mgr, now = make_settle_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    heads(h, "bb" * 20)
    wire(mgr, h)
    mgr.cycle()
    assert h.reviews == 0
    row = store.set_pending_override("o/r", 1, by="ops-oncall",
                                     reason="security fix", now=now[0])
    assert row["override_by"] == "ops-oncall"
    out = mgr.cycle()
    assert h.reviews == 1
    assert out.get("settle_overrides") == [
        {"pr": "o/r#1", "by": "ops-oncall", "reason": "security fix"}]
    assert store.get_pending("o/r", 1) is None


def test_override_without_pending_row_refuses(tmp_path):
    _cfg, store, _mgr, now = make_settle_mgr(tmp_path)
    with pytest.raises(S.StoreError):
        store.set_pending_override("o/r", 9, by="ops", reason="x",
                                   now=now[0])
    with pytest.raises(S.StoreError):
        store.set_pending_override("o/r", 1, by="", reason="x",
                                   now=now[0])


# --------------------------------- gate: no duplicate for settled head

def test_settled_head_is_never_reviewed_again(tmp_path):
    _cfg, store, mgr, now = make_settle_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    heads(h, "bb" * 20)
    wire(mgr, h)
    mgr.cycle()
    now[0] += 900
    mgr.cycle()
    assert h.reviews == 1
    # The same head stays open: further cycles review nothing and queue
    # nothing pending.
    for _ in range(3):
        now[0] += 900
        mgr.cycle()
    assert h.reviews == 1
    assert store.get_pending("o/r", 1) is None


# ------------------------------------------------- store unit tests

def test_pending_upsert_keeps_first_seen_across_heads(tmp_path):
    store = S.Store(tmp_path / "watch.sqlite")
    r1 = store.upsert_pending("o/r", 1, "aa" * 20, "b1" * 20, now=1000.0)
    assert (r1["first_seen"], r1["last_change"]) == (1000.0, 1000.0)
    r2 = store.upsert_pending("o/r", 1, "aa" * 20, "b1" * 20, now=1500.0)
    assert (r2["first_seen"], r2["last_change"]) == (1000.0, 1000.0)
    r3 = store.upsert_pending("o/r", 1, "aa" * 20, "b2" * 20, now=1800.0)
    assert (r3["first_seen"], r3["last_change"]) == (1000.0, 1800.0)
    assert [p["pr"] for p in store.list_pending()] == [1]
    store.clear_pending("o/r", 1)
    assert store.get_pending("o/r", 1) is None


def test_pending_consume_override_keeps_episode(tmp_path):
    store = S.Store(tmp_path / "watch.sqlite")
    store.upsert_pending("o/r", 1, "aa" * 20, "b1" * 20, now=1000.0)
    store.set_pending_override("o/r", 1, by="ops", reason="y", now=1100.0)
    store.consume_pending_override("o/r", 1, now=1200.0)
    row = store.get_pending("o/r", 1)
    assert row["override_by"] == "" and row["first_seen"] == 1000.0
