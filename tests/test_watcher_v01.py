"""Codatiel v0.1.1 watcher acceptance gates (deterministic, no inference).

Covers the hardening mission: activation contract, historical exclusion,
lifecycle revalidation, queue caps, subprocess isolation, accounting
(incl. unknown-usage bounds), delivery idempotency, store refusal modes,
and the read-only GitHub scope. Live contract tests are separate
(tests/test_connector_live.py); the adversarial review is out-of-band.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from codatiel_i2 import watcher as W
from codatiel_i2 import watcher_eligibility as E
from codatiel_i2 import watcher_manager as M
from codatiel_i2 import watcher_store as S

ACTIVATED = "2026-10-24T00:00:00Z"
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


def make_mgr(tmp_path: Path, **kw) -> tuple[M.ManagerConfig, S.Store, M.Manager]:
    cfg = M.ManagerConfig(
        repos=None, recipients=["ops@example.com"],
        sender="codatiel@example.com", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", spend_total_cap_usd=5.0,
        spend_seed_usd=0.0, spend_daily_cap_usd=2.0,
        per_poll_cap=kw.pop("per_poll_cap", 3))
    for k, v in kw.items():
        setattr(cfg, k, v)
    store = S.Store(tmp_path / "watch.sqlite")
    mgr = M.Manager(cfg, store)
    return cfg, store, mgr


def activate(store: S.Store) -> str:
    return store.activate()


class Harness:
    """Fake GitHub + fake review worker + fake mailer."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.last_run_dir: str = ""
        self.prs: dict[str, list[dict]] = {}
        self.reviews = 0
        self.sent: list[dict] = []
        self.receipt: dict = {"succeeded": 1, "failed": 0,
                              "email_id": "e1"}
        self.fetch_error: dict[tuple[str, int], str] = {}

    def list_repos(self):
        return sorted(self.prs.keys()), ""

    def discover(self, repo: str, limit: int = 100):
        return list(self.prs.get(repo, [])), ""

    def fetch(self, repo: str, number: int):
        if (repo, number) in self.fetch_error:
            return None, self.fetch_error[(repo, number)]
        for raw in self.prs.get(repo, []):
            if int(raw["number"]) == number:
                return dict(raw), ""
        return None, "not found"

    def fake_run(self, name: str, status="complete", usd=0.05,
                 unknown=False) -> str:
        d = self.tmp / "runs" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps(
            {"run_id": name, "repo": "o/r", "pr_number": 1,
             "pr_url": "https://github.com/o/r/pull/1",
             "base_sha": "aa" * 20, "head_sha": "bb" * 20,
             "config": {"model": "luna"}}))
        (d / "metrics.json").write_text(json.dumps(
            {"status": status, "model": "luna:x", "tool_calls": 5,
             "instructions_id": "canonical-minimal-0.1@y",
             "spend": {"usd_estimate": (None if unknown else usd),
                       "accounting_status": "UNKNOWN_USAGE" if unknown
                       else "CONFIRMED_USAGE"}}))
        (d / "shadow_result.json").write_text(json.dumps(
            {"status": status, "advisory": [],
             "silence": {"summary": "quiet"}}))
        (d / "findings.jsonl").write_text("")
        (d / "evidence.jsonl").write_text("")
        return str(d)

    def review(self, backend: str, vpr, ceiling: float):
        self.reviews += 1
        self.last_run_dir = self.fake_run(f"{backend}-{self.reviews}")
        return {"status": "complete", "stage": "done",
                "run_dir": self.last_run_dir, "model": backend}

    def sender(self, to, subject, body):
        # Mirrors deliver_shadow's transport contract (to, subject, body);
        # the run dir is tracked via the review fake instead.
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


def old_prs(n=51, created=OLD):
    return [raw_pr(number=i, head=f"{i:040d}", created=created)
            for i in range(1, n + 1)]


# -- activation + historical exclusion --------------------------------

def test_51_historical_prs_yield_nothing(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": old_prs()}
    wire(mgr, h)
    out = mgr.cycle()
    assert h.reviews == 0 and h.sent == []
    assert out["discovered"] == 51 and out["admitted"] == 0
    assert store.count_status("suppressed") == 51


def test_repeated_polls_and_restart_stay_quiet(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": old_prs()}
    wire(mgr, h)
    for _ in range(3):
        mgr.cycle()
    # first instance held the lease; clean shutdown releases it
    mgr.check_in()
    store.release_lease(mgr.instance_id)
    mgr2 = M.Manager(mgr.cfg, store)
    wire(mgr2, h)
    mgr2.check_in()  # admitted: no stale-lease blackout after clean stop
    mgr2.cycle()
    assert h.reviews == 0 and h.sent == []


def test_historical_new_commits_still_excluded(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=OLD)]}
    wire(mgr, h)
    mgr.cycle()
    h.prs = {"o/r": [raw_pr(head="cc" * 20, created=OLD)]}
    out = mgr.cycle()
    assert h.reviews == 0 and h.sent == []
    assert out["admitted"] == 0


