"""Event-driven incremental review pipeline: deterministic verification.

No paid inference: scripted models through the real SDK runtime, real
git repos, fixture transports. Covers the operational contract:
restart/duplicate delivery, concurrent updates, same-SHA metadata,
draft-to-ready, rebase/force-push/divergence, truncation, partial
failure, ceiling+reset, no-historical-after-enable, pipeline switch.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import event_pipeline as P  # noqa: E402
from codatiel_i2 import event_store as S  # noqa: E402
from codatiel_i2 import runtime_sdk  # noqa: E402

REPO = "o/r"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def chain(tmp_path: Path) -> dict:
    """A -> B -> C -> D plus rebase/divergence fixtures. Returns SHAs."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "evt@t")
    _git(root, "config", "user.name", "evt")
    (root / "app.py").write_text("LIMIT = 100\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "A")
    a = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 10\n")
    (root / "feat.py").write_text("X = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "B")
    b = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 5\n")
    _git(root, "commit", "-qam", "C")
    c = _git(root, "rev-parse", "HEAD")
    (root / "extra.py").write_text("Y = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "D")
    d = _git(root, "rev-parse", "HEAD")
    return {"root": root, "A": a, "B": b, "C": c, "D": d}


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: Path,
         cap: str = "20", pipeline: str = "terra_only") -> None:
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", cap)
    monkeypatch.setenv("CODATIEL_TEST_TO", "test-ops@example.com")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "codatiel-test@example.com")
    monkeypatch.setenv("CODATIEL_TEST_REPO_PATHS", f"{REPO}={root}")
    monkeypatch.setenv("REVIEW_PIPELINE", pipeline)
    monkeypatch.setenv("CODATIEL_EVENTS_TRANSPORT", "fixture")
    monkeypatch.delenv("CODATIEL_WEBHOOK_SECRET", raising=False)


@pytest.fixture
def cfg_store(chain: dict, tmp_path: Path,
              monkeypatch: pytest.MonkeyPatch):
    _env(monkeypatch, tmp_path, chain["root"])
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
    yield cfg, store
    store.close()


def _terra_factory(final: dict | None = None, path: str = "app.py"):
    text = json.dumps(final if final is not None else
                      {"status": "silence", "summary": "ok", "findings": []})

    def _mk(role: str):
        script = [runtime_sdk.scripted_tool_call(
            call_id="t1", name="read_file",
            arguments={"path": path, "revision": "head",
                       "start_line": 1, "end_line": 0},
            response_id="r1"),
            runtime_sdk.scripted_final(text, response_id="f1")]
        return runtime_sdk.ScriptedModel(script), "scripted:terra-test"
    return _mk


def _patch_additive(monkeypatch: pytest.MonkeyPatch, luna_text: str):
    from codatiel_i2 import providers as providers_mod
    from agents.model_settings import ModelSettings
    terra = runtime_sdk.ScriptedModel([
        runtime_sdk.scripted_tool_call(
            call_id="t1", name="read_file",
            arguments={"path": "app.py", "revision": "head",
                       "start_line": 1, "end_line": 0},
            response_id="r1"),
        runtime_sdk.scripted_final(
            json.dumps({"status": "silence", "summary": "ok",
                        "findings": []}), response_id="f1")])
    luna = runtime_sdk.ScriptedModel(
        [runtime_sdk.scripted_final(luna_text, response_id="f2")])
    real = providers_mod.build_model

    def _fake(name: str):
        if name == "terra":
            return terra, ModelSettings(store=False)
        if name == "luna":
            return luna, ModelSettings(store=False, max_tokens=8000)
        return real(name)
    monkeypatch.setattr(providers_mod, "build_model", _fake)
    monkeypatch.setenv("OPENAI_API_KEY", "test")


def pr_event(action: str, number: int, base: str, head: str,
             draft: bool = False, merged: bool = False,
             created: str = "9999-01-01T00:00:00Z",
             branch: str = "f") -> dict:
    state = "closed" if (action == "closed" or merged) else "open"
    return {"action": action, "repository": {"full_name": REPO},
            "pull_request": {
                "number": number, "state": state, "draft": draft,
                "merged": merged, "created_at": created,
                "base": {"sha": base}, "head": {"sha": head, "ref": branch}}}


def push_event(branch: str, before: str, after: str, pr_number=None,
               forced: bool = False) -> dict:
    body = {"ref": f"refs/heads/{branch}", "before": before,
            "after": after, "forced": forced,
            "repository": {"full_name": REPO}, "commits": [{"id": after}]}
    if pr_number is not None:
        body["pr_number"] = pr_number
    return body


def _ingest(cfg, store, raw: dict, did: str, **kw):
    ev, err = P.normalize_event(raw, did)
    assert err == "", err
    return P.process_event(cfg=cfg, store=store, ev=ev, **kw)


def _runs(cfg) -> list[Path]:
    return sorted((cfg.state_dir / "runs").rglob("run.json"))


# -- config guards ------------------------------------------------------------
def test_env_required(monkeypatch):
    monkeypatch.delenv("CODATIEL_ENV", raising=False)
    with pytest.raises(S.StoreError, match="CODATIEL_ENV"):
        P.config_from_env()


def test_project_cap_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", "20")
    monkeypatch.setenv("CODATIEL_DEV_SPEND_CAP_USD", "15")
    with pytest.raises(S.StoreError, match="exceeds"):
        P.config_from_env()
    monkeypatch.setenv("CODATIEL_DEV_SPEND_CAP_USD", "5")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(tmp_path / "s"))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_TEST_TO", "a@b.c")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "s@b.c")
    cfg = P.config_from_env()
    assert cfg.spend_cap_usd == 20.0


