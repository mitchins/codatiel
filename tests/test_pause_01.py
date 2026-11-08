"""PAUSE-01: pause enforcement at queued dispatch, pre-inference
revalidation, and email delivery (deterministic, no inference).

A paused repo must see: no dispatch of already-queued rows, no
revalidation-driven state changes, no email (fresh or redelivery),
with pair state preserved for resume. Resume, restart, and superseded
heads complete the matrix.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from codatiel_i2 import watcher as W
from codatiel_i2 import watcher_eligibility as E
from codatiel_i2 import watcher_manager as M
from codatiel_i2 import watcher_store as S

ACTIVATED = "2026-10-24T00:00:00Z"
OLD = "2026-10-01T00:00:00Z"
NEW = "2026-10-24T12:00:00Z"


def future_ts(seconds_ahead: int = 300) -> str:
    """CreatedAt safely after the store's activation instant, so the
    activation gate (createdAt > activated_at) passes regardless of
    wall-clock drift. Deterministic clock math, no inference."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + seconds_ahead))


def raw_pr(number=1, base="aa" * 20, head="bb" * 20, created=NEW,
           state="OPEN", draft=False, merged="",
           url=None, repo="o/r"):
    return {"number": number, "title": "t", "headRefOid": head,
            "baseRefOid": base, "headRefName": "f", "baseRefName": "m",
            "url": url or f"https://github.com/{repo}/pull/{number}",
            "createdAt": created, "updatedAt": created, "state": state,
            "isDraft": draft, "mergedAt": merged}


def make_mgr(tmp_path: Path, **kw):
    cfg = M.ManagerConfig(
        repos=None, recipients=["ops@example.com"],
        sender="codatiel@example.com", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", spend_total_cap_usd=5.0,
        spend_seed_usd=0.0, spend_daily_cap_usd=2.0,
        per_poll_cap=kw.pop("per_poll_cap", 3),
        settle_quiet_s=kw.pop("settle_quiet_s", 0),
        settle_max_deferral_s=kw.pop("settle_max_deferral_s", 0))
    for k, v in kw.items():
        setattr(cfg, k, v)
    store = S.Store(tmp_path / "watch.sqlite")
    mgr = M.Manager(cfg, store)
    return cfg, store, mgr


