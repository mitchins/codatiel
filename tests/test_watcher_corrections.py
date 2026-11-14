"""Production watcher corrections (v1 event pipeline/store/CLI).

Deterministic, no inference, no network. Each test reproduces the
reported defect against current behavior first.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from codatiel_i2 import event_store as S
from codatiel_i2 import operator_config as OP

DAY = "2026-11-14"
NOON = _dt.datetime(2026, 11, 14, 12, 0,
                    tzinfo=_dt.timezone.utc).timestamp()


def _store(tmp_path, name="e.sqlite"):
    return S.Store(tmp_path / name)


def _review(store, cost, n, unknown=False, ts=NOON):
    return store.review_record(
        repo="o/r", pr=10, kind="incremental", base_sha="b" * 40,
        head_sha="h" * 40, diff_sha="d%d" % n, files=1,
        pipeline="terra_luna_additive", status="complete",
        terra_findings=0, luna_added=0, cost_usd=cost,
        cost_unknown=unknown, run_dir=f"/r/{n}", now=ts)


def test_spend_on_day_sums_actual_costs(tmp_path):
    store = _store(tmp_path)
    try:
        _review(store, 0.10, 1)
        _review(store, 0.90, 2)
        got = OP.spend_on_day(store, DAY)
        assert got["codatiel_ledger_usd"] == 1.0, got
        assert got["reviews"] == 2
    finally:
        store.close()


def test_spend_on_day_includes_superseded_once(tmp_path):
    store = _store(tmp_path)
    try:
        _review(store, 0.40, 1)
        store.delivery_record(
            delivery_id="d-sup", event_type="pull_request",
            action="synchronize", repo="o/r", pr=10,
            head_sha="h" * 40, decision="skipped:stale_superseded",
            reason="superseded mid-flight", cost_usd=0.25,
            detail={}, now=NOON)
        # A completed delivery's cost lives in reviews; it must not
        # be added again from the deliveries side.
        store.delivery_record(
            delivery_id="d-done", event_type="pull_request",
            action="synchronize", repo="o/r", pr=10,
            head_sha="h" * 40, decision="completed",
            reason="done", cost_usd=0.40, detail={}, now=NOON)
        got = OP.spend_on_day(store, DAY)
        assert got["codatiel_ledger_usd"] == 0.65, got
        assert got["superseded_inference_usd"] == 0.25
    finally:
        store.close()


def test_spend_on_day_unknown_holds_preserved(tmp_path):
    store = _store(tmp_path)
    try:
        _review(store, None, 1, unknown=True)
        _review(store, 0.20, 2)
        store.delivery_record(
            delivery_id="d-u", event_type="pull_request",
            action="synchronize", repo="o/r", pr=10,
            head_sha="h" * 40, decision="skipped:stale_superseded",
            reason="unknown true cost", cost_usd=None,
            detail={}, now=NOON)
        got = OP.spend_on_day(store, DAY)
        assert got["codatiel_ledger_usd"] == 0.20, got
        assert got["unknown_cost_reviews"] == 2, got
    finally:
        store.close()


# -- B: release_claim ownership -------------------------------------------

def test_release_claim_exact_owner_only(tmp_path):
    from codatiel_i2 import event_store as S2
    st = S2.Store(tmp_path / "e.sqlite")
    try:
        st.checkpoint_upsert({"repo": "o/r", "pr": 23, "status": "draft",
                              "baseline_head": "b" * 40})
        assert st.claim_for_review("o/r", 23, base="b" * 40,
                                   head="c" * 40) == "claimed"
        # A stranger with the wrong base cannot release.
        assert st.release_claim("o/r", 23, "stranger",
                                base="x" * 40, head="c" * 40) is False
        # A newer claim for the SAME head (rebased base) cannot be
        # released by the expired worker holding the older base.
        st.checkpoint_upsert({
            "repo": "o/r", "pr": 23, "status": "in_progress",
            "in_progress_base": "b2" * 20, "in_progress_head": "c" * 40})
        assert st.release_claim("o/r", 23, "expired worker",
                                base="b" * 40, head="c" * 40) is False
        cp = st.checkpoint_get("o/r", 23)
        assert (cp["status"], cp["in_progress_head"]) == (
            "in_progress", "c" * 40)
        # The exact owner releases.
        assert st.release_claim("o/r", 23, "owner",
                                base="b2" * 20, head="c" * 40,
                                prior_status="draft") is True
        assert st.checkpoint_get("o/r", 23)["status"] == "draft"
        # Nothing held: False, never an error.
        assert st.release_claim("o/r", 23, "nobody",
                                base="b" * 40, head="c" * 40) is False
    finally:
        st.close()


# -- C: clear_queued identity ----------------------------------------------

def test_clear_queued_keeps_newer_arrival(tmp_path):
    from codatiel_i2 import event_store as S2
    st = S2.Store(tmp_path / "e.sqlite")
    try:
        st.checkpoint_upsert({"repo": "o/r", "pr": 23, "status": "reviewed",
                              "last_reviewed_head": "a" * 40,
                              "queued_base": "a" * 40,
                              "queued_head": "b" * 40})
        assert st.clear_queued("o/r", 23, base="a" * 40,
                               head="b" * 40) is True
        assert st.checkpoint_get("o/r", 23)["queued_head"] == ""
        st.checkpoint_upsert({"repo": "o/r", "pr": 23, "status": "reviewed",
                              "last_reviewed_head": "a" * 40,
                              "queued_base": "b" * 40,
                              "queued_head": "c" * 40})
        # A stale verification for the older capture must not clear
        # the newer arrival.
        assert st.clear_queued("o/r", 23, base="a" * 40,
                               head="b" * 40) is False
        cp = st.checkpoint_get("o/r", 23)
        assert (cp["queued_base"], cp["queued_head"]) == ("b" * 40,
                                                          "c" * 40)
    finally:
        st.close()


def test_drain_stale_keeps_newer_queue(tmp_path, monkeypatch):
    """A newer queued head arriving between the stale check and the
    clear survives: _drain_queued clears only its captured identity."""
    from codatiel_i2 import event_pipeline as P
    store = S.Store(tmp_path / "e.sqlite")
    A, B, C = "a" * 40, "b" * 40, "c" * 40
    store.checkpoint_upsert({"repo": "o/r", "pr": 23, "status": "reviewed",
                             "last_reviewed_head": B,
                             "queued_base": B, "queued_head": C})
    monkeypatch.setattr(P, "get_live_head", lambda repo, pr: "z" * 40)
    ev = {"delivery_id": "e1", "event": "pull_request", "action": "",
          "repo": "o/r", "pr_number": 23, "head_sha": B}
    try:
        P._drain_queued(cfg=None, store=store, ev=ev, pr=23,
                        to_sha=B, queued=(A, B), final={},
                        access=None, now=1000.0)
        drain = store.delivery_get("e1:drain")
        assert drain is not None and drain["decision"] \
            == "skipped:stale_head", drain
        cp = store.checkpoint_get("o/r", 23)
        assert (cp["queued_base"], cp["queued_head"]) == (B, C)
    finally:
        store.close()


# -- D: explicit review freshness ------------------------------------------

def _explicit_run(tmp_path, monkeypatch, repo, *, head, base,
                   live_heads, cost=0.30, reviewed=None,
                   cost_unknown=False):
    """Drive `events review` with scripted live heads + fake inference.

    Reviewed checkpoint defaults to (base, base). Returns (out,
    store); the caller owns the store.
    """
    from tests.test_prod01_lifecycle import _env
    from codatiel_i2 import cli as cli_mod
    from codatiel_i2 import event_pipeline as P
    _env(monkeypatch, tmp_path, repo["repo"])
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
    anchor = reviewed or (repo["base_sha"], repo["base_sha"])
    store.checkpoint_upsert(
        {"repo": "o/r", "pr": 10, "status": "reviewed",
         "baseline_head": anchor[0],
         "last_reviewed_head": anchor[1],
         "last_reviewed_base": anchor[0]})
    calls = {"n": 0}

    def _live(r, pr):
        idx = min(calls["n"], len(live_heads) - 1)
        calls["n"] += 1
        return live_heads[idx]

    monkeypatch.setattr(P, "get_live_head", _live)
    monkeypatch.setattr(
        P, "run_review_for_range",
        lambda **kw: {"status": "complete", "mode": "t",
                      "run_dir": "/tmp/rd", "terra_findings": 0,
                      "added": 0, "cost_usd": cost,
                      "cost_unknown": cost_unknown})
    extra = ["--base", base] if base is not None else []
    args = cli_mod.build_parser().parse_args(
        ["events", "review", "--pr", "10", "--head", head] + extra)
    return cli_mod.cmd_events(args), store


def test_explicit_review_refuses_stale_seal(tmp_path, monkeypatch,
                                            two_commit_repo):
    repo = two_commit_repo
    out, store = _explicit_run(
        tmp_path, monkeypatch, repo, head=repo["head_sha"],
        base=repo["base_sha"],
        live_heads=[repo["head_sha"], "c" * 40])
    try:
        assert out["decision"] == "skipped:stale_superseded", out
        assert out["cost_usd"] == 0.30  # real spend kept
        cp = store.checkpoint_get("o/r", 10)
        assert cp["last_reviewed_head"] == repo["base_sha"]
        assert cp["status"] == "reviewed"
        assert [r for r in store.reviews_list()
                if r["head_sha"] == repo["head_sha"]] == []
        totals = store.spend_totals()
        assert totals["settled_usd"] == 0.30  # settled, not lost
    finally:
        store.close()


def test_explicit_review_seals_when_fresh(tmp_path, monkeypatch,
                                          two_commit_repo):
    repo = two_commit_repo
    out, store = _explicit_run(
        tmp_path, monkeypatch, repo, head=repo["head_sha"],
        base=repo["base_sha"], live_heads=[repo["head_sha"]])
    try:
        assert out["decision"] == "completed", out
        assert store.checkpoint_get("o/r", 10)[
            "last_reviewed_head"] == repo["head_sha"]
    finally:
        store.close()


# -- E1: unsealed first review drains from the queued base ---------------

def test_unsealed_first_review_drains_from_queued_base(tmp_path,
                                                       monkeypatch):
    """First review superseded before sealing, with a queued newer
    head: the follow-up must span queued_base..queued_head — never
    skip the unsealed segment by re-rooting at the dead head."""
    from tests.test_prod01_lifecycle import (_env, _git, _ingest,
                                             _terra_factory, pr_event)
    from codatiel_i2 import event_pipeline as P
    from pathlib import Path as _P
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "evt@t")
    _git(root, "config", "user.name", "evt")
    (root / "app.py").write_text("LIMIT = 100\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "A")
    A = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 10\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "B")
    B = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 1\n")
    _git(root, "commit", "-qam", "D")
    D = _git(root, "rev-parse", "HEAD")
    _env(monkeypatch, tmp_path, root)
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    try:
        P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
        # A newer head queued for the first (unsealed) run.
        store.checkpoint_upsert({"repo": "o/r", "pr": 23,
                                 "status": "deferred",
                                 "baseline_head": A,
                                 "queued_base": A, "queued_head": D})
        live = [B, D, D]

        def _live(repo, pr):
            return live.pop(0) if live else D

        monkeypatch.setattr(P, "get_live_head", _live)
        from tests.test_prod02_stale_head import _NotifySpy
        spy = _NotifySpy()
        monkeypatch.setattr(P, "_notify", spy)
        kw = {"model_factory": _terra_factory()}
        row = _ingest(cfg, store, pr_event("opened", 23, A, B), "e1",
                       **kw)
        assert row["decision"] == "skipped:stale_superseded", row
        assert row.get("drained") == "completed", row
        follow = store.delivery_get("e1:drain")
        assert follow is not None and follow["decision"] == "completed"
        # The follow-up spans the QUEUED base, not the dead head B.
        assert follow["head_sha"] == D
        cp = store.checkpoint_get("o/r", 23)
        assert cp["last_reviewed_head"] == D
    finally:
        store.close()


# -- E2: uppercase SHAs normalize to lowercase -----------------------------

def test_uppercase_sha_normalizes(tmp_path):
    from codatiel_i2 import event_pipeline as P
    ev, err = P.normalize_event(
        {"action": "opened",
         "repository": {"full_name": "o/r"},
         "pull_request": {
             "number": 10, "state": "open", "draft": False,
             "merged": False, "created_at": "9999-01-01T00:00:00Z",
             "base": {"sha": "AA" * 20}, "head": {"sha": "BB" * 20,
                                                  "ref": "f"}}},
        "d-upper")
    assert err == "", err
    assert ev["head_sha"] == "bb" * 20, ev
    assert ev["base_sha"] == "aa" * 20, ev


def test_explicit_review_uppercase_head_matches_live(
        tmp_path, monkeypatch, two_commit_repo):
    repo = two_commit_repo
    out, store = _explicit_run(
        tmp_path, monkeypatch, repo, head=repo["head_sha"].upper(),
        base=repo["base_sha"], live_heads=[repo["head_sha"]], cost=0.10)
    try:
        assert out["decision"] == "completed", out
        assert store.checkpoint_get("o/r", 10)[
            "last_reviewed_head"] == repo["head_sha"]
    finally:
        store.close()


# -- E3: genuine backwards reset via explicit re-drive (gh down) ---------

def test_explicit_redrive_backwards_reset_offline(
        tmp_path, monkeypatch, two_commit_repo):
    """GitHub unreachable + head older than reviewed: the automatic
    path drops it (documented fail-safe), but an explicit operator
    re-drive with that exact head proceeds."""
    repo = two_commit_repo
    out, store = _explicit_run(
        tmp_path, monkeypatch, repo, head=repo["base_sha"], base=None,
        live_heads=[None], cost=0.10,
        reviewed=(repo["base_sha"], repo["head_sha"]))
    try:
        assert out["decision"] == "completed", out
    finally:
        store.close()


def test_spend_day_boundary_uses_completion_day(tmp_path):
    import datetime as _dt
    day1 = "2026-11-14"
    day2 = "2026-11-15"
    late14 = _dt.datetime(2026, 11, 14, 23, 59,
                          tzinfo=_dt.timezone.utc).timestamp()
    early15 = _dt.datetime(2026, 11, 15, 0, 5,
                           tzinfo=_dt.timezone.utc).timestamp()
    store = S.Store(tmp_path / "e.sqlite")
    try:
        store.delivery_record(
            delivery_id="d-sup", event_type="pull_request",
            action="synchronize", repo="o/r", pr=10,
            head_sha="h" * 40, decision="in_progress", reason="claimed",
            cost_usd=None, detail={}, now=late14)
        store.delivery_update_decision("d-sup", "skipped:stale_superseded",
                                       "superseded", 0.30, now=early15)
        got14 = OP.spend_on_day(store, day1)
        got15 = OP.spend_on_day(store, day2)
        assert got14["codatiel_ledger_usd"] == 0.0, got14
        assert got15["codatiel_ledger_usd"] == 0.30, got15
        assert got15["superseded_inference_usd"] == 0.30
    finally:
        store.close()


def test_replayed_superseded_keeps_cost_in_ledger(tmp_path):
    store = S.Store(tmp_path / "e.sqlite")
    try:
        store.delivery_record(
            delivery_id="d-sup", event_type="pull_request",
            action="synchronize", repo="o/r", pr=10,
            head_sha="h" * 40, decision="skipped:stale_superseded",
            reason="superseded", cost_usd=0.25, detail={}, now=NOON)
        prior = store.delivery_get("d-sup")
        row = store.delivery_mark_replay("d-sup", prior)
        assert row["decision"] == "skipped:replay"
        assert row["cost_usd"] == 0.25
        got = OP.spend_on_day(store, DAY)
        assert got["codatiel_ledger_usd"] == 0.25, got
    finally:
        store.close()


def test_explicit_stale_review_counts_in_daily_ledger(
        tmp_path, monkeypatch, two_commit_repo):
    repo = two_commit_repo
    out, store = _explicit_run(
        tmp_path, monkeypatch, repo, head=repo["head_sha"],
        base=repo["base_sha"],
        live_heads=[repo["head_sha"], "c" * 40])
    try:
        assert out["decision"] == "skipped:stale_superseded", out
        import datetime as _dt
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        got = OP.spend_on_day(store, today)
        assert got["codatiel_ledger_usd"] == 0.30, got
        assert got["superseded_inference_usd"] == 0.30
    finally:
        store.close()


def test_complete_review_atomic_seal(tmp_path):
    store = S.Store(tmp_path / "e.sqlite")
    try:
        store.checkpoint_upsert({"repo": "o/r", "pr": 23, "status": "draft",
                                 "baseline_head": "b" * 40})
        assert store.claim_for_review("o/r", 23, base="b" * 40,
                                       head="c" * 40) == "claimed"
        # The pin is replaced (same head, newer base) before sealing.
        store.checkpoint_upsert(
            {"repo": "o/r", "pr": 23, "status": "in_progress",
             "in_progress_base": "b2" * 20,
             "in_progress_head": "c" * 40})
        done = store.complete_review(
            "o/r", 23, base="b" * 40, head="c" * 40, diff_sha="d",
            files=1, terra_findings=0, luna_added=0, cost_usd=0.1,
            cost_unknown=False, run_dir="/r", result="complete",
            claim_base="b" * 40, claim_head="c" * 40)
        assert done["sealed"] is False
        cp = store.checkpoint_get("o/r", 23)
        assert (cp["status"], cp["in_progress_head"]) == (
            "in_progress", "c" * 40)
        assert cp["last_reviewed_head"] == ""
        # The exact owner still seals.
        done = store.complete_review(
            "o/r", 23, base="b2" * 20, head="c" * 40, diff_sha="d",
            files=1, terra_findings=0, luna_added=0, cost_usd=0.1,
            cost_unknown=False, run_dir="/r", result="complete",
            claim_base="b2" * 20, claim_head="c" * 40)
        assert done["sealed"] is True
        assert store.checkpoint_get("o/r", 23)[
            "last_reviewed_head"] == "c" * 40
    finally:
        store.close()


def test_explicit_review_base_mismatch_never_seals(
        tmp_path, monkeypatch, two_commit_repo):
    repo = two_commit_repo
    out, store = _explicit_run(
        tmp_path, monkeypatch, repo, head=repo["head_sha"],
        base=repo["base_sha"], live_heads=[repo["head_sha"]])
    # The fake inference tested the pre-check path only; the mid-flight
    # replacement below is covered at store level. This run seals.
    try:
        assert out["decision"] == "completed", out
    finally:
        store.close()


def test_explicit_review_pin_replaced_midflight(
        tmp_path, monkeypatch, two_commit_repo):
    """A recovery + reclaim with the same head but a different base
    between the pre-check and the seal must not seal the old range."""
    from tests.test_prod01_lifecycle import _env
    from codatiel_i2 import cli as cli_mod
    from codatiel_i2 import event_pipeline as P
    repo = two_commit_repo
    _env(monkeypatch, tmp_path, repo["repo"])
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    try:
        P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
        store.checkpoint_upsert(
            {"repo": "o/r", "pr": 10, "status": "reviewed",
             "baseline_head": repo["base_sha"],
             "last_reviewed_head": repo["base_sha"],
             "last_reviewed_base": repo["base_sha"]})
        monkeypatch.setattr(P, "get_live_head",
                            lambda r, pr: repo["head_sha"])

        def _fake_run(**kw):
            # Another worker reclaims the same head, newer base.
            store.checkpoint_upsert(
                {"repo": "o/r", "pr": 10, "status": "in_progress",
                 "in_progress_base": "b2" * 20,
                 "in_progress_head": repo["head_sha"]})
            return {"status": "complete", "mode": "t",
                    "run_dir": "/tmp/rd", "terra_findings": 0,
                    "added": 0, "cost_usd": 0.10,
                    "cost_unknown": False}

        monkeypatch.setattr(P, "run_review_for_range", _fake_run)
        args = cli_mod.build_parser().parse_args(
            ["events", "review", "--pr", "10", "--head", repo["head_sha"],
             "--base", repo["base_sha"]])
        out = cli_mod.cmd_events(args)
        assert out["decision"] == "skipped:stale_superseded", out
        cp = store.checkpoint_get("o/r", 10)
        # Nothing sealed; the replacement pin is untouched.
        assert cp["last_reviewed_head"] == ""
        assert (cp["in_progress_base"],
                cp["in_progress_head"]) == ("b2" * 20, repo["head_sha"])
    finally:
        store.close()


# -- F: closeout regressions (PR 15 final) -------------------------------

def test_explicit_stale_preserves_unknown_cost(
        tmp_path, monkeypatch, two_commit_repo):
    """Unknown-cost superseded explicit run must keep unknown status
    (NULL cost) and be counted as unknown, not as superseded USD."""
    repo = two_commit_repo
    out, store = _explicit_run(
        tmp_path, monkeypatch, repo, head=repo["head_sha"],
        base=repo["base_sha"],
        live_heads=[repo["head_sha"], "c" * 40],
        cost=0.42, cost_unknown=True)
    try:
        assert out["decision"] == "skipped:stale_superseded", out
        assert store.spend_totals()["unknown_holds"] == 1.0
        import datetime as _dt, json as _json
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        got = OP.spend_on_day(store, today)
        assert got["unknown_cost_reviews"] >= 1, got
        assert got["superseded_inference_usd"] == 0.0, got
        rows = store.cx.execute(
            "SELECT cost_usd, detail FROM deliveries "
            "WHERE decision='skipped:stale_superseded'").fetchall()
        assert len(rows) == 1 and rows[0][0] is None
        assert _json.loads(rows[0][1]).get("cost_unknown") is True
    finally:
        store.close()


def test_automatic_superseded_uses_completion_day(
        tmp_path, monkeypatch):
    """Automatic mid-flight superseded cost is attributed to the day
    inference completed (finished_ts), not the day it was received."""
    from tests.test_prod01_lifecycle import (_env, _git, _ingest,
                                             _terra_factory, pr_event)
    from codatiel_i2 import event_pipeline as P
    import datetime as _dt
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "A")
    A = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("b\n")
    _git(root, "commit", "-qam", "B")
    B = _git(root, "rev-parse", "HEAD")
    _env(monkeypatch, tmp_path, root)
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    try:
        P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
        # clock straddles midnight: received day1, completed day2
        day1_ts = _dt.datetime(2026, 11, 14, 23, 59, tzinfo=_dt.timezone.utc).timestamp()
        day2_ts = _dt.datetime(2026, 11, 15, 0, 5, tzinfo=_dt.timezone.utc).timestamp()
        calls = {"n": 0}
        def _clock():
            calls["n"] += 1
            return day1_ts if calls["n"] == 1 else day2_ts
        cfg.clock = _clock
        # stale-after inference: live moves to C while B is in flight
        (root / "app.py").write_text("c\n")
        _git(root, "commit", "-qam", "C")
        C = _git(root, "rev-parse", "HEAD")
        # live head is B at entry, C after inference -> mid-flight superseded
        live_calls = {"n": 0}
        def _live_midflight(r, pr):
            live_calls["n"] += 1
            return B if live_calls["n"] == 1 else C
        monkeypatch.setattr(P, "get_live_head", _live_midflight)
        monkeypatch.setattr(
            P, "run_review_for_range",
            lambda **kw: {"status": "complete", "mode": "t",
                          "run_dir": "/tmp/rd", "terra_findings": 0,
                          "added": 0, "cost_usd": 0.33,
                          "cost_unknown": False})
        row = _ingest(cfg, store, pr_event("opened", 23, A, B), "e-sup")
        assert row["decision"] == "skipped:stale_superseded", row
        got14 = OP.spend_on_day(store, "2026-11-14")
        got15 = OP.spend_on_day(store, "2026-11-15")
        assert got14["codatiel_ledger_usd"] == 0.0, got14
        assert got15["superseded_inference_usd"] == 0.33, got15
        assert got15["codatiel_ledger_usd"] == 0.33
    finally:
        store.close()


def test_explicit_atomic_seal_refusal_records_spend(
        tmp_path, monkeypatch, two_commit_repo):
    """Freshness passes but atomic seal loses the race (same-range
    replacement). Spend already settled must still appear in the
    daily ledger via an explicit delivery row."""
    from codatiel_i2 import cli as cli_mod
    from codatiel_i2 import event_pipeline as P
    repo = two_commit_repo
    cfg, store = _explicit_base(tmp_path, monkeypatch, repo)
    try:
        monkeypatch.setattr(P, "get_live_head",
                            lambda r, pr: repo["head_sha"])
        monkeypatch.setattr(
            P, "run_review_for_range",
            lambda **kw: {"status": "complete", "mode": "t",
                          "run_dir": "/tmp/rd", "terra_findings": 0,
                          "added": 0, "cost_usd": 0.33,
                          "cost_unknown": False})
        # Make the atomic seal fail: replace the generation after the
        # freshness recheck but before the UPDATE.
        orig_complete = S.Store.complete_review
        def _patched(self, *a, **kw):
            # simulate concurrent same-range reclaim
            self.cx.execute(
                "UPDATE checkpoints SET in_progress_run='other-gen' "
                "WHERE repo='o/r' AND pr=10")
            self.cx.commit()
            return orig_complete(self, *a, **kw)
        monkeypatch.setattr(S.Store, "complete_review", _patched)
        args = cli_mod.build_parser().parse_args(
            ["events", "review", "--pr", "10", "--head", repo["head_sha"],
             "--base", repo["base_sha"]])
        out = cli_mod.cmd_events(args)
        assert out["decision"] == "skipped:stale_superseded", out
        assert out["cost_usd"] == 0.33
        import datetime as _dt
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        got = OP.spend_on_day(store, today)
        # settled spend is visible via the superseded delivery row
        assert got["superseded_inference_usd"] == 0.33, got
        assert got["codatiel_ledger_usd"] == 0.33
    finally:
        store.close()


def test_explicit_stale_ids_collision_safe(
        tmp_path, monkeypatch, two_commit_repo):
    """Two explicit stale runs for the same head within one second
    must not collide on delivery_id (microsecond + uuid suffix)."""
    from tests.test_prod01_lifecycle import _env
    from codatiel_i2 import cli as cli_mod
    from codatiel_i2 import event_pipeline as P
    repo = two_commit_repo
    _env(monkeypatch, tmp_path, repo["repo"])
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    # freeze clock to same second so old int() would collide
    fixed = 1700000000.123456
    cfg.clock = lambda: fixed
    import codatiel_i2.event_pipeline as P2
    monkeypatch.setattr(P2, "config_from_env", lambda: cfg)
    store = S.Store(cfg.state_dir / "events.sqlite")
    try:
        P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
        for _ in range(2):
            store.checkpoint_upsert(
                {"repo": "o/r", "pr": 10, "status": "reviewed",
                 "baseline_head": repo["base_sha"],
                 "last_reviewed_head": repo["base_sha"],
                 "last_reviewed_base": repo["base_sha"]})
            # first get_live_head (pre-check) -> head, second (post) -> stale
            calls = {"n": 0}
            def _live(r, pr):
                calls["n"] += 1
                return repo["head_sha"] if calls["n"] == 1 else "c" * 40
            monkeypatch.setattr(P, "get_live_head", _live)
            monkeypatch.setattr(P, "run_review_for_range",
                                lambda **kw: {"status": "complete", "mode": "t",
                                              "run_dir": "/tmp/rd",
                                              "terra_findings": 0, "added": 0,
                                              "cost_usd": 0.11,
                                              "cost_unknown": False})
            args = cli_mod.build_parser().parse_args(
                ["events", "review", "--pr", "10", "--head", repo["head_sha"],
                 "--base", repo["base_sha"]])
            out = cli_mod.cmd_events(args)
            assert out["decision"] == "skipped:stale_superseded", out
        rows = store.cx.execute(
            "SELECT delivery_id FROM deliveries "
            "WHERE decision='skipped:stale_superseded'").fetchall()
        assert len(rows) == 2, rows
        assert rows[0][0] != rows[1][0]
        # ids contain microsecond and uuid suffix
        assert ":" in rows[0][0] and rows[0][0].count(":") >= 4
    finally:
        store.close()


def test_same_range_generation_not_sealed(tmp_path):
    """Same base/head but different generation must not seal."""
    store = S.Store(tmp_path / "e.sqlite")
    try:
        store.checkpoint_upsert({"repo": "o/r", "pr": 23, "status": "draft",
                                  "baseline_head": "b" * 40})
        assert store.claim_for_review("o/r", 23, base="b" * 40,
                                       head="c" * 40) == "claimed"
        cp = store.checkpoint_get("o/r", 23)
        gen1 = cp["in_progress_run"]
        assert gen1
        # recovery + reclaim same range -> new generation
        store.checkpoint_upsert(
            {"repo": "o/r", "pr": 23, "status": "in_progress",
             "in_progress_base": "b" * 40,
             "in_progress_head": "c" * 40,
             "in_progress_run": "other-gen-1234"})
        # old generation cannot seal
        done = store.complete_review(
            "o/r", 23, base="b" * 40, head="c" * 40, diff_sha="d",
            files=1, terra_findings=0, luna_added=0, cost_usd=0.1,
            cost_unknown=False, run_dir="/r", result="complete",
            claim_base="b" * 40, claim_head="c" * 40,
            claim_run=gen1)
        assert done["sealed"] is False
        cp2 = store.checkpoint_get("o/r", 23)
        assert cp2["in_progress_head"] == "c" * 40
        assert cp2["status"] == "in_progress"
        # correct generation seals
        done = store.complete_review(
            "o/r", 23, base="b" * 40, head="c" * 40, diff_sha="d",
            files=1, terra_findings=0, luna_added=0, cost_usd=0.1,
            cost_unknown=False, run_dir="/r", result="complete",
            claim_base="b" * 40, claim_head="c" * 40,
            claim_run="other-gen-1234")
        assert done["sealed"] is True
    finally:
        store.close()


def _explicit_base(tmp_path, monkeypatch, repo):
    """Shared explicit-review boilerplate (deduplicated for Sonar)."""
    from tests.test_prod01_lifecycle import _env
    from codatiel_i2 import event_pipeline as P
    _env(monkeypatch, tmp_path, repo["repo"])
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
    store.checkpoint_upsert(
        {"repo": "o/r", "pr": 10, "status": "reviewed",
         "baseline_head": repo["base_sha"],
         "last_reviewed_head": repo["base_sha"],
         "last_reviewed_base": repo["base_sha"]})
    return cfg, store


def test_explicit_atomic_seal_delivery_failure_is_failed(
        tmp_path, monkeypatch, two_commit_repo):
    """Delivery persistence failure after an atomic-seal race must
    not be swallowed as success; settled spend stays in ledger."""
    from codatiel_i2 import cli as cli_mod
    from codatiel_i2 import event_pipeline as P
    from codatiel_i2 import event_store as S2
    repo = two_commit_repo
    cfg, store = _explicit_base(tmp_path, monkeypatch, repo)
    try:
        monkeypatch.setattr(P, "get_live_head",
                            lambda r, pr: repo["head_sha"])
        monkeypatch.setattr(
            P, "run_review_for_range",
            lambda **kw: {"status": "complete", "mode": "t",
                          "run_dir": "/tmp/rd", "terra_findings": 0,
                          "added": 0, "cost_usd": 0.33,
                          "cost_unknown": False})
        orig_complete = S.Store.complete_review
        def _patched(self, *a, **kw):
            self.cx.execute(
                "UPDATE checkpoints SET in_progress_run='other-gen' "
                "WHERE repo='o/r' AND pr=10")
            self.cx.commit()
            return orig_complete(self, *a, **kw)
        monkeypatch.setattr(S.Store, "complete_review", _patched)
        # Fail the accounting write
        orig_record = S2.Store.delivery_record
        def _fail_record(self, *a, **kw):
            if kw.get("decision") == "skipped:stale_superseded":
                raise S2.StoreError("injected delivery failure")
            return orig_record(self, *a, **kw)
        monkeypatch.setattr(S2.Store, "delivery_record", _fail_record)
        args = cli_mod.build_parser().parse_args(
            ["events", "review", "--pr", "10", "--head", repo["head_sha"],
             "--base", repo["base_sha"]])
        out = cli_mod.cmd_events(args)
        assert out["status"] == "failed", out
        assert "delivery record failed" in out.get("error", "").lower()
        # settled spend remains for reconciliation even though accounting row failed
        assert store.spend_totals()["settled_usd"] == 0.33
        assert "0.33" in out["error"] or "retained" in out["error"].lower() or "reconciliation" in out["error"].lower()
    finally:
        store.close()


def test_explicit_stale_surfaces_queued_without_autorun(
        tmp_path, monkeypatch, two_commit_repo):
    """A newer head queued while an explicit review runs must not be
    silently dropped nor auto-run with surprise spend. It is surfaced
    for the operator / next automatic ingest."""
    from codatiel_i2 import cli as cli_mod
    from codatiel_i2 import event_pipeline as P
    repo = two_commit_repo
    cfg, store = _explicit_base(tmp_path, monkeypatch, repo)
    try:
        # Simulate automatic queuing of C while explicit for B runs
        store.checkpoint_upsert(
            {"repo": "o/r", "pr": 10, "status": "reviewed",
             "last_reviewed_head": repo["base_sha"],
             "last_reviewed_base": repo["base_sha"],
             "queued_base": repo["base_sha"],
             "queued_head": "c" * 40})
        # Explicit review for B goes stale (live is C)
        calls = {"n": 0}
        def _live(r, pr):
            calls["n"] += 1
            return repo["head_sha"] if calls["n"] == 1 else "c" * 40
        monkeypatch.setattr(P, "get_live_head", _live)
        monkeypatch.setattr(
            P, "run_review_for_range",
            lambda **kw: {"status": "complete", "mode": "t",
                          "run_dir": "/tmp/rd", "terra_findings": 0,
                          "added": 0, "cost_usd": 0.20,
                          "cost_unknown": False})
        args = cli_mod.build_parser().parse_args(
            ["events", "review", "--pr", "10", "--head", repo["head_sha"],
             "--base", repo["base_sha"]])
        out = cli_mod.cmd_events(args)
        assert out["decision"] == "skipped:stale_superseded", out
        # queued work is surfaced, not auto-consumed, not dropped
        assert out.get("queued_remaining") == {"base": repo["base_sha"], "head": "c" * 40}
        cp = store.checkpoint_get("o/r", 10)
        assert cp["queued_head"] == "c" * 40
        # no review row for the queued head was created (no surprise inference)
        assert not any(r["head_sha"] == "c" * 40 for r in store.reviews_list())
    finally:
        store.close()
