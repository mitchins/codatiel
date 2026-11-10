"""SEMDUP-08 integrated stage: deterministic verification.

No paid inference anywhere: the Terra/Luna judgment is exercised with
injected fakes through the real evidence/validation path on real git
repos with real index builds (embeddings disabled -> deterministic
channels only). Covers: env enablement, unavailable-on-missing-index,
direct retrieval into Terra/Luna without Gemma, PR cap, dedup against
the ordinary review, email rendering, and TEST-pipeline invariance.
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
from codatiel_i2 import service as service_mod  # noqa: E402
from codatiel_i2.semdup_dev import stage as ST  # noqa: E402

REPO = "o/r"

EXISTING = '''"""Money formatting helpers."""


def format_money(amount, places=2):
    """Format amount with thousands separators for display."""
    quantized = round(float(amount), places)
    return f"{quantized:,.{places}f}"
'''

NEW_DUP = '''"""Price formatting for the checkout page."""


def format_price(amount, places=2):
    """Format amount with thousands separators for display."""
    quantized = round(float(amount), places)
    return f"{quantized:,.{places}f}"
'''


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def dupe_repo(tmp_path: Path) -> dict:
    """Base has format_money; head adds a near-duplicate format_price."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "existing.py").write_text(EXISTING)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "new.py").write_text(NEW_DUP)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add duplicate")
    head = _git(root, "rev-parse", "HEAD")
    return {"root": root, "base": base, "head": head}


def _fake_assess(*, evidence, head_rev, target_kind, tools, model,
                 model_settings, ledger, ctx=None, call=None) -> dict:
    return {"ok": True,
            "terra": {"overlap": "near-duplicate",
                      "relationship": "same behavior",
                      "opportunity": "direct_reuse",
                      "shared_component": "money formatting",
                      "differences": ["name only"]},
            "transcript": {"tool_calls": []}}


def _fake_edit(*, package, proposed_comment, tools, model,
               model_settings, ledger, ctx=None, call=None,
               max_turns=4) -> dict:
    return {"ok": True,
            "editor": {"decision": "KEEP",
                       "revised_comment": proposed_comment,
                       "reason": "concrete reuse"},
            "transcript": {"tool_calls": []}}


# -- enablement (pipeline config; TEST forced off) ---------------------

def _dev_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
             root: Path) -> None:
    monkeypatch.setenv("CODATIEL_ENV", "dev")
    monkeypatch.setenv("CODATIEL_DEV_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_DEV_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_DEV_SPEND_CAP_USD", "5")
    monkeypatch.setenv("CODATIEL_DEV_TO", "t@example.com")
    monkeypatch.setenv("CODATIEL_DEV_SENDER", "s@example.com")
    monkeypatch.setenv("CODATIEL_DEV_REPO_PATHS", f"{REPO}={root}")


def test_stage_dev_default_on(tmp_path: Path, dupe_repo: dict,
                              monkeypatch: pytest.MonkeyPatch):
    _dev_env(monkeypatch, tmp_path, dupe_repo["root"])
    cfg = P.config_from_env()
    assert cfg.semdup_enabled is True
    assert cfg.semdup_ceiling_usd == pytest.approx(0.20)
    assert cfg.semdup_max_pairs == 4


def test_stage_test_always_off(tmp_path: Path, dupe_repo: dict,
                               monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", "20")
    monkeypatch.setenv("CODATIEL_TEST_TO", "t@example.com")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "s@example.com")
    monkeypatch.setenv("CODATIEL_TEST_REPO_PATHS",
                       f"{REPO}={dupe_repo['root']}")
    monkeypatch.setenv("CODATIEL_DEV_SEMDUP", "1")
    cfg = P.config_from_env()
    assert cfg.semdup_enabled is False


def test_stage_opt_out(tmp_path: Path, dupe_repo: dict,
                       monkeypatch: pytest.MonkeyPatch):
    _dev_env(monkeypatch, tmp_path, dupe_repo["root"])
    monkeypatch.setenv("CODATIEL_DEV_SEMDUP", "0")
    assert P.config_from_env().semdup_enabled is False