class Harness:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
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

    def fake_run(self, name: str) -> str:
        d = self.tmp / "runs" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps(
            {"run_id": name, "repo": "o/r", "pr_number": 1,
             "pr_url": "https://github.com/o/r/pull/1",
             "base_sha": "aa" * 20, "head_sha": "bb" * 20,
             "config": {"model": "luna"}}))
        (d / "metrics.json").write_text(json.dumps(
            {"status": "complete", "model": "luna:x", "tool_calls": 5,
             "instructions_id": "canonical-minimal-0.1@y",
             "spend": {"usd_estimate": 0.05,
                       "accounting_status": "CONFIRMED_USAGE"}}))
        (d / "shadow_result.json").write_text(json.dumps(
            {"status": "complete", "advisory": [],
             "silence": {"summary": "quiet"}}))
        (d / "findings.jsonl").write_text("")
        (d / "evidence.jsonl").write_text("")
        return str(d)

    def review(self, backend: str, vpr, ceiling: float):
        self.reviews += 1
        d = self.fake_run(f"{backend}-{self.reviews}")
        return {"status": "complete", "stage": "done",
                "run_dir": d, "model": backend}

    def sender(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return dict(self.receipt)


def wire(mgr: M.Manager, h: Harness):
    mgr.run_review = h.review  # type: ignore[method-assign]
    mgr.sender = h.sender  # type: ignore[method-assign]
    mgr.list_repos = h.list_repos  # type: ignore[method-assign]
    mgr.discover = h.discover  # type: ignore[method-assign]
    mgr.fetch = h.fetch  # type: ignore[method-assign]
    return mgr


def pause(store: S.Store, repo: str):
    store.cx.execute("INSERT OR IGNORE INTO repos_paused(repo) VALUES(?)",
                     (repo,))
    store.cx.commit()


def resume(store: S.Store, repo: str):
    store.cx.execute("DELETE FROM repos_paused WHERE repo=?", (repo,))
    store.cx.commit()


def deliveries(store: S.Store) -> int:
    return int(store.cx.execute(
        "SELECT COUNT(*) FROM deliveries").fetchone()[0])


def test_pause_after_queue_blocks_dispatch_and_delivery(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    store.activate()
    h = Harness(tmp_path)
    created = future_ts()
    h.prs = {"o/paused": [raw_pr(repo="o/paused", created=created)],
             "o/active": [raw_pr(repo="o/active", created=created)]}
    wire(mgr, h)
    for repo in ("o/paused", "o/active"):
        store.upsert_pair(repo, 1, "aa" * 20, "bb" * 20,
                          pr_url=f"https://github.com/{repo}/pull/1",
                          pr_created_at=NEW)
        store.set_status(repo, 1, "aa" * 20, "bb" * 20, "queued")
    pause(store, "o/paused")
    out = mgr.cycle()
    # Active repo dispatched + emailed; paused repo untouched.
    assert h.reviews == 1, out
    assert len(h.sent) == 1
    assert "o/active" in h.sent[0]["subject"]
    assert store.get_pair("o/paused", 1, "aa" * 20,
                          "bb" * 20)["status"] == "queued"
    assert store.get_pair("o/active", 1, "aa" * 20,
                          "bb" * 20)["status"] == "delivered"
    assert deliveries(store) == 1


def test_direct_dispatch_and_revalidate_fail_closed_on_pause(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    store.activate()
    h = Harness(tmp_path)
    h.prs = {"o/paused": [raw_pr(repo="o/paused")]}
    wire(mgr, h)
    store.upsert_pair("o/paused", 1, "aa" * 20, "bb" * 20,
                      pr_url="https://github.com/o/paused/pull/1",
                      pr_created_at=NEW)
    store.set_status("o/paused", 1, "aa" * 20, "bb" * 20, "queued")
    pause(store, "o/paused")
    rec = mgr.dispatch("o/paused", 1, "aa" * 20, "bb" * 20, mgr.day())
    assert rec.get("suppressed") == "repo paused"
    assert h.reviews == 0
    assert store.get_pair("o/paused", 1, "aa" * 20,
                          "bb" * 20)["status"] == "queued"
    live, reason = mgr.revalidate("o/paused", 1, "aa" * 20, "bb" * 20)
    assert live is None and reason == "repo paused"


def test_pause_during_review_holds_email_then_resume_delivers(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    store.activate()
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr()]}
    wire(mgr, h)
    run_dir = h.fake_run("done-1")
    store.upsert_pair("o/r", 1, "aa" * 20, "bb" * 20,
                      pr_url="https://github.com/o/r/pull/1",
                      pr_created_at=NEW)
    store.set_status("o/r", 1, "aa" * 20, "bb" * 20, "complete",
                     run_dir=run_dir, model="luna")
    vpr, _ = E.validate_pr_metadata("o/r", raw_pr())
    assert vpr is not None
    # Pause lands after inference, before delivery.
    pause(store, "o/r")
    res = mgr.maybe_email(vpr, {"status": "complete", "run_dir": run_dir},
                          False)
    assert res == {"emailed": False, "delivery": "repo-paused"}
    assert h.sent == []
    assert deliveries(store) == 0
    assert store.get_pair("o/r", 1, "aa" * 20,
                          "bb" * 20)["status"] == "complete"
    # Resume delivers the same bytes with no new inference.
    resume(store, "o/r")
    res = mgr.maybe_email(vpr, {"status": "complete", "run_dir": run_dir},
                          False)
    assert res == {"emailed": True}
    assert len(h.sent) == 1
    assert store.get_pair("o/r", 1, "aa" * 20,
                          "bb" * 20)["status"] == "delivered"


def test_redelivery_sweep_skips_paused_repo(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    store.activate()
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=future_ts())]}
    wire(mgr, h)
    run_dir = h.fake_run("done-2")
    store.upsert_pair("o/r", 1, "aa" * 20, "bb" * 20,
                      pr_url="https://github.com/o/r/pull/1",
                      pr_created_at=NEW)
    store.set_status("o/r", 1, "aa" * 20, "bb" * 20, "complete",
                     run_dir=run_dir, model="luna")
    pause(store, "o/r")
    out = mgr.cycle()
    assert h.reviews == 0
    assert h.sent == []
    assert deliveries(store) == 0
    assert out["emails"] == []
    assert store.get_pair("o/r", 1, "aa" * 20,
                          "bb" * 20)["status"] == "complete"
    resume(store, "o/r")
    out = mgr.cycle()
    assert h.reviews == 0  # same bytes, never re-inference
    assert len(h.sent) == 1
    assert store.get_pair("o/r", 1, "aa" * 20,
                          "bb" * 20)["status"] == "delivered"