def test_pipeline_mode_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("REVIEW_PIPELINE", "bogus")
    with pytest.raises(S.StoreError, match="REVIEW_PIPELINE"):
        P.config_from_env()


def test_signatures(monkeypatch, tmp_path):
    body = b'{"a":1}'
    monkeypatch.delenv("CODATIEL_WEBHOOK_SECRET", raising=False)
    ok, _ = P.verify_signature(body, None)
    assert ok
    monkeypatch.setenv("CODATIEL_WEBHOOK_SECRET", "s3cret")
    ok, why = P.verify_signature(body, None)
    assert not ok and "missing" in why
    ok, why = P.verify_signature(body, "sha256=dead")
    assert not ok and "mismatch" in why
    import hashlib
    import hmac
    good = "sha256=" + hmac.new(b"s3cret", body,
                                hashlib.sha256).hexdigest()
    ok, _ = P.verify_signature(body, good)
    assert ok


# -- store semantics ------------------------------------------------------------
def test_delivery_replay_and_claim(tmp_path):
    st = S.Store(tmp_path / "e.sqlite")
    row = st.delivery_record(delivery_id="d1", event_type="pull_request",
                             action="opened", repo=REPO, pr=1,
                             head_sha="ab" * 20, decision="completed",
                             reason="ok", cost_usd=0.0)
    assert row["decision"] == "completed"
    again = st.delivery_record(
        delivery_id="d1", event_type="pull_request", action="opened",
        repo=REPO, pr=1, head_sha="ab" * 20, decision="completed",
        reason="different", cost_usd=9.0)
    assert again["reason"] == "ok" and again["cost_usd"] == 0.0
    assert st.claim_for_review(REPO, 7, base="a" * 40,
                              head="b" * 40) == "claimed"
    assert st.claim_for_review(REPO, 7, base="a" * 40,
                              head="b" * 40) == "already"
    assert st.claim_for_review(REPO, 7, base="a" * 40,
                              head="c" * 40) == "queued"
    out = st.complete_review(REPO, 7, base="a" * 40, head="b" * 40,
                             diff_sha="d", files=1, terra_findings=0,
                             luna_added=0, cost_usd=0.0,
                             cost_unknown=False, run_dir="r",
                             result="complete")
    assert out["queued"] == ("a" * 40, "c" * 40)
    st.close()


# -- no historical processing after first startup -----------------------------
def test_no_historical_after_enable(cfg_store, chain):
    cfg, store = cfg_store
    row = _ingest(cfg, store, pr_event(
        "opened", 11, chain["A"], chain["B"],
        created="2020-05-05T00:00:00Z"), "h1")
    assert row["decision"] == "skipped:historical"
    assert _runs(cfg) == [] and store.spend_totals()["settled_usd"] == 0.0