def test_stage_bad_numbers_fail_closed(tmp_path: Path, dupe_repo: dict,
                                        monkeypatch: pytest.MonkeyPatch):
    _dev_env(monkeypatch, tmp_path, dupe_repo["root"])
    monkeypatch.setenv("CODATIEL_DEV_SEMDUP_CEILING_USD", "nonsense")
    with pytest.raises(S.StoreError):
        P.config_from_env()


# -- unavailable paths (never raise, never fake zero) -------------------

def test_run_stage_bad_rev_unavailable(tmp_path: Path, dupe_repo: dict):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev="0" * 40, head_rev=dupe_repo["head"], run_dir=run_dir,
        state_dir=tmp_path / "state", allow_embeddings=False)
    assert out["status"] == "unavailable"
    assert out["published"] == []
    assert "base index unavailable" in out["reason"]
    assert (run_dir / "semdup.json").exists()


def test_run_stage_no_backends_unavailable(tmp_path: Path, dupe_repo: dict,
                                           monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False)
    assert out["status"] in ("unavailable", "ok")
    if out["status"] == "unavailable":
        # Inability is explicit, never a clean-looking zero.
        assert out["published"] == []
        assert "backend" in out["reason"]
    assert (run_dir / "semdup.json").exists()


# -- direct retrieval into Terra/Luna (no Gemma) ------------------------

def test_run_stage_direct_pairs_judged(tmp_path: Path, dupe_repo: dict,
                                           monkeypatch: pytest.MonkeyPatch):
    # Fakes replace the model calls, but backend resolution still runs:
    # a dummy key keeps this hermetic with zero network use.
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, assess_fn=_fake_assess,
        edit_fn=_fake_edit)
    assert out["status"] == "ok", out
    assert out["coverage"].startswith("reduced")  # deterministic only
    assert len(out["targets"]) >= 1
    assert out["pairs_examined"] >= 1  # retrieval really ran
    assert len(out["published"]) <= ST.PR_FINDING_CAP
    assert (run_dir / "semdup.json").exists()
    # Approved rows carry exact anchors and the Luna decision.
    for row in out["published"]:
        assert row["anchors"]["target"]["path"] == "new.py"
        assert row["decision"] == "KEEP"


def test_run_stage_retrieval_failure_degraded(tmp_path: Path,
                                                dupe_repo: dict,
                                                monkeypatch: pytest.MonkeyPatch):
    """Total retrieval failure is degraded, never a clean-looking zero."""
    import codatiel_i2.semdup_dev.oai_dupe as OD
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    monkeypatch.setattr(OD, "retrieve_pairs",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, assess_fn=_fake_assess,
        edit_fn=_fake_edit)
    assert out["status"] == "degraded"
    assert out["retrieval_failures"] >= 1
    assert out["published"] == []
    assert "not examined" in out["reason"]
    assert "Possible existing functionality (0)" not in (
        run_dir / "semdup.json").read_text()


def test_run_stage_target_kind_reaches_terra(tmp_path: Path,
                                             dupe_repo: dict,
                                             monkeypatch: pytest.MonkeyPatch):
    """The changed/introduced role is passed through, not defaulted."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    seen: list = []

    def _capture_assess(*, target_kind, **kw):
        seen.append(target_kind)
        return _fake_assess(target_kind=target_kind, **kw)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, assess_fn=_capture_assess,
        edit_fn=_fake_edit)
    assert out["status"] == "ok", out
    # new.py is added in head: introduced, not the "unknown" default.
    assert seen and all(k == "introduced" for k in seen)


def test_run_stage_no_gemma_invoked(tmp_path: Path, dupe_repo: dict,
                                    monkeypatch: pytest.MonkeyPatch):
    """The Gemma investigator is never constructed on the stage path."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    import codatiel_i2.semdup_vendored.semdup.staged as staged_mod
    called: list = []
    real = staged_mod.investigate

    def _boom(*a, **k):
        called.append(1)
        return real(*a, **k)

    monkeypatch.setattr(staged_mod, "investigate", _boom)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, assess_fn=_fake_assess,
        edit_fn=_fake_edit)
    assert called == []