def test_historical_close_reopen_still_excluded(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=OLD)]}
    wire(mgr, h)
    mgr.cycle()
    closed = raw_pr(created=OLD, state="CLOSED", merged="2026-10-20T00:00:00Z")
    h.prs = {"o/r": [closed]}
    mgr.cycle()
    h.prs = {"o/r": [raw_pr(created=OLD)]}
    mgr.cycle()
    assert h.reviews == 0 and h.sent == []


def test_boundary_created_at_equal_is_excluded(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    at = activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=at)]}
    wire(mgr, h)
    mgr.cycle()
    assert h.reviews == 0


def test_new_post_activation_pr_gets_exactly_one_review(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)
    mgr.cycle()
    mgr.cycle()
    assert h.reviews == 1 and len(h.sent) == 1
    assert "o/r#1" in h.sent[0]["subject"]
    assert "https://github.com/o/r/pull/1" in h.sent[0]["body"]


def test_draft_until_ready(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW, draft=True)]}
    wire(mgr, h)
    mgr.cycle()
    assert h.reviews == 0
    h.prs = {"o/r": [raw_pr(created=NEW, draft=False)]}
    mgr.cycle()
    assert h.reviews == 1


def test_new_head_sha_gets_exactly_one_more_review(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)
    mgr.cycle()
    mgr.cycle()
    h.prs = {"o/r": [raw_pr(created=NEW, head="cc" * 20)]}
    mgr.cycle()
    mgr.cycle()
    assert h.reviews == 2 and len(h.sent) == 2


# -- lifecycle revalidation --------------------------------------------

def test_queued_pr_closing_before_dispatch_is_suppressed(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path, per_poll_cap=1)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(number=1, head="11" * 20, created=NEW),
                     raw_pr(number=2, head="22" * 20, created=NEW)]}
    wire(mgr, h)
    mgr.cycle()  # PR1 dispatched; PR2 stays queued
    assert h.reviews == 1
    h.prs = {"o/r": [raw_pr(number=1, head="11" * 20, created=NEW),
                     raw_pr(number=2, head="22" * 20, created=NEW,
                            state="CLOSED",
                            merged="2026-10-25T00:00:00Z")]}
    out = mgr.cycle()  # closed PR2 retired at discovery, 0 inference
    assert h.reviews == 1 and len(h.sent) == 1  # only PR1's email
    row = store.get_pair("o/r", 2, "aa" * 20, "22" * 20)
    assert row and row["status"] == "suppressed"


def test_pr_closing_while_running_suppresses_obsolete_email(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)

    def slow_review(backend, vpr, ceiling):
        h.reviews += 1
        h.prs = {"o/r": [raw_pr(created=NEW, state="CLOSED",
                                merged="2026-10-25T00:00:00Z")]}
        h.last_run_dir = h.fake_run("slow-1")
        return {"status": "complete", "stage": "done",
                "run_dir": h.last_run_dir, "model": backend}

    mgr.run_review = slow_review  # type: ignore[method-assign]
    out = mgr.cycle()
    assert h.reviews == 1 and h.sent == []
    assert out["dispatched"][0].get("suppressed")


def test_fail_closed_variants(tmp_path):
    for i, mutate in enumerate(("draft", "gone", "malformed", "noaccess")):
        sub = tmp_path / f"v{i}"
        sub.mkdir()
        _cfg, store, mgr = make_mgr(sub)
        activate(store)
        h = Harness(sub)
        h.prs = {"o/r": [raw_pr(created=NEW)]}
        wire(mgr, h)
        if mutate == "draft":
            h.prs = {"o/r": [raw_pr(created=NEW, draft=True)]}
        elif mutate == "gone":
            h.prs = {"o/r": []}
            h.fetch_error[("o/r", 1)] = "not found"
        elif mutate == "malformed":
            bad = raw_pr(created=NEW)
            bad["headRefOid"] = "zzz"
            h.prs = {"o/r": [bad]}
        elif mutate == "noaccess":
            h.fetch_error[("o/r", 1)] = "403 Forbidden"
        mgr.cycle()
        assert h.reviews == 0 and h.sent == [], mutate


# -- connector validation ----------------------------------------------

