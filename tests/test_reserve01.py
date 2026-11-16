"""RESERVE-RECOVERY-01: ownership-safe reservation recovery.

Each review reservation is attributed to its claim/run (generation
token) and settled or conservatively retained exactly once. No paid
inference, no network: fabricated run dirs, tmp stores, scripted
models only.

Crash matrix: before reservation, after reservation, during inference,
after inference but before settlement; repeated recovery; an old worker
racing a replacement claim; a live worker left untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codatiel_i2 import event_pipeline as P
from codatiel_i2 import event_store as S

REPO = "o/r"
PR = 7
HEAD = "c" * 40


def _env(monkeypatch, tmp_path, root=None, cap="20.0"):
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", cap)
    monkeypatch.setenv("CODATIEL_TEST_TO", "test-ops@example.com")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "codatiel-test@example.com")
    if root is not None:
        monkeypatch.setenv("CODATIEL_TEST_REPO_PATHS", f"{REPO}={root}")
    monkeypatch.setenv("REVIEW_PIPELINE", "terra_only")
    monkeypatch.setenv("CODATIEL_EVENTS_TRANSPORT", "fixture")
    monkeypatch.delenv("CODATIEL_WEBHOOK_SECRET", raising=False)


def _cfg_store(tmp_path, monkeypatch, root=None):
    _env(monkeypatch, tmp_path, root)
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.clock = lambda: 10000.0
    store = S.Store(cfg.state_dir / "events.sqlite")
    P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
    return cfg, store


def _run_dir(state_dir: Path, run_id: str, *, priced=0.0,
             semdup=None, delivery_id="d1") -> Path:
    d = state_dir / "runs" / "scan" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(
        {"run_id": run_id, "head_sha": HEAD}), encoding="utf-8")
    (d / "identity.json").write_text(json.dumps(
        {"run_id": run_id, "delivery_id": delivery_id,
         "head_sha": HEAD, "repo_slug": REPO}), encoding="utf-8")
    ledger = [{"priced_cost_usd": priced, "usage_known": True}] \
        if priced else []
    (d / "metrics.json").write_text(json.dumps(
        {"spend": {"request_ledger": ledger}}), encoding="utf-8")
    if semdup is not None:
        (d / "semdup.json").write_text(
            json.dumps({"spend_usd": semdup}), encoding="utf-8")
    return d


def _claim(store, head=HEAD, did="d1", token="tok-1", now=1000.0):
    store.delivery_record(delivery_id=did, event_type="pull_request",
                          action="synchronize", repo=REPO, pr=PR,
                          head_sha=head, decision="in_progress",
                          reason="claimed", cost_usd=None, detail={},
                          now=now)
    store.checkpoint_upsert({"repo": REPO, "pr": PR, "status": "in_progress",
                             "in_progress_head": head,
                             "in_progress_run": token}, now=now)
    return token


# -- holder lifecycle --------------------------------------------------------

def test_reserve_settle_happy_path(tmp_path):
    st = S.Store(tmp_path / "e.sqlite")
    st.reserve(1.0, holder="tok-1", repo=REPO, pr=PR, head_sha=HEAD,
               delivery_id="d1")
    assert st.spend_totals()["reserved_usd"] == 1.0
    out = st.settle(1.0, 0.11, holder="tok-1")
    assert out["reserved_usd"] == 0.0
    assert out["settled_usd"] == pytest.approx(0.11)
    assert st.reservations_open() == []
    st.close()


def test_double_settle_refused_and_totals_unchanged(tmp_path):
    st = S.Store(tmp_path / "e.sqlite")
    st.reserve(1.0, holder="tok-1", repo=REPO, pr=PR, head_sha=HEAD,
               delivery_id="d1")
    st.settle(1.0, 0.11, holder="tok-1")
    before = st.spend_totals()
    with pytest.raises(S.StoreError):
        st.settle(1.0, 0.11, holder="tok-1")
    assert st.spend_totals() == before
    st.close()


def test_duplicate_reserve_refused(tmp_path):
    st = S.Store(tmp_path / "e.sqlite")
    st.reserve(1.0, holder="tok-1", repo=REPO, pr=PR, head_sha=HEAD,
               delivery_id="d1")
    with pytest.raises(S.StoreError):
        st.reserve(1.0, holder="tok-1", repo=REPO, pr=PR, head_sha=HEAD,
                   delivery_id="d1")
    st.close()


# -- crash matrix ------------------------------------------------------------

def test_recover_before_inference_releases_without_hold(tmp_path,
                                                        monkeypatch):
    cfg, store = _cfg_store(tmp_path, monkeypatch)
    token = _claim(store, token="tok-1", now=1000.0)
    store.reserve(1.0, holder=token, repo=REPO, pr=PR, head_sha=HEAD,
                  delivery_id="d1")
    out = P.recover(cfg=cfg, store=store)
    assert store.spend_totals()["reserved_usd"] == 0.0
    assert store.spend_totals()["settled_usd"] == 0.0
    assert store.spend_totals()["unknown_holds"] == 0.0
    assert store.reservations_open() == []
    assert store.checkpoint_get(REPO, PR)["status"] == "interrupted"
    assert any("tok-1" in m for m in out["reservations_recovered"])
    store.close()


def test_recover_during_inference_settles_proven_and_holds_rest(
        tmp_path, monkeypatch):
    cfg, store = _cfg_store(tmp_path, monkeypatch)
    token = _claim(store, token="tok-1", now=1000.0)
    store.reserve(1.0, holder=token, repo=REPO, pr=PR, head_sha=HEAD,
                  delivery_id="d1")
    _run_dir(cfg.state_dir, "run-1", priced=0.05, delivery_id="d1")
    P.recover(cfg=cfg, store=store)
    totals = store.spend_totals()
    assert totals["reserved_usd"] == 0.0
    assert totals["settled_usd"] == pytest.approx(1.0)  # 0.05 + 0.95 hold
    assert totals["unknown_holds"] == 1.0
    assert store.reservations_open() == []
    store.close()


def test_recover_zero_priced_started_run_holds_all(tmp_path, monkeypatch):
    cfg, store = _cfg_store(tmp_path, monkeypatch)
    token = _claim(store, token="tok-1", now=1000.0)
    store.reserve(1.0, holder=token, repo=REPO, pr=PR, head_sha=HEAD,
                  delivery_id="d1")
    _run_dir(cfg.state_dir, "run-1", delivery_id="d1")  # started, $0 priced
    P.recover(cfg=cfg, store=store)
    totals = store.spend_totals()
    assert totals["settled_usd"] == pytest.approx(1.0)
    assert totals["unknown_holds"] == 1.0
    store.close()


def test_recover_is_idempotent(tmp_path, monkeypatch):
    cfg, store = _cfg_store(tmp_path, monkeypatch)
    token = _claim(store, token="tok-1", now=1000.0)
    store.reserve(1.0, holder=token, repo=REPO, pr=PR, head_sha=HEAD,
                  delivery_id="d1")
    _run_dir(cfg.state_dir, "run-1", priced=0.05, delivery_id="d1")
    P.recover(cfg=cfg, store=store)
    before = store.spend_totals()
    out = P.recover(cfg=cfg, store=store)
    assert store.spend_totals() == before
    assert out["reservations_recovered"] == []
    store.close()


def test_recover_skips_live_worker(tmp_path, monkeypatch):
    cfg, store = _cfg_store(tmp_path, monkeypatch)
    cfg.clock = lambda: 1100.0  # fresh heartbeat: worker may be alive
    token = _claim(store, token="tok-1", now=1000.0)
    store.reserve(1.0, holder=token, repo=REPO, pr=PR, head_sha=HEAD,
                  delivery_id="d1")
    P.recover(cfg=cfg, store=store)
    assert store.spend_totals()["reserved_usd"] == 1.0
    assert store.checkpoint_get(REPO, PR)["status"] == "in_progress"
    assert len(store.reservations_open()) == 1
    store.close()


def test_recover_releases_superseded_holder_keeps_replacement(
        tmp_path, monkeypatch):
    cfg, store = _cfg_store(tmp_path, monkeypatch)
    old = _claim(store, token="tok-old", now=1000.0)
    store.reserve(1.0, holder=old, repo=REPO, pr=PR, head_sha=HEAD,
                  delivery_id="d1")
    _run_dir(cfg.state_dir, "run-old", priced=0.02, delivery_id="d1")
    # Replacement claim takes over with a fresh token and reserve.
    store.checkpoint_upsert({"repo": REPO, "pr": PR,
                             "status": "in_progress",
                             "in_progress_head": HEAD,
                             "in_progress_run": "tok-new"}, now=9000.0)
    store.reserve(1.0, holder="tok-new", repo=REPO, pr=PR, head_sha=HEAD,
                  delivery_id="d2")
    P.recover(cfg=cfg, store=store)
    totals = store.spend_totals()
    assert totals["reserved_usd"] == 1.0  # only the live claim's hold
    assert totals["settled_usd"] == pytest.approx(1.0)  # 0.02 + 0.98 hold
    cp = store.checkpoint_get(REPO, PR)
    assert cp["status"] == "in_progress"
    assert cp["in_progress_run"] == "tok-new"
    assert [r["holder"] for r in store.reservations_open()] == ["tok-new"]
    store.close()


# -- accounting --------------------------------------------------------------

def test_settled_never_rewritten_or_double_counted(tmp_path):
    st = S.Store(tmp_path / "e.sqlite")
    st.reserve(1.0, holder="a", repo=REPO, pr=PR, head_sha=HEAD,
               delivery_id="d1")
    st.settle(1.0, 0.4, holder="a")
    st.reserve(1.0, holder="b", repo=REPO, pr=PR, head_sha=HEAD,
               delivery_id="d2")
    st.settle(1.0, 0.6, holder="b")
    assert st.spend_totals()["settled_usd"] == pytest.approx(1.0)
    assert st.spend_totals()["unknown_holds"] == 0.0
    st.close()


def test_admission_ledger_untouched_by_recovery(tmp_path, monkeypatch):
    from codatiel_i2 import operator_config as opcfg
    cfg, store = _cfg_store(tmp_path, monkeypatch)
    token = _claim(store, token="tok-1", now=1000.0)
    store.reserve(1.0, holder=token, repo=REPO, pr=PR, head_sha=HEAD,
                  delivery_id="d1")
    before = opcfg.spend_on_day(store, "1970-01-01")
    P.recover(cfg=cfg, store=store)
    assert opcfg.spend_on_day(store, "1970-01-01") == before
    store.close()


def test_full_review_closes_its_holder(tmp_path, monkeypatch):
    import subprocess
    from codatiel_i2 import runtime_sdk
    root = tmp_path / "repo"
    root.mkdir()
    for a in (["init", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *a], check=True,
                       capture_output=True, timeout=60)
    (root / "app.py").write_text("LIMIT = 100\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "A"],
                   check=True, capture_output=True, timeout=60)
    a = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                       check=True, capture_output=True, text=True,
                       timeout=60).stdout.strip()
    (root / "app.py").write_text("LIMIT = 5\n")
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "B"],
                   check=True, capture_output=True, timeout=60)
    b = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                       check=True, capture_output=True, text=True,
                       timeout=60).stdout.strip()
    cfg, store = _cfg_store(tmp_path, monkeypatch, root)
    real_run = P.run_review_for_range

    def _spy(*args, **kwargs):
        text = '{"status": "silence", "summary": "ok", "findings": []}'
        script = [runtime_sdk.scripted_tool_call(
            call_id="t1", name="read_file",
            arguments={"path": "app.py", "revision": "head",
                       "start_line": 1, "end_line": 0}, response_id="r1"),
            runtime_sdk.scripted_final(text, response_id="f1")]

        def _mk(role: str):
            return runtime_sdk.ScriptedModel(script), "scripted:t"
        kwargs["model_factory"] = _mk
        return real_run(*args, **kwargs)

    monkeypatch.setattr(P, "run_review_for_range", _spy)
    raw = {"action": "synchronize", "repository": {"full_name": REPO},
           "pull_request": {"number": PR, "state": "open", "draft": False,
                            "merged": False,
                            "created_at": "9999-01-01T00:00:00Z",
                            "base": {"sha": a},
                            "head": {"sha": b, "ref": "f"}}}
    ev, err = P.normalize_event(raw, "did-1")
    assert err == ""
    out = P.process_event(cfg=cfg, store=store, ev=ev)
    assert out["decision"] == "completed"
    assert store.reservations_open() == []
    assert store.spend_totals()["reserved_usd"] == 0.0
    store.close()