# -- caps and dedup -----------------------------------------------------

def _pub_row(i: int) -> dict:
    return {"target": {"path": "n.py", "symbol": f"f{i}"},
            "counterpart": {"path": "e.py", "symbol": "g"},
            "head_rev": "h", "decision": "KEEP",
            "revised_comment": f"c{i}", "reason": "r",
            "anchors": {"target": {"path": "n.py", "start_line": 10 * i,
                                   "end_line": 10 * i + 5}}}


def test_pr_cap():
    kept, capped = ST._apply_pr_cap([_pub_row(1), _pub_row(2),
                                     _pub_row(3)])
    assert len(kept) == 2 and len(capped) == 1
    assert capped[0]["reason"] == "pr_cap"


def test_dedup_overlapping_suppressed(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "shadow_result.json").write_text(json.dumps({
        "status": "complete",
        "advisory": [{"finding_id": "f1", "detector": "x",
                      "changed_anchor": {"path": "n.py",
                                        "line_start": 11}}]}))
    rows = [_pub_row(1)]  # anchor line 10, within window of 11
    kept, dups = ST._dedup(run_dir, rows)
    assert kept == [] and len(dups) == 1
    assert dups[0]["reason"] == "duplicate_of_review"


def test_dedup_distant_kept(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "shadow_result.json").write_text(json.dumps({
        "status": "complete",
        "advisory": [{"finding_id": "f1", "detector": "x",
                      "changed_anchor": {"path": "n.py",
                                        "line_start": 500}}]}))
    kept, dups = ST._dedup(run_dir, [_pub_row(1)])
    assert len(kept) == 1 and dups == []


# -- email rendering ----------------------------------------------------

def _min_run(tmp_path: Path, sem: dict | None) -> Path:
    run_dir = tmp_path / "erun"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "r1", "repo": REPO,
        "base_sha": "a" * 40, "head_sha": "b" * 40,
        "config": {"model": "scripted:x"}}))
    (run_dir / "shadow_result.json").write_text(json.dumps({
        "status": "complete", "counts": {}, "advisory": []}))
    if sem is not None:
        (run_dir / "semdup.json").write_text(json.dumps(sem))
    return run_dir


def test_deliver_shadow_semdup_section(tmp_path: Path):
    run_dir = _min_run(tmp_path, {
        "version": ST.STAGE_VERSION, "status": "ok",
        "reason": "examined 1 targets, 1 pairs; 1 approved",
        "coverage": "reduced", "spend_usd": 0.0,
        "published": [{
            "target": {"path": "new.py", "symbol": "format_price"},
            "counterpart": {"path": "existing.py",
                            "symbol": "format_money"},
            "head_rev": "h", "decision": "KEEP",
            "revised_comment": "reuse format_money",
            "reason": "concrete reuse",
            "anchors": {
                "target": {"path": "new.py", "start_line": 4,
                           "end_line": 8},
                "counterpart": {"path": "existing.py", "start_line": 4,
                                "end_line": 8}}}],
        "suppressed": []})
    receipt = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b: {"ok": True, "body": b})
    body = receipt["transport_receipt"]["body"]
    assert "Possible existing functionality (1):" in body
    assert "new.py:4-8 `format_price` may reuse existing.py:4-8" in body
    assert "semdup: ok" in body


def test_deliver_shadow_unavailable_explicit(tmp_path: Path):
    run_dir = _min_run(tmp_path, {
        "version": ST.STAGE_VERSION, "status": "unavailable",
        "reason": "base index unavailable: no base index for rev",
        "coverage": "unknown", "spend_usd": 0.0,
        "published": [], "suppressed": []})
    receipt = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b: {"ok": True, "body": b})
    body = receipt["transport_receipt"]["body"]
    assert "semdup: unavailable" in body
    assert "no reuse claim is made" in body
    assert "Possible existing functionality (0)" not in body