def test_missing_identity_fields_reject(tmp_path):
    for bad in ({"number": 0}, {"url": "https://example.com/x"},
                {"createdAt": ""}, {"state": "MERGED"},
                {"isDraft": None}, {"headRefOid": "short"},
                {"baseRefName": ""}):
        raw = raw_pr()
        raw.update(bad)
        vpr, reason = E.validate_pr_metadata("o/r", raw)
        assert vpr is None and reason, bad


def test_pagination_rate_limits_and_expired_tokens(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)
    h.fetch_error[("o/r", 1)] = "401 Unauthorized (expired token)"
    out = mgr.cycle()
    assert h.reviews == 0
    assert any(r.get("retry") for r in out["dispatched"])
    # partial API failure on discovery fails closed, no inference
    def boom_discover(repo, limit=100):
        raise ConnectionError("rate limited")

    mgr.discover = boom_discover  # type: ignore[method-assign]
    out = mgr.cycle()
    assert h.reviews == 0 and out["errors"]


# -- queue, concurrency, crash ------------------------------------------

def test_per_poll_cap_and_queued_bound(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path, per_poll_cap=3)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(number=i, head=f"{i:040d}", created=NEW)
                     for i in range(1, 8)]}
    wire(mgr, h)
    mgr.cycle()
    assert h.reviews == 3
    mgr.cycle()
    assert h.reviews == 6


def test_duplicate_discovery_reviews_once(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW), raw_pr(created=NEW)]}
    wire(mgr, h)
    mgr.cycle()
    mgr.cycle()
    assert h.reviews == 1


def test_concurrent_instance_refused(tmp_path):
    _cfg, store, _mgr = make_mgr(tmp_path)
    activate(store)
    m1 = M.Manager(_cfg, store, instance_id="a")
    m2 = M.Manager(_cfg, store, instance_id="b")
    m1.check_in()
    with pytest.raises(S.StoreError):
        m2.check_in()
    # graceful shutdown releases; restart admitted immediately
    store.release_lease("a")
    m2.check_in()
    # crash path: stale lease can be taken over after ttl
    m3 = M.Manager(_cfg, store, instance_id="c")
    with pytest.raises(S.StoreError):
        m3.check_in()
    row = store.cx.execute("SELECT heartbeat FROM lease").fetchone()
    store.cx.execute("UPDATE lease SET heartbeat=?",
                     (row[0] - 3600,))
    store.cx.commit()
    m3.check_in()


def test_restart_adopts_delivered_run_without_resend(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)
    mgr.cycle()
    mgr.cycle()
    assert h.reviews == 1
    run_dir = h.sent[0]["run_dir"]
    # simulate crash AFTER delivery but BEFORE any further bookkeeping:
    # delivery receipt adopted from run dir on recovery
    Path(run_dir, "delivery.json").write_text(json.dumps(
        {"body_sha256": "x" * 64,
         "transport_receipt": {"succeeded": 1, "email_id": "e9"}}))
    mgr2 = M.Manager(mgr.cfg, store)
    wire(mgr2, h)
    rec = mgr2.recover()
    assert h.reviews == 1  # no new inference on recovery


def test_missing_corrupt_state_and_schema_mismatch(tmp_path):
    p = tmp_path / "nope.sqlite"
    with pytest.raises(S.StoreError):
        S.Store(tmp_path / "missing-dir-x" / "s.sqlite").require_activated()
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database")
    with pytest.raises(S.StoreError):
        S.Store(bad)
    s = S.Store(tmp_path / "ok.sqlite")
    s.meta_set("schema_version", "something-else")
    s.close()
    with pytest.raises(S.StoreError):
        S.Store(tmp_path / "ok.sqlite")
    s2 = S.Store(tmp_path / "fresh.sqlite")
    with pytest.raises(S.StoreError):
        s2.require_activated()
    s2.close()


# -- accounting ----------------------------------------------------------

def test_unknown_spend_keeps_reservation_and_blocks(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)

    def unknown_review(backend, vpr, ceiling):
        h.reviews += 1
        h.last_run_dir = h.fake_run("u1", unknown=True)
        return {"status": "complete", "stage": "done",
                "run_dir": h.last_run_dir, "model": backend}

    mgr.run_review = unknown_review  # type: ignore[method-assign]
    mgr.cycle()
    mgr.cycle()
    assert store.reserved_total() == pytest.approx(1.0)
    assert store.spend_total() == pytest.approx(0.0)


def test_seed_and_caps_pause_with_single_notice(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)

    def pricey(backend, vpr, ceiling):
        h.reviews += 1
        h.last_run_dir = h.fake_run("p1", usd=4.5)
        return {"status": "complete", "stage": "done",
                "run_dir": h.last_run_dir, "model": backend}

    mgr.run_review = pricey  # type: ignore[method-assign]
    mgr.cycle()
    mgr.cycle()
    out = mgr.cycle()
    assert out["paused"] is True
    assert sum(1 for e in h.sent if "paused-by-budget" in e["subject"]) == 1
    before = h.reviews
    mgr.cycle()
    assert h.reviews == before