def test_grandfather_then_incremental(cfg_store, chain, tmp_path,
                                      monkeypatch):
    cfg, store = cfg_store
    _env(monkeypatch, tmp_path, chain["root"])
    cfg2 = P.config_from_env()
    cfg2.state_dir = tmp_path / "estate2"
    cfg2.state_dir.mkdir(parents=True, exist_ok=True)
    store2 = S.Store(cfg2.state_dir / "events.sqlite")
    rows = [{"number": 7, "title": "old", "headRefOid": chain["B"],
             "baseRefOid": chain["A"], "headRefName": "f",
             "baseRefName": "main",
             "url": f"https://github.com/{REPO}/pull/7",
             "createdAt": "2020-01-01T00:00:00Z",
             "updatedAt": "2020-01-02T00:00:00Z", "state": "OPEN",
             "isDraft": False, "mergedAt": ""}]
    out = P.enable(cfg=cfg2, store=store2,
                   discover=lambda r: (rows, ""))
    assert out["grandfathered"] == [f"{REPO}#7"]
    assert _runs(cfg2) == []
    row = _ingest(cfg2, store2, pr_event(
        "synchronize", 7, chain["A"], chain["B"],
        created="2020-01-01T00:00:00Z"), "g1",
        model_factory=_terra_factory())
    assert row["decision"] == "skipped:unchanged", row
    # An old PR missed by discovery has no checkpoint: still historical.
    row = _ingest(cfg2, store2, pr_event(
        "opened", 8, chain["A"], chain["B"],
        created="2020-01-01T00:00:00Z"), "g0",
        model_factory=_terra_factory())
    assert row["decision"] == "skipped:historical", row
    row = _ingest(cfg2, store2, pr_event(
        "synchronize", 7, chain["A"], chain["C"],
        created="2020-01-01T00:00:00Z"), "g2",
        model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    cp = store2.checkpoint_get(REPO, 7)
    assert cp["last_reviewed_head"] == chain["C"] and \
        cp["last_reviewed_base"] == chain["A"]  # PR base identity
    ranges = [r for r in store2.reviews_list() if r["pr"] == 7]
    assert ranges and ranges[0]["base_sha"] == chain["B"]  # range start
    store2.close()


def test_new_pr_full_review_terra_only(cfg_store, chain):
    cfg, store = cfg_store
    row = _ingest(cfg, store,
                  pr_event("opened", 1, chain["A"], chain["B"]), "n1",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 1
    cp = store.checkpoint_get(REPO, 1)
    assert cp["status"] == "reviewed"
    assert cp["last_reviewed_head"] == chain["B"]
    assert cp["last_reviewed_base"] == chain["A"]
    assert cp["reviewed_files"] == 2  # app.py + feat.py
    assert not list((cfg.state_dir / "runs").rglob("additive.json"))
    dash = P.dashboard(cfg=cfg, store=store)
    assert dash["prs_reviewed"] == 1 and dash["reviews_completed"] == 1
    assert dash["terra_findings_total"] == 0


def test_additive_mode_merges_luna(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    cfg.pipeline = "terra_luna_additive"
    luna_f = [{"title": "limit lowered without caller updates",
               "severity": "P2", "path": "app.py", "revision": "head",
               "line_start": 1, "line_end": 1, "evidence": "LIMIT = 10",
               "why_pr_introduced": "changed"}]
    _patch_additive(monkeypatch, "notes\n```json\n" +
                    json.dumps({"findings": luna_f}) + "\n```\n")
    row = _ingest(cfg, store,
                  pr_event("opened", 2, chain["A"], chain["B"]), "a1")
    assert row["decision"] == "completed", row
    merged = list((cfg.state_dir / "runs").rglob("additive.json"))
    assert len(merged) == 1
    doc = json.loads(merged[0].read_text())
    assert len(doc["luna_added"]) == 1
    assert doc["luna_added"][0]["provenance"] == "luna-blind-sweep"
    cp = store.checkpoint_get(REPO, 2)
    assert cp["luna_added"] == 1
    dash = P.dashboard(cfg=cfg, store=store)
    assert dash["luna_exclusive_total"] == 1


# -- replays, restarts, metadata ----------------------------------------------
def test_duplicate_delivery_no_respend(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    r1 = _ingest(cfg, store, pr_event("opened", 3, chain["A"], chain["B"]),
                 "dup", **kw)
    assert r1["decision"] == "completed"
    spent = store.spend_totals()["settled_usd"]
    r2 = _ingest(cfg, store, pr_event("opened", 3, chain["A"], chain["B"]),
                 "dup", **kw)
    assert r2["decision"] == "skipped:replay"
    assert len(_runs(cfg)) == 1
    assert store.spend_totals()["settled_usd"] == spent


def test_restart_recovery_no_respend(cfg_store, chain):
    cfg, store = cfg_store
    store.claim_for_review(REPO, 4, base=chain["A"], head=chain["B"])
    store.close()
    cfg2_state = cfg.state_dir
    store2 = S.Store(cfg2_state / "events.sqlite")
    try:
        cfg.stale_after_s = 0.0  # the crashed worker never comes back
        out = P.recover(cfg=cfg, store=store2)
        assert out["interrupted"] == [f"{REPO}#4"]
        row = _ingest(cfg, store2, pr_event(
            "synchronize", 4, chain["A"], chain["B"]), "r1",
            model_factory=_terra_factory())
        assert row["decision"].startswith("needs_attention"), row
        assert _runs(cfg) == []
    finally:
        store2.close()


def test_same_sha_metadata_and_reopen(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    _ingest(cfg, store, pr_event("opened", 5, chain["A"], chain["B"]),
            "m0", **kw)
    for did, raw in (
            ("m1", pr_event("labeled", 5, chain["A"], chain["B"])),
            ("m2", pr_event("edited", 5, chain["A"], chain["B"])),
            ("m3", pr_event("synchronize", 5, chain["A"], chain["B"])),
            ("m4", pr_event("closed", 5, chain["A"], chain["B"])),
            ("m5", pr_event("reopened", 5, chain["A"], chain["B"]))):
        row = _ingest(cfg, store, raw, did, **kw)
        assert row["decision"].split(":")[0] == "skipped", (did, row)
    assert len(_runs(cfg)) == 1
    cp = store.checkpoint_get(REPO, 5)
    assert cp["updated_ts"]  # informational only
    assert cp["last_reviewed_head"] == chain["B"]
    # Content-keyed, not timestamp-keyed: touching updated_ts changes nothing.
    store.checkpoint_upsert({**cp, "updated_ts": 0.0})
    row = _ingest(cfg, store, pr_event(
        "synchronize", 5, chain["A"], chain["B"]), "m6", **kw)
    assert row["decision"] == "skipped:unchanged"


def test_reopened_with_new_head_reviews_delta(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    _ingest(cfg, store, pr_event("opened", 6, chain["A"], chain["B"]),
            "o1", **kw)
    _ingest(cfg, store, pr_event("closed", 6, chain["A"], chain["B"]),
            "o2", **kw)
    row = _ingest(cfg, store, pr_event(
        "reopened", 6, chain["A"], chain["C"]), "o3", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 2
    assert store.checkpoint_get(REPO, 6)["last_reviewed_head"] == chain["C"]


# -- incremental, batching, queue ----------------------------------------------
def test_three_commits_one_combined_review(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    _ingest(cfg, store, pr_event("opened", 8, chain["A"], chain["B"]),
            "b0", **kw)
    row = _ingest(cfg, store, push_event("f", chain["B"], chain["D"],
                                        pr_number=8), "b1", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 2  # full + exactly one incremental
    cp = store.checkpoint_get(REPO, 8)
    assert cp["last_reviewed_head"] == chain["D"]
    assert cp["last_reviewed_base"] == chain["B"]


def test_concurrent_updates_queue_without_mixing(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    _ingest(cfg, store, pr_event("opened", 9, chain["A"], chain["B"]),
            "c0", **kw)
    # Worker 1 pins C..D range (simulated live worker).
    assert store.claim_for_review(REPO, 9, base=chain["C"],
                                  head=chain["D"]) == "claimed"
    # Worker 2 retries the identical pinned range: no second spend.
    row = _ingest(cfg, store, push_event("f", chain["C"], chain["D"],
                                        pr_number=9), "c1", **kw)
    assert row["decision"] == "skipped:unchanged", row
    assert len(_runs(cfg)) == 1
    # Worker 2 reports a newer head while worker 1 runs: queued, no run.
    _git(chain["root"], "checkout", "-qb", "w2", chain["D"])
    (chain["root"] / "w2.py").write_text("W = 1\n")
    _git(chain["root"], "add", "-A")
    _git(chain["root"], "commit", "-qm", "E")
    e = _git(chain["root"], "rev-parse", "HEAD")
    row = _ingest(cfg, store, push_event("f", chain["D"], e, pr_number=9),
                  "c2", **kw)
    assert row["decision"] == "queued", row
    assert len(_runs(cfg)) == 1
    # Worker 1 seals D; the queued delta is recorded for a separate run.
    out = store.complete_review(REPO, 9, base=chain["C"], head=chain["D"],
                                diff_sha="d", files=1, terra_findings=0,
                                luna_added=0, cost_usd=0.0,
                                cost_unknown=False, run_dir="r",
                                result="complete")
    assert out["queued"] == (chain["D"], e)


def test_queued_delta_drains_as_separate_run(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    _ingest(cfg, store, pr_event("opened", 10, chain["A"], chain["B"]),
            "d0", **kw)
    # A queued newer delta that landed just before the claim.
    store.checkpoint_upsert({
        **store.checkpoint_get(REPO, 10),
        "queued_base": chain["B"], "queued_head": chain["C"]})
    row = _ingest(cfg, store, push_event("f", chain["B"], chain["B"],
                                        pr_number=10), "d1", **kw)
    # B..B is empty -> unchanged for the event itself...
    assert row["decision"] == "skipped:unchanged", row
    # ...but the pre-existing queue is NOT auto-run by an empty event.
    assert len(_runs(cfg)) == 1
    # A real newer event drains the recorded queue after its own review.
    store.checkpoint_upsert({
        **store.checkpoint_get(REPO, 10),
        "queued_base": chain["C"], "queued_head": chain["D"]})
    row = _ingest(cfg, store, push_event("f", chain["B"], chain["C"],
                                        pr_number=10), "d2", **kw)
    assert row["decision"] == "completed", row
    assert row.get("drained") == "completed", row
    assert len(_runs(cfg)) == 3  # B, C, then drained D
    assert store.checkpoint_get(REPO, 10)["last_reviewed_head"] == chain["D"]


# -- rebase / force-push / divergence / truncation ------------------------------
def test_rebase_reviews_actual_patch_changes(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    root = chain["root"]
    _ingest(cfg, store, pr_event("opened", 12, chain["A"], chain["B"]),
            "r0", **kw)
    _git(root, "checkout", "-qb", "rb", chain["A"])
    (root / "base.txt").write_text("new base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "A2")
    a2 = _git(root, "rev-parse", "HEAD")
    _git(root, "cherry-pick", chain["B"])
    b2 = _git(root, "rev-parse", "HEAD")
    row = _ingest(cfg, store, pr_event(
        "synchronize", 12, a2, b2, branch="rb"), "r1", **kw)
    assert row["decision"] == "completed", row
    cp = store.checkpoint_get(REPO, 12)
    assert cp["last_reviewed_base"] == a2 and \
        cp["last_reviewed_head"] == b2


def test_same_tree_rewrite_is_unchanged(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    root = chain["root"]
    _ingest(cfg, store, pr_event("opened", 13, chain["A"], chain["B"]),
            "s0", **kw)
    _git(root, "checkout", "-qb", "same", chain["B"])
    _git(root, "commit", "-q", "--allow-empty", "-m", "same tree")
    b3 = _git(root, "rev-parse", "HEAD")
    assert b3 != chain["B"]
    row = _ingest(cfg, store, pr_event("synchronize", 13, chain["A"], b3),
                  "s1", **kw)
    assert row["decision"] == "skipped:unchanged", row
    assert len(_runs(cfg)) == 1


def test_diverged_branch_reviews_interdiff(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    root = chain["root"]
    _ingest(cfg, store, pr_event("opened", 14, chain["A"], chain["B"]),
            "v0", **kw)
    _git(root, "checkout", "-qb", "div", chain["A"])
    (root / "div.py").write_text("D = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "X")
    x = _git(root, "rev-parse", "HEAD")
    row = _ingest(cfg, store, pr_event("synchronize", 14, chain["A"], x),
                  "v1", **kw)
    assert row["decision"] == "completed", row
    cp = store.checkpoint_get(REPO, 14)
    assert cp["last_reviewed_head"] == x


def test_truncation_and_failed_compare_spend_nothing(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    root = chain["root"]
    _git(root, "checkout", "-qb", "big", chain["B"])
    (root / "bulk").mkdir()
    for i in range(310):
        (root / "bulk" / f"f{i:03d}.txt").write_text(f"row {i}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "bulk")
    bulk = _git(root, "rev-parse", "HEAD")
    row = _ingest(cfg, store, pr_event("opened", 15, chain["A"], bulk),
                  "t0", **kw)
    assert row["decision"] == "needs_attention:truncated", row
    assert _runs(cfg) == []
    row = _ingest(cfg, store, pr_event(
        "synchronize", 15, chain["A"], "f" * 40), "t1", **kw)
    assert row["decision"] == "needs_attention:unknown_endpoint", row
    # A from-SHA that no longer exists is never an empty diff either.
    store.checkpoint_upsert({"repo": REPO, "pr": 16, "status": "reviewed",
                             "last_reviewed_head": "e" * 40,
                             "last_reviewed_base": chain["A"]})
    row = _ingest(cfg, store, pr_event(
        "synchronize", 16, chain["A"], chain["C"]), "t2", **kw)
    assert row["decision"] == "needs_attention:unknown_endpoint", row
    assert _runs(cfg) == [] and store.spend_totals()["settled_usd"] == 0.0


def test_compare_range_unit(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "u@t")
    _git(root, "config", "user.name", "u")
    (root / "f.txt").write_text("a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "1")
    one = _git(root, "rev-parse", "HEAD")
    info, err = P.compare_range(root, one, one)
    assert err == "" and info["empty"] and info["files"] == 0
    assert P.compare_range(root, one, "0" * 40)[0] is None
    (root / "f.txt").write_text("b\n")
    _git(root, "commit", "-qam", "2")
    two = _git(root, "rev-parse", "HEAD")
    a, _ = P.compare_range(root, one, two)
    b, _ = P.compare_range(root, one, two)
    assert a["diff_sha"] == b["diff_sha"] and not a["empty"]


# -- partial failure / ceiling / dashboard ---------------------------------------
def test_partial_failure_advances_nothing(cfg_store, chain):
    cfg, store = cfg_store
    bad = runtime_sdk.ScriptedModel(
        [runtime_sdk.scripted_final("not json at all", response_id="f9")])

    def _mk(role: str):
        return bad, "scripted:bad"
    row = _ingest(cfg, store, pr_event("opened", 17, chain["A"], chain["B"]),
                  "f0", model_factory=_mk)
    assert row["decision"] == "failed", row
    cp = store.checkpoint_get(REPO, 17)
    assert not cp["last_reviewed_head"]
    assert not list((cfg.state_dir / "runs").rglob("additive.json"))
    fails = [r for r in store.reviews_list() if r["status"] != "complete"]
    assert len(fails) == 1


def test_additive_keeps_terra_on_luna_failure(cfg_store, chain,
                                             monkeypatch):
    cfg, store = cfg_store
    cfg.pipeline = "terra_luna_additive"
    _patch_additive(monkeypatch, "free text, no findings block")
    row = _ingest(cfg, store, pr_event("opened", 18, chain["A"], chain["B"]),
                  "f1")
    assert row["decision"] == "completed", row
    # Terra's complete review stands; Luna contributed nothing.
    assert not list((cfg.state_dir / "runs").rglob("additive.json"))
    cp = store.checkpoint_get(REPO, 18)
    assert cp["status"] == "reviewed" and cp["luna_added"] == 0


def test_ceiling_stops_and_reset_does_not_backlog(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    _ingest(cfg, store, pr_event("opened", 19, chain["A"], chain["B"]),
            "e0", **kw)
    cfg.spend_cap_usd = 0.5  # reserve ($1) no longer fits
    row = _ingest(cfg, store, pr_event(
        "opened", 20, chain["A"], chain["B"]), "e1", **kw)
    assert row["decision"] == "skipped:ceiling", row
    assert len(_runs(cfg)) == 1
    out = P.reset_spend(store=store)
    assert out["cleared"]["settled_usd"] == 0.0
    cfg.spend_cap_usd = 20.0
    row = _ingest(cfg, store, pr_event(
        "opened", 21, chain["A"], chain["B"]), "e2", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 2  # PR19 + PR21; PR20 never auto-ran
    assert store.delivery_get("e1")["decision"] == "skipped:ceiling"


def test_dashboard_shape(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    _ingest(cfg, store, pr_event("opened", 22, chain["A"], chain["B"]),
            "q0", **kw)
    _ingest(cfg, store, pr_event("labeled", 22, chain["A"], chain["B"]),
            "q1", **kw)
    dash = P.dashboard(cfg=cfg, store=store)
    for key in ("prs_reviewed", "prs_incrementally_reviewed",
                "reviews_completed", "reviews_failed", "skipped_events",
                "needs_attention_events", "historical_replayed_blocked",
                "terra_findings_total", "luna_exclusive_total",
                "cumulative_spend_usd", "avg_cost_per_reviewed_pr_usd",
                "spend_cap_usd"):
        assert key in dash, key
    assert dash["prs_reviewed"] == 1
    assert dash["skipped_events"].get("skipped:metadata") == 1


def test_no_pr_comment_path():
    import re
    src = (Path(__file__).parent.parent / "codatiel_i2"
           / "event_pipeline.py").read_text(encoding="utf-8")
    # No import of, or call into, any PR-comment path. Docstring prose
    # mentioning the prohibition is fine; code references are not.
    for pat in (r"^\s*(import|from)\s+\S*publish",
                r"(?<![\w.])publish\.",
                r"post_to_pr", r"create_comment", r"post_comment"):
        assert not re.search(pat, src, re.M), pat
    assert "deliver_shadow" in src  # private digest is the only channel


# -- draft lifecycle --------------------------------------------------------------
def test_draft_to_ready(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    row = _ingest(cfg, store, pr_event(
        "opened", 23, chain["A"], chain["B"], draft=True), "dr0", **kw)
    assert row["decision"] == "skipped:draft", row
    assert _runs(cfg) == []
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["C"], draft=True), "dr1",
        **kw)
    assert row["decision"] == "skipped:draft", row
    row = _ingest(cfg, store, pr_event(
        "ready_for_review", 23, chain["A"], chain["C"]), "dr2", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 1
    row = _ingest(cfg, store, pr_event(
        "ready_for_review", 23, chain["A"], chain["C"]), "dr3", **kw)
    assert row["decision"] == "skipped:unchanged", row
    assert len(_runs(cfg)) == 1


# -- CLI wiring ----------------------------------------------------------------------
def test_cli_ingest_and_status(cfg_store, chain, monkeypatch, tmp_path):
    from codatiel_i2.cli import build_parser, cmd_events
    from codatiel_i2 import providers as providers_mod
    from agents.model_settings import ModelSettings
    cfg, store = cfg_store
    store.close()
    terra = runtime_sdk.ScriptedModel([
        runtime_sdk.scripted_tool_call(
            call_id="t1", name="read_file",
            arguments={"path": "app.py", "revision": "head",
                       "start_line": 1, "end_line": 0},
            response_id="r1"),
        runtime_sdk.scripted_final(
            json.dumps({"status": "silence", "summary": "ok",
                        "findings": []}), response_id="f1")])
    real = providers_mod.build_model
    monkeypatch.setattr(providers_mod, "build_model",
                        lambda n: (terra, ModelSettings(store=False))
                        if n == "terra" else real(n))
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    payload = tmp_path / "ev.json"
    payload.write_text(json.dumps(
        pr_event("opened", 30, chain["A"], chain["B"])))
    args = build_parser().parse_args(
        ["events", "ingest", "--payload", str(payload),
         "--delivery-id", "cli-1"])
    out = cmd_events(args)
    assert out["status"] == "completed", out
    args = build_parser().parse_args(["events", "status"])
    assert cmd_events(args)["status"] == "ok"
    args = build_parser().parse_args(["events", "dashboard"])
    assert cmd_events(args)["prs_reviewed"] == 1
    # Unknown transport refuses before any work.
    monkeypatch.setenv("CODATIEL_EVENTS_TRANSPORT", "pigeon")
    payload2 = tmp_path / "ev2.json"
    payload2.write_text(json.dumps(
        pr_event("opened", 31, chain["A"], chain["B"])))
    args = build_parser().parse_args(
        ["events", "ingest", "--payload", str(payload2),
         "--delivery-id", "cli-2"])
    assert cmd_events(args)["status"] == "failed"


def test_cli_ingest_rejects_unknown_repo(cfg_store, chain, tmp_path):
    from codatiel_i2.cli import build_parser, cmd_events
    payload = tmp_path / "ev.json"
    body = pr_event("opened", 30, chain["A"], chain["B"])
    body["repository"]["full_name"] = "evil/fork"
    payload.write_text(json.dumps(body))
    args = build_parser().parse_args(
        ["events", "ingest", "--payload", str(payload),
         "--delivery-id", "cli-9"])
    out = cmd_events(args)
    assert out["status"] == "failed" and "not in configured" in out["error"]