def test_deliver_shadow_unknown_spend_honest(tmp_path: Path):
    run_dir = _min_run(tmp_path, {
        "version": ST.STAGE_VERSION, "status": "ok",
        "reason": "examined 1 targets, 1 pairs; 0 approved",
        "coverage": "full", "spend_usd": 0.0, "spend_unknown": True,
        "published": [], "suppressed": []})
    receipt = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b: {"ok": True, "body": b})
    body = receipt["transport_receipt"]["body"]
    assert "spend unknown" in body
    assert "$0.0000" not in body


def test_notify_test_env_unchanged(tmp_path: Path):
    """TEST digests carry no event label and no semdup section."""
    run_dir = _min_run(tmp_path, None)
    sent: list = []
    P._notify(
        P.EventConfig(env="test", state_dir=tmp_path,
                      notify_to=["t@example.com"],
                      notify_sender="s@example.com"),
        str(run_dir), "complete",
        transport=lambda t, s, b: sent.append(b) or {"ok": True},
        context=None)
    assert sent and "event:" not in sent[0]
    assert "semdup" not in sent[0]


def test_deliver_shadow_context_label(tmp_path: Path):
    run_dir = _min_run(tmp_path, None)
    receipt = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b: {"ok": True, "body": b},
        context="dev:o/r#1")
    body = receipt["transport_receipt"]["body"]
    assert "event: dev:o/r#1" in body
    # Without context the header is byte-identical to before.
    plain = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b: {"ok": True, "body": b})
    assert "event:" not in plain["transport_receipt"]["body"]


# -- pipeline invariance ------------------------------------------------

def _scripted_factory():
    def _mk(role: str):
        script = [runtime_sdk.scripted_tool_call(
            call_id="t1", name="read_file",
            arguments={"path": "existing.py", "revision": "head",
                       "start_line": 1, "end_line": 0},
            response_id="r1"),
            runtime_sdk.scripted_final(
                json.dumps({"status": "silence", "summary": "ok",
                            "findings": []}), response_id="f1")]
        return runtime_sdk.ScriptedModel(script), f"scripted:{role}"
    return _mk


def test_pipeline_test_env_stage_off(tmp_path: Path, dupe_repo: dict,
                                     monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", "20")
    monkeypatch.setenv("CODATIEL_TEST_TO", "t@example.com")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "s@example.com")
    monkeypatch.setenv("CODATIEL_TEST_REPO_PATHS",
                       f"{REPO}={dupe_repo['root']}")
    monkeypatch.setenv("REVIEW_PIPELINE", "terra_only")
    cfg = P.config_from_env()
    assert cfg.semdup_enabled is False
    result = P.run_review_for_range(
        cfg=cfg, repo=REPO, pr=1, base=dupe_repo["base"],
        head=dupe_repo["head"], kind="full",
        repo_path=dupe_repo["root"],
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        model_factory=_scripted_factory())
    assert result["status"] in ("complete", "inconclusive")
    assert result["semdup_status"] == "off"
    assert not (Path(result["run_dir"]) / "semdup.json").exists()


def test_pipeline_dev_opt_out_intact(tmp_path: Path, dupe_repo: dict,
                                     monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODATIEL_ENV", "dev")
    monkeypatch.setenv("CODATIEL_DEV_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_DEV_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_DEV_SPEND_CAP_USD", "5")
    monkeypatch.setenv("CODATIEL_DEV_TO", "t@example.com")
    monkeypatch.setenv("CODATIEL_DEV_SENDER", "s@example.com")
    monkeypatch.setenv("CODATIEL_DEV_REPO_PATHS",
                       f"{REPO}={dupe_repo['root']}")
    monkeypatch.setenv("CODATIEL_DEV_SEMDUP", "0")
    monkeypatch.setenv("REVIEW_PIPELINE", "terra_only")
    cfg = P.config_from_env()
    assert cfg.semdup_enabled is False
    result = P.run_review_for_range(
        cfg=cfg, repo=REPO, pr=1, base=dupe_repo["base"],
        head=dupe_repo["head"], kind="full",
        repo_path=dupe_repo["root"],
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        model_factory=_scripted_factory())
    assert result["status"] in ("complete", "inconclusive")
    assert result["semdup_status"] == "off"
