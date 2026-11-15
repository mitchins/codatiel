"""DASH-01: first self-hosted operator dashboard.

Covers the acceptance criteria with deterministic fixtures (no paid
inference, no network, no live state): overview reconciliation with
SQLite, spend breakdowns without double-counting, unknown-cost
handling, budget-block vs completed distinction, superseded vs
completed, generated vs delivered findings, cache-only reviews, empty
and degraded states, read-only guarantee, and the FastAPI route layer
(in-thread uvicorn + stdlib HTTP, no extra test dependencies).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from codatiel_i2 import dashboard_app as DA
from codatiel_i2 import dashboard_data as DD
from codatiel_i2 import event_store as S
from codatiel_i2 import operator_config as OC

REPO_A = "o/r"
REPO_B = "o/s"


@pytest.fixture
def rich_state(tmp_path: Path) -> Path:
    """Completed + blocked + stale + failed + cache-reused reviews."""
    state = tmp_path / "estate"
    state.mkdir(parents=True)
    store = S.Store(state / "events.sqlite")
    store.meta_set("enabled_at", "2026-11-01T00:00:00Z")
    store.meta_set("pipeline", "terra_luna_additive")
    store.meta_set("repos", json.dumps([REPO_A, REPO_B]))
    now = time.time()
    rd1 = state / "runs" / "rd1"
    rd1.mkdir(parents=True)
    (rd1 / "metrics.json").write_text(json.dumps(
        {"model": "terra:test"}))
    (rd1 / "delivery.json").write_text(json.dumps(
        {"transport_receipt": {"parsed": True, "succeeded": 1,
                               "failed": 0}}))
    (rd1 / "semdup.json").write_text(json.dumps(
        {"published": [{"id": "p1"}, {"id": "p2"}],
         "suppressed": [
             {"id": "s1", "reason": "budget_stop"},
             {"id": "s2", "reason": "already_delivered_email"},
             {"id": "s3", "reason": "already_delivered_github"}],
         "pairs_examined": 12, "pairs_reused": 9,
         "pairs_cache_hits": 6, "pairs_total": 15,
         "spend_terra_usd": 0.4, "spend_luna_usd": 0.1}))
    # Completed review ($1.5 known) with a posted publication.
    store.review_record(repo=REPO_A, pr=1, kind="full", base_sha="a" * 40,
                        head_sha="b" * 40, diff_sha="d1", files=3,
                        pipeline="terra_luna_additive", status="complete",
                        terra_findings=2, luna_added=1, cost_usd=1.5,
                        cost_unknown=False, run_dir=str(rd1), now=now)
    store.delivery_record(
        delivery_id="h1", event_type="pull_request", action="opened",
        repo=REPO_A, pr=1, head_sha="b" * 40, decision="completed",
        reason="full review complete", cost_usd=1.5,
        detail={"provenance_version": "cost01b-v1",
                "total_cost_usd": 1.5, "cost_unknown": False,
                "terra_spend_usd": 1.0, "luna_spend_usd": 0.0,
                "semdup_spend_usd": 0.5,
                "semdup_terra_spend_usd": 0.4,
                "semdup_luna_spend_usd": 0.1,
                "semdup_status": "ok", "semdup_published": 2,
                "pairs_examined": 12, "pairs_reused": 9,
                "pairs_cache_hits": 6, "pairs_total": 15,
                "run_dir": str(rd1)})
    store.publication_record(repo=REPO_A, pr=1, head_sha="b" * 40,
                             run_id="run1", comment_id="c1",
                             outcome="posted", reason="sent", now=now)
    store.finding_delivery_record(repo=REPO_A, pr=1, finding_hash="f1",
                                  channel="email", head_sha="b" * 40,
                                  run_id="run1", outcome="posted", now=now)
    store.finding_delivery_record(repo=REPO_A, pr=1, finding_hash="f2",
                                  channel="github", head_sha="b" * 40,
                                  run_id="run1", outcome="posted", now=now)
    # Unknown-cost completed review (never priced as $0).
    store.review_record(repo=REPO_A, pr=2, kind="full", base_sha="a" * 40,
                        head_sha="d" * 40, diff_sha="d3", files=9,
                        pipeline="terra_luna_additive", status="complete",
                        terra_findings=0, luna_added=0, cost_usd=None,
                        cost_unknown=True, run_dir="", now=now)
    # Budget-blocked head (pure refusal, still live).
    store.delivery_record(
        delivery_id="h2", event_type="pull_request", action="synchronize",
        repo=REPO_A, pr=3, head_sha="e" * 40,
        decision="skipped:ceiling", reason="daily budget reached",
        cost_usd=0.0, detail={"operator_gate": True})
    # Superseded head ($0.4 settled spend attributed separately).
    store.delivery_record(
        delivery_id="h3", event_type="pull_request", action="synchronize",
        repo=REPO_B, pr=4, head_sha="f" * 40,
        decision="skipped:stale_superseded", reason="head moved",
        cost_usd=0.4, detail={})
    # Failed review + refused publication.
    store.review_record(repo=REPO_B, pr=5, kind="full", base_sha="a" * 40,
                        head_sha="g" * 40, diff_sha="d5", files=2,
                        pipeline="terra_only", status="failed",
                        terra_findings=0, luna_added=0, cost_usd=None,
                        cost_unknown=True, run_dir="", now=now)
    store.publication_record(repo=REPO_B, pr=5, head_sha="g" * 40,
                             run_id="run5", comment_id="",
                             outcome="refused", reason="publish gate",
                             now=now)
    store.checkpoint_upsert({"repo": REPO_A, "pr": 1,
                             "head_branch": "f", "status": "reviewed",
                             "baseline_head": "a" * 40,
                             "last_reviewed_head": "b" * 40,
                             "note": ""})
    store.checkpoint_upsert({"repo": REPO_A, "pr": 3,
                             "head_branch": "f", "status": "deferred",
                             "baseline_head": "a" * 40,
                             "queued_head": "e" * 40, "note": ""})
    store.close()
    (state / "poll.last").write_text("2026-11-15T08:55:04Z\n")
    (state / "poll.log").write_text(
        "2026-11-15T08:55:04Z poll[cron] END ok ingested=1 "
        "last_ok=2026-11-15T08:55:04Z\n")
    return state


def _serve(state: Path):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        DA.create_app(state), host="127.0.0.1", port=0,
        log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        time.sleep(0.05)
        if getattr(server, "started", False):
            break
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, port


def _get(port: int, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                 timeout=10) as resp:
        return resp.status, resp.read().decode()


# -- taxonomy --------------------------------------------------------------------


def test_review_state_never_calls_budget_blocked_completed():
    assert DD.review_state("completed") == "completed"
    assert DD.review_state("skipped:ceiling") == "budget_blocked"
    assert DD.review_state("skipped:replay", "skipped:ceiling") == \
        "budget_blocked"
    assert DD.review_state("skipped:stale_head") == "stale_superseded"
    assert DD.review_state("skipped:replay",
                           "skipped:stale_superseded") == "stale_superseded"
    assert DD.review_state("failed") == "failed"
    assert DD.review_state("needs_attention:interrupted") == "failed"
    assert DD.review_state("queued") == "running"
    # A replay inherits the head's true state (the raw decision column
    # still shows skipped:replay): replay-of-completed is completed.
    assert DD.review_state("skipped:replay", "completed") == "completed"
    assert DD.review_state("skipped:replay", "failed") == "failed"
    assert DD.review_state("skipped:draft") == "skipped_replayed"
    assert DD.review_state("mystery-decision") == "skipped_replayed"


def test_poll_status_rules():
    base = 1794732904.0  # 2026-11-15T08:55:04Z
    running = DD.poll_status(
        {"last_successful_poll": "2026-11-15T08:55:04Z",
         "last_end_ok": True},
        now=base + 60)
    assert running["state"] == "running", running
    stale = DD.poll_status(
        {"last_successful_poll": "2026-11-15T08:55:04Z",
         "last_end_ok": True},
        now=base + 3600)
    assert stale["state"] == "stale", stale
    assert DD.poll_status({"last_successful_poll": None,
                           "last_end_ok": None})["state"] == "idle"
    assert DD.poll_status({"last_successful_poll": "x",
                           "last_end_ok": False})["state"] == "error"


# -- overview reconciliation -------------------------------------------------------


def test_overview_reconciles_with_sqlite(rich_state):
    data = DD.collect_overview(rich_state)
    assert data["available"]
    assert data["ledger"] == {"settled_usd": 0.0, "reserved_usd": 0.0,
                              "unknown_holds": 0.0}
    assert data["decisions"]["completed"] == 1
    assert data["decisions"]["skipped:ceiling"] == 1
    assert data["reviews"]["completed"] == 2
    assert data["reviews"]["failed"] == 1
    blocked = data["budget_blocked"]
    assert len(blocked) == 1 and blocked[0]["decision"] == \
        "skipped:ceiling"
    assert all(b["decision"] != "completed" for b in blocked)
    assert len(data["stale"]) == 1
    assert data["efficiency"]["pairs_reused"] == 9
    assert data["efficiency"]["cache_hit_pct"] == pytest.approx(75.0)
    assert data["efficiency"]["avoided_usd"] is None
    assert data["publications"]["posted"] == 1
    assert data["publications"]["refused"] == 1


def test_spend_reconciles_without_double_counting(rich_state):
    data = DD.collect_spend(rich_state, days=3650)
    assert data["available"]
    known = sum(r.get("codatiel_ledger_usd") or 0.0
                for r in data["daily"]
                if "codatiel_ledger_usd" in r)
    # Day ledger = known review spend ($1.5) + superseded inference
    # ($0.4), same formula as the authoritative spend_on_day.
    assert known == pytest.approx(1.9)
    by_repo = {r["repo"]: r for r in data["by_repo"]}
    assert by_repo[REPO_A]["known_usd"] == pytest.approx(1.5)
    assert by_repo[REPO_A]["unknown_cost_reviews"] == 1
    # No double counting: reviews-known + superseded == daily total.
    sup = data["superseded"]
    assert sup["count"] == 1 and sup["known_usd"] == pytest.approx(0.4)
    assert by_repo[REPO_A]["known_usd"] + sup["known_usd"] == \
        pytest.approx(known)
    by_pr = {(r["repo"], r["pr"]): r for r in data["by_pr"]}
    assert by_pr[(REPO_A, 1)]["known_usd"] == pytest.approx(1.5)
    assert by_pr[(REPO_A, 1)]["unknown_cost_reviews"] == 0
    assert by_pr[(REPO_A, 2)]["known_usd"] == pytest.approx(0.0)
    assert by_pr[(REPO_A, 2)]["unknown_cost_reviews"] == 1
    memo = data["stage_memo"]
    assert memo["available"]
    assert memo["terra_usd"] == pytest.approx(1.0)
    assert memo["semdup_terra_usd"] == pytest.approx(0.4)
    sup = data["superseded"]
    assert sup["count"] == 1 and sup["known_usd"] == pytest.approx(0.4)
    unk = data["unknown"]
    assert unk["reviews"] == 2 and unk["superseded"] == 0
    assert data["ledger"]["settled_usd"] == 0.0


def test_unknown_cost_never_zero(rich_state):
    data = DD.collect_pr(rich_state, REPO_A, 2)
    assert data["available"]
    assert len(data["reviews"]) == 1
    cost = data["reviews"][0]["cost_usd"]
    assert cost is None
    server, thread, port = _serve(rich_state)
    try:
        _, body = _get(port, "/pr/o/r/2")
        assert "unknown" in body
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# -- drill-down --------------------------------------------------------------------


def test_pr_history_distinguishes_generated_delivered_suppressed(
        rich_state):
    data = DD.collect_pr(rich_state, REPO_A, 1)
    assert data["available"]
    assert data["checkpoint"]["status"] == "reviewed"
    gvd = data["generated_vs_delivered"]
    assert gvd["terra_findings"] == 2
    assert gvd["semdup_published"] == 2
    assert gvd["semdup_suppressed"] == 3
    assert gvd["finding_deliveries_posted"] == 2
    assert gvd["publications_posted"] == 1
    assert gvd["suppressed_as_previously_delivered"] == 2
    assert gvd["generated_total"] == 7
    cache = data["deliveries"][0]["cache_reuse"]
    assert cache["pairs_examined"] == 12
    assert cache["pairs_reused"] == 9
    assert cache["pairs_cache_hits"] == 6


def test_repos_queue_and_filters(rich_state):
    data = DD.collect_repos(rich_state)
    assert data["available"]
    by_repo = {r["repo"]: r for r in data["repos"]}
    assert REPO_A in by_repo
    assert by_repo[REPO_A]["queue_by_state"].get("budget_blocked") == 1
    assert by_repo[REPO_A]["queue_by_state"].get("completed") == 1
    assert by_repo[REPO_A]["tracked_prs"] == [1, 3]
    server, thread, port = _serve(rich_state)
    try:
        status, body = _get(port, "/repos")
        assert status == 200 and "o/r" in body
        status, body = _get(port, "/pr/o/r/1")
        assert status == 200
        assert "generated" in body.lower() or "Generated" in body
        status, body = _get(port, "/ops?state=budget_blocked")
        assert status == 200 and "skipped:ceiling" in body
        status, body = _get(port, "/spend?days=7")
        assert status == 200 and "Known USD" in body
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_unknown_pr_is_404(rich_state):
    server, thread, port = _serve(rich_state)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(port, "/pr/o/r/999")
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as exc2:
            _get(port, "/pr/o/r/notanumber")
        assert exc2.value.code == 404
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# -- read-only guarantee -------------------------------------------------------------


def test_reads_change_nothing(rich_state):
    import sqlite3

    def snapshot():
        db = rich_state / "events.sqlite"
        cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            totals = {k: cx.execute(
                "SELECT value FROM spend WHERE key=?",
                (k,)).fetchone()[0]
                for k in ("settled_usd", "reserved_usd",
                          "unknown_holds")}
            counts = {
                "deliveries": cx.execute(
                    "SELECT COUNT(*) FROM deliveries").fetchone()[0],
                "reviews": cx.execute(
                    "SELECT COUNT(*) FROM reviews").fetchone()[0],
            }
        finally:
            cx.close()
        digest = hashlib.sha256(db.read_bytes()).hexdigest()
        op = rich_state / "operator.json"
        return totals, counts, digest, (op.read_bytes() if op.exists()
                                        else None)
    before = snapshot()
    server, thread, port = _serve(rich_state)
    try:
        for path in ("/", "/repos", "/pr/o/r/1", "/spend",
                      "/efficiency", "/ops", "/settings",
                      "/api/overview", "/api/repos", "/api/spend",
                      "/api/efficiency", "/api/ops", "/api/pr/o/r/1",
                      "/healthz"):
            _get(port, path)
        # Bad form posts change nothing either.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/settings",
                     body=urllib.parse.urlencode(
                         {"monthly_budget_usd": "-3"}).encode(),
                     headers={"Content-Type":
                              "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        assert resp.status in (400, 403)
        resp.read()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert snapshot() == before


def test_empty_queue_renders(rich_state):
    empty = rich_state.parent / "empty"
    empty.mkdir()
    server, thread, port = _serve(empty)
    try:
        for path in ("/", "/repos", "/spend", "/efficiency",
                      "/ops", "/settings"):
            status, body = _get(port, path)
            assert status == 200, path
            assert "Traceback" not in body
    finally:
        server.should_exit = True
        thread.join(timeout=10)