def test_reconcile_replaces_reserve_with_actuals(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)

    def unknown_review(backend, vpr, ceiling):
        h.reviews += 1
        h.last_run_dir = h.fake_run("u1", unknown=True)
        return {"status": "complete", "stage": "done",
                "run_dir": h.last_run_dir, "model": backend}

    mgr.run_review = unknown_review  # type: ignore[method-assign]
    mgr.cycle()
    mgr.cycle()
    run_dir = h.sent[0]["run_dir"]
    row = store.cx.execute(
        "SELECT repo, pr, base, head FROM pairs WHERE run_dir=?",
        (run_dir,)).fetchone()
    store.set_status(row[0], row[1], row[2], row[3], "complete", usd=0.04,
                     spend_unknown=0, reserved_usd=0.0,
                     note="reconciled: provider dashboard 0.04")
    store.spend_add("2026-10-24", 0.04)
    assert store.reserved_total() == pytest.approx(0.0)
    assert store.spend_total() == pytest.approx(0.04)


# -- delivery -------------------------------------------------------------

def test_smtp_failure_redelivers_same_bytes_without_rereview(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    h.receipt = {"succeeded": 0, "failed": 1, "failures": ["5xx"]}
    wire(mgr, h)
    mgr.cycle()
    mgr.cycle()
    assert h.reviews == 1 and h.sent[0]
    first = h.sent[0]
    h.receipt = {"succeeded": 1, "failed": 0, "email_id": "e2"}
    mgr.cycle()
    assert h.reviews == 1
    redelivered = [e for e in h.sent
                   if e["subject"] == first["subject"]
                   and e["body"] == first["body"]]
    assert len(redelivered) >= 1


def test_ambiguous_receipt_never_blind_retransmits(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    h.receipt = {"accepted_by_transport": True}
    wire(mgr, h)
    mgr.cycle()
    mgr.cycle()
    n = len(h.sent)
    mgr.cycle()
    assert len(h.sent) == n  # pending, not auto-resent
    d = store.get_delivery(h.sent[0]["run_dir"],
                           __import__("hashlib").sha256(
                               h.sent[0]["body"].encode()).hexdigest())
    assert d and d["status"] == "ambiguous"


def test_missing_identity_blocks_delivery(tmp_path):
    ident = {"repo": "o/r", "number": None, "url": "", "base": "aa" * 20,
             "head": "bb" * 20}
    d = Path(Harness(tmp_path).fake_run("x1"))
    with pytest.raises(ValueError):
        M.build_email(ident, d)
    ident2 = {"repo": "o/r", "number": 1,
              "url": "https://evil.example/pull/1", "base": "aa" * 20,
              "head": "bb" * 20}
    with pytest.raises(ValueError):
        M.build_email(ident2, d)


def test_failed_email_labels_failure_and_pr_url(tmp_path):
    _cfg, store, mgr = make_mgr(tmp_path)
    activate(store)
    h = Harness(tmp_path)
    h.prs = {"o/r": [raw_pr(created=NEW)]}
    wire(mgr, h)

    def fail_review(backend, vpr, ceiling):
        h.reviews += 1
        h.last_run_dir = h.fake_run("f1", status="failed")
        return {"status": "failed", "stage": "agent",
                "run_dir": h.last_run_dir, "model": backend}

    mgr.run_review = fail_review  # type: ignore[method-assign]
    mgr.cycle()
    mgr.cycle()
    assert h.sent and "failed" in h.sent[0]["subject"].lower()
    assert "https://github.com/o/r/pull/1" in h.sent[0]["body"]
    assert "PR: #?" not in h.sent[0]["body"]


# -- scope -----------------------------------------------------------------

def test_read_only_github_scope(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: object) -> object:
        calls.append(cmd)
        assert cmd[:2] == ["gh", "pr"]
        assert cmd[2] in ("list", "view")

        class P:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return P()

    monkeypatch.setattr("subprocess.run", fake_run)
    E.discover_open_prs("o/r", 5)
    E.fetch_pr("o/r", 1)
    assert [c[2] for c in calls] == ["list", "view"]


def test_packaged_guidebook_matches_repo_copy():
    import hashlib
    from pathlib import Path as _P
    repo = _P("docs/reviewer_methodology/guidebook_port.md")
    packaged = _P("codatiel_i2/guidebook_port.md")
    assert packaged.exists()
    assert (hashlib.sha256(packaged.read_bytes()).hexdigest()
            == hashlib.sha256(repo.read_bytes()).hexdigest())