def test_restart_under_pause_stays_quiet_then_resume_dispatches(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    store.activate()
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=future_ts())]}
    wire(mgr, h)
    store.upsert_pair("o/r", 1, "aa" * 20, "bb" * 20,
                      pr_url="https://github.com/o/r/pull/1",
                      pr_created_at=NEW)
    store.set_status("o/r", 1, "aa" * 20, "bb" * 20, "queued")
    pause(store, "o/r")
    mgr.cycle()
    assert h.reviews == 0 and h.sent == []
    # Restart: new instance on the same store, recovery included.
    mgr.check_in()
    store.release_lease(mgr.instance_id)
    mgr2 = M.Manager(mgr.cfg, store)
    wire(mgr2, h)
    mgr2.check_in()
    mgr2.recover()
    mgr2.cycle()
    assert h.reviews == 0
    assert h.sent == []
    assert deliveries(store) == 0
    assert store.get_pair("o/r", 1, "aa" * 20, "bb" * 20)["status"] in (
        "queued", "eligible")
    resume(store, "o/r")
    mgr2.cycle()
    assert h.reviews == 1
    assert len(h.sent) == 1


def test_superseded_head_never_dispatches_no_inference(tmp_path):
    # Latest-head check: a queued row whose head moved must never
    # dispatch. Revalidation fails closed (retry, no inference, no
    # delivery) while the current head proceeds through settle.
    _cfg, store, mgr = make_mgr(
        tmp_path, settle_quiet_s=3600, settle_max_deferral_s=3600)
    store.activate()
    h = Harness(tmp_path)
    old, new = "bb" * 20, "cc" * 20
    created = future_ts()
    h.prs = {"o/r": [raw_pr(head=old, created=created)]}
    wire(mgr, h)
    store.upsert_pair("o/r", 1, "aa" * 20, old,
                      pr_url="https://github.com/o/r/pull/1",
                      pr_created_at=created)
    store.set_status("o/r", 1, "aa" * 20, old, "queued")
    pause(store, "o/r")
    h.prs = {"o/r": [raw_pr(head=new, created=created)]}  # head advances
    mgr.cycle()
    assert h.reviews == 0
    assert store.get_pair("o/r", 1, "aa" * 20, old)["status"] == "queued"
    resume(store, "o/r")
    out = mgr.cycle()
    assert h.reviews == 0, out
    assert h.sent == []
    assert deliveries(store) == 0
    cur = store.get_pair("o/r", 1, "aa" * 20, old)
    # Stale head held without inference or delivery; the live head was
    # admitted separately and is settling (quiet period unexpired).
    assert cur["status"] in ("eligible", "suppressed")
    assert "stale" in (cur.get("note") or "")
    live = store.get_pair("o/r", 1, "aa" * 20, new)
    assert live is not None and live["status"] == "eligible"
    # Second cycle: still quiet, still no inference.
    mgr.cycle()
    assert h.reviews == 0
    assert h.sent == []
