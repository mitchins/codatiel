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
    assert cfg.semdup_max_pairs == 0  # experimental cap disabled
    assert cfg.whole_pr_ceiling_usd == pytest.approx(2.0)


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
    monkeypatch.setenv("CODATIEL_DEV_WHOLE_PR_CEILING_USD", "nonsense")
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
    # Examination completeness is top-level; deterministic retrieval-only
    # coverage lives in the detail record.
    assert out["coverage"] == "full", out
    assert out["coverage_detail"]["retrieval"]["coverage"].startswith(
        "reduced")  # deterministic only
    assert out["coverage_detail"]["examination"]["targets_examined"] >= 1
    assert len(out["targets"]) >= 1
    assert out["pairs_examined"] >= 1  # retrieval really ran
    assert "pr_cap" not in json.dumps(out)  # no numerical publication cap
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


def test_no_publication_cap_all_approved_publish(
        tmp_path: Path, dupe_repo: dict, monkeypatch: pytest.MonkeyPatch):
    """Every Luna-approved pair publishes: no numerical cap exists."""
    assert not hasattr(ST, "PR_FINDING_CAP")
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
    assert len(out["published"]) >= 1
    assert "pr_cap" not in json.dumps(out.get("suppressed", []))


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
    (run_dir / "metrics.json").write_text(json.dumps({
        "status": "complete", "model": "scripted:x",
        "spend": {"usd_estimate": 0.0123}}))
    if sem is not None:
        (run_dir / "semdup.json").write_text(json.dumps(sem))
    return run_dir


def test_deliver_shadow_semdup_section(tmp_path: Path):
    run_dir = _min_run(tmp_path, {
        "version": ST.STAGE_VERSION, "status": "ok",
        "reason": "examined 1 targets, 1 pairs; 1 approved",
        "coverage": "full", "spend_usd": 0.0,
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
        transport=lambda t, s, b, h=None: {"ok": True, "body": b,
                                           "html": h})
    body = receipt["transport_receipt"]["body"]
    assert "Possible existing functionality" in body
    assert "new.py:4-8" in body and "format_price" in body
    assert "existing.py" in body and "KEEP" in body
    assert "reuse format_money" in body
    assert "duplication check finished, complete coverage" in body
    html = receipt["transport_receipt"]["html"]
    assert html and "<html" in html and "format_price" in html
    stored = json.loads((run_dir / "delivery.json").read_text())
    assert stored["html_body"] == html and stored["html_sha256"]


def test_deliver_shadow_unavailable_explicit(tmp_path: Path):
    run_dir = _min_run(tmp_path, {
        "version": ST.STAGE_VERSION, "status": "unavailable",
        "reason": "base index unavailable: no base index for rev",
        "coverage": "unknown", "spend_usd": 0.0,
        "published": [], "suppressed": []})
    receipt = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b, h=None: {"ok": True, "body": b})
    body = receipt["transport_receipt"]["body"]
    assert "Did not complete (unavailable)" in body
    assert "No reuse claim is made" in body
    assert "may reuse" not in body


def test_deliver_shadow_unknown_spend_honest(tmp_path: Path):
    run_dir = _min_run(tmp_path, {
        "version": ST.STAGE_VERSION, "status": "ok",
        "reason": "examined 1 targets, 1 pairs; 0 approved",
        "coverage": "full", "spend_usd": 0.0, "spend_unknown": True,
        "published": [], "suppressed": []})
    receipt = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b, h=None: {"ok": True, "body": b})
    body = receipt["transport_receipt"]["body"]
    assert "spend unknown" in body
    # The unknown semdup spend is never rendered as $0.00; the ordinary
    # review cost (from run metrics) is stated separately.
    assert "duplication check finished, complete coverage, spend unknown" in body
    assert "$0.0123" in body


def test_notify_test_env_unchanged(tmp_path: Path):
    """TEST digests carry no event label and no semdup section."""
    run_dir = _min_run(tmp_path, None)
    sent: list = []
    P._notify(
        P.EventConfig(env="test", state_dir=tmp_path,
                      notify_to=["t@example.com"],
                      notify_sender="s@example.com"),
        str(run_dir), "complete",
        transport=lambda t, s, b, h=None: sent.append(b) or {"ok": True},
        context=None)
    assert sent and "event:" not in sent[0]
    assert "semdup" not in sent[0]


def test_deliver_shadow_context_label(tmp_path: Path):
    run_dir = _min_run(tmp_path, None)
    receipt = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b, h=None: {"ok": True, "body": b,
                                           "html": h},
        context="dev:o/r#1")
    body = receipt["transport_receipt"]["body"]
    # The event label now drives the human header + PR link, not a raw
    # "event:" debug line.
    assert "Codatiel review: o/r#1" in body
    assert "https://github.com/o/r/pull/1" in body
    assert "event:" not in body
    # Without context the header names the repo with no PR link.
    plain = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b, h=None: {"ok": True, "body": b},
        context=None, force=True)
    assert "Codatiel review: o/r" in plain["transport_receipt"]["body"]
    assert "github.com" not in plain["transport_receipt"]["body"]


def test_deliver_shadow_duplicate_send_prevented(tmp_path: Path):
    """An identical body with an accepted receipt is not re-sent."""
    run_dir = _min_run(tmp_path, None)
    calls: list = []

    def _send(t, s, b, h=None):
        calls.append(b)
        return {"accepted_by_transport": True, "parsed": True,
                "succeeded": 1, "failed": 0, "email_id": "e1"}

    first = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=_send)
    assert len(calls) == 1
    second = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=_send)
    assert len(calls) == 1
    assert second.get("delivery") == "already-delivered"
    assert second["body_sha256"] == first["body_sha256"]
    # An explicit operator replay bypasses the guard.
    third = service_mod.deliver_shadow(
        run_dir, to=["t@example.com"], sender="s@example.com",
        transport=_send, force=True)
    assert len(calls) == 2
    assert third.get("delivery") != "already-delivered"


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


# -- COMPLETENESS-01: spend-gated examination, no count caps ---------

def _multi_dupe_repo(tmp_path: Path) -> dict:
    """Base + head with three near-duplicate added functions."""
    import subprocess

    def _git(repo, *args):
        proc = subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    root = tmp_path / "multirepo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "existing.py").write_text(EXISTING)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    body = ('\n\ndef {name}(amount, places=2):\n'
            '    """Format amount with thousands separators for display."""\n'
            '    quantized = round(float(amount), places)\n'
            '    return f"{{quantized:,{{places}}f}}"\n')
    (root / "new.py").write_text(
        '"""Checkout helpers."""\n' + "".join(
            body.format(name=n) for n in
            ("alpha_price", "beta_price", "gamma_price")))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add three duplicates")
    return {"root": root, "base": base,
            "head": _git(root, "rev-parse", "HEAD")}


def test_publish_all_multiple_approvals(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Three genuine approvals all publish — no finding-count cap."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    repo = _multi_dupe_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=repo["root"], repo=REPO, pr=1,
        base_rev=repo["base"], head_rev=repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, assess_fn=_fake_assess,
        edit_fn=_fake_edit)
    assert out["status"] == "ok", out
    assert len(out["published"]) >= 3, out
    assert out["coverage"] == "full", out


def test_mirror_publications_collapsed(tmp_path: Path):
    """A->B and B->A approvals publish once; the mirror is recorded."""
    a = _pub_row(1)
    b = {"target": a["counterpart"], "counterpart": a["target"],
         "head_rev": "h", "decision": "REVISE",
         "revised_comment": "c", "reason": "r", "anchors": {}}
    kept, mirrors = ST._dedup_mirrors([a, b])
    assert kept == [a]
    assert len(mirrors) == 1
    assert mirrors[0]["reason"] == "mirror_of_published"


def test_deterministic_filter_fragments_excluded():
    assert ST._is_filtered_unit("oracles/x.py", "f")
    assert ST._is_filtered_unit("oracles", "f")
    assert ST._is_filtered_unit("a.py", "x#region@1")
    assert ST._is_filtered_unit("a.py", "  ")
    assert ST._is_filtered_unit("", "f")
    assert not ST._is_filtered_unit("a.py", "real_fn")
    assert not ST._is_filtered_unit("a.py", "<closure>@101")


def test_pair_ordering_multichannel_first():
    def _p(t, c, via):
        return {"target": {"path": "n.py", "symbol": t},
                "counterpart": {"path": "e.py", "symbol": c},
                "channels": via}

    ordered = ST._order_pairs([
        _p("z", "z", ["embedding"]),
        _p("a", "a", ["lexical", "semantic"]),
        _p("m", "m", []),
        _p("b", "b", ["semantic", "lexical"]),
    ])
    assert [(p["target"]["symbol"], len(p["channels"]))
            for p in ordered] == [("a", 2), ("b", 2), ("z", 1), ("m", 0)]


def test_whole_pr_ceiling_stops_cleanly(
        tmp_path: Path, dupe_repo: dict, monkeypatch: pytest.MonkeyPatch):
    """A ~zero remainder stops inference with explicit partial status."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, ceiling_usd=0.0,
        assess_fn=_fake_assess, edit_fn=_fake_edit)
    assert out["status"] == "ok", out
    assert out["published"] == []
    assert out["coverage"] == "partial", out
    assert all(r.get("reason") == "budget_stop"
               for r in out["suppressed"]), out
    assert "budget" in out["reason"].lower()


def test_terra_without_luna_never_publishes(
        tmp_path: Path, dupe_repo: dict, monkeypatch: pytest.MonkeyPatch):
    """A Terra proposal held back by a failed Luna edit is suppressed."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")

    def _fail_edit(*, package, proposed_comment, tools, model,
                   model_settings, ledger, ctx=None, call=None,
                   max_turns=4):
        return {"ok": False, "error": "luna exploded"}

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, assess_fn=_fake_assess,
        edit_fn=_fail_edit)
    assert out["status"] == "ok", out
    assert out["published"] == []
    assert any(r.get("reason") == "editor_unpublishable"
               for r in out["suppressed"])


def test_continuation_rerun_no_recharge(
        tmp_path: Path, dupe_repo: dict, monkeypatch: pytest.MonkeyPatch):
    """An authorized rerun reuses recorded verdicts: zero new inference."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    calls = {"assess": 0, "edit": 0}

    def _count_assess(**kw):
        calls["assess"] += 1
        return _fake_assess(**kw)

    def _count_edit(**kw):
        calls["edit"] += 1
        return _fake_edit(**kw)

    kw = dict(repo_path=dupe_repo["root"], repo=REPO, pr=1,
              base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
              state_dir=tmp_path / "state",
              allow_embeddings=False, assess_fn=_count_assess,
              edit_fn=_count_edit)
    run1 = tmp_path / "run1"
    run1.mkdir()
    out1 = ST.run_stage(run_dir=run1, **kw)
    assert out1["status"] == "ok", out1
    n_assess = calls["assess"]
    assert n_assess >= 1
    assert out1["pairs_reused"] == 0
    run2 = tmp_path / "run2"
    run2.mkdir()
    out2 = ST.run_stage(run_dir=run2, **kw)
    assert out2["status"] == "ok", out2
    assert calls["assess"] == n_assess  # no repeated paid work
    assert calls["edit"] == out1["pairs_examined"]
    assert out2["pairs_reused"] == out1["pairs_examined"]
    assert out2["spend_usd"] == 0.0
    assert len(out2["published"]) == len(out1["published"])


def test_maybe_semdup_stage_remainder_guard(tmp_path: Path):
    """Unknown or exhausted normal cost => explicit unavailable, no spend."""
    from codatiel_i2 import event_pipeline as P
    cfg = P.EventConfig(env="dev", state_dir=tmp_path,
                        whole_pr_ceiling_usd=2.0, semdup_enabled=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"run_id": "r"}))
    (run_dir / "metrics.json").write_text(json.dumps({}))
    out = P._maybe_semdup_stage(
        cfg=cfg, repo=REPO, pr=1, base="b", head="h",
        repo_path=tmp_path, runs_root=tmp_path, run_dir=str(run_dir),
        result={"cost_usd": None, "cost_unknown": True})
    assert out["semdup_status"] == "unavailable"
    assert "without an accountable" in out["semdup_reason"]
    out2 = P._maybe_semdup_stage(
        cfg=cfg, repo=REPO, pr=1, base="b", head="h",
        repo_path=tmp_path, runs_root=tmp_path, run_dir=str(run_dir),
        result={"cost_usd": 2.5, "cost_unknown": False})
    assert out2["semdup_status"] == "unavailable"
    assert "already reached" in out2["semdup_reason"]

def test_stage_pair_budget_partial_never_full(
        tmp_path: Path, dupe_repo: dict, monkeypatch: pytest.MonkeyPatch):
    """An explicit experimental pair cap truncates honestly as partial."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, max_pairs=0,
        assess_fn=_fake_assess, edit_fn=_fake_edit)
    assert out["status"] == "ok", out
    assert out["coverage"] == "partial", out
    assert out["skipped"], out
    assert all(s.get("reason") for s in out["skipped"])
    assert any("experimental pair budget" in s.get("reason", "")
               for s in out["skipped"])
    assert "skipped" in out["reason"]
    detail = out["coverage_detail"]["examination"]
    assert detail["targets_examined"] == 0
    assert detail["pairs_examined"] == 0
    assert len(detail["skipped"]) == len(out["skipped"])


def test_stage_no_count_caps_by_default(
        tmp_path: Path, dupe_repo: dict, monkeypatch: pytest.MonkeyPatch):
    """Normal operation examines every retrieved pair (no truncation)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, max_pairs=None,
        assess_fn=_fake_assess, edit_fn=_fake_edit)
    assert out["status"] == "ok", out
    assert out["pairs_total"] >= 1
    assert out["pairs_examined"] == out["pairs_total"]
    assert not any("budget exhausted" in s.get("reason", "")
                   for s in out["skipped"])


def test_stage_full_only_when_nothing_skipped(
        tmp_path: Path, dupe_repo: dict, monkeypatch: pytest.MonkeyPatch):
    """The clean fixture genuinely earns 'full' with empty skip lists."""
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
    assert out["coverage"] == "full", out
    assert out["skipped"] == []
    assert out["coverage_detail"]["retrieval"]["failures"] == 0


def test_budget_refusal_never_labelled_as_decline(
        tmp_path: Path, dupe_repo: dict, monkeypatch: pytest.MonkeyPatch):
    """A ledger ceiling refusal is a budget_stop, not a model verdict."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")

    def _refuse_assess(**kw):
        return {"ok": False, "error": "BudgetCeilingExceeded: next request "
                "estimated $0.0671 exceeds remaining budget $0.0021",
                "transcript": {"tool_calls": []}}

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, ceiling_usd=2.0,
        assess_fn=_refuse_assess, edit_fn=_fake_edit)
    assert out["status"] == "ok", out
    assert out["published"] == []
    assert out["suppressed"], out
    assert all(r.get("reason") == "budget_stop"
               for r in out["suppressed"])
    assert out["coverage"] == "partial", out


def test_estimate_uses_actual_evidence_size(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A 60 KB unit stops a $0.02 ceiling; the old 12 K proxy would pass."""
    import subprocess

    def _git(repo, *args):
        proc = subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def _big(name):
        return ('"""Big module."""\n\n\ndef %s():\n    """Doc."""\n'
                % name + "\n".join(f"    v{i} = {i}"
                                   for i in range(3000))
                + "\n    return 1\n")

    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    root = tmp_path / "bigrepo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "existing.py").write_text(_big("big_money"))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "new.py").write_text(_big("big_price"))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "big")
    head = _git(root, "rev-parse", "HEAD")
    called = []

    def _spy_assess(**kw):
        called.append(kw)
        return _fake_assess(**kw)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ST.run_stage(
        repo_path=root, repo=REPO, pr=1, base_rev=base, head_rev=head,
        run_dir=run_dir, state_dir=tmp_path / "state",
        allow_embeddings=False, ceiling_usd=0.02,
        assess_fn=_spy_assess, edit_fn=_fake_edit)
    assert out["status"] == "ok", out
    assert called == []  # estimate gate stopped before any model call
    assert any(r.get("reason") == "budget_stop"
               for r in out["suppressed"])
    assert out["coverage"] == "partial", out


def test_continuation_ignores_mislabeled_refusals(tmp_path: Path):
    """Budget-mislabeled rows are re-judged, never reused as verdicts."""
    from codatiel_i2.semdup_dev import stage as S
    pair = {"target": {"path": "n.py", "symbol": "f"},
            "counterpart": {"path": "e.py", "symbol": "g"}}
    key = S._pair_key(pair["target"], pair["counterpart"])
    known = {
        key: {"target": pair["target"], "counterpart": pair["counterpart"],
              "path": "suppressed", "reason": "terra_no_lead",
              "detail": "BudgetCeilingExceeded: next request estimated "
                        "$0.0671 exceeds remaining budget $0.0021"},
    }
    new_pairs, reused = S._partition_new([pair], known)
    assert new_pairs == [pair] and reused == []
    known[key] = {"target": pair["target"],
                  "counterpart": pair["counterpart"],
                  "path": "suppressed", "reason": "terra_no_lead",
                  "detail": ""}
    new_pairs, reused = S._partition_new([pair], known)
    assert new_pairs == [] and len(reused) == 1


def test_validate_anchor_pins_ambiguous_unit(tmp_path: Path):
    """Same (path, symbol) twice: bare lookup refuses, pinned resolves."""
    import sqlite3
    from codatiel_i2.semdup_dev import advisory as AV
    db = tmp_path / "idx.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE units (path TEXT, symbol TEXT, "
                 "start_line INT, end_line INT, text TEXT)")
    conn.execute("INSERT INTO units VALUES (?,?,?,?,?)",
                 ("a.py", "run", 10, 15, "def run(): pass"))
    conn.execute("INSERT INTO units VALUES (?,?,?,?,?)",
                 ("a.py", "run", 40, 45, "def run(): stop"))
    conn.commit()
    conn.close()
    assert AV.validate_anchor(db, "a.py", "run")["ok"] is False
    good = AV.validate_anchor(db, "a.py", "run", 40)
    assert good["ok"] and good["start_line"] == 40
    assert "def run(): stop" in good["text"]
    assert AV.validate_anchor(db, "a.py", "run", 999)["ok"] is False


def test_pair_key_line_qualified():
    from codatiel_i2.semdup_dev import stage as S
    t = {"path": "a.py", "symbol": "run", "start_line": 10}
    c = {"path": "b.py", "symbol": "run", "start_line": 40}
    assert S._pair_key(t, c) == "a.py:10::run>>b.py:40::run"
    legacy = S._pair_key({"path": "a.py", "symbol": "run"},
                         {"path": "b.py", "symbol": "run"})
    assert legacy != S._pair_key(t, c)


def test_preflight_bounds_actual_spend(tmp_path: Path, dupe_repo: dict,
                                       monkeypatch: pytest.MonkeyPatch):
    """Preflight is a conservative upper bound, never below actuals."""
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
    assert out["preflight_usd"] is not None
    assert out["preflight_usd"] >= out["spend_usd"]


def test_resume_reuses_and_reports_spend(tmp_path: Path, dupe_repo: dict,
                                        monkeypatch: pytest.MonkeyPatch):
    """Operator resume: prior verdicts reused, spend reconciled."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    calls = {"n": 0}

    def _count_assess(**kw):
        calls["n"] += 1
        return _fake_assess(**kw)

    from codatiel_i2 import event_pipeline as P
    cfg = P.EventConfig(env="dev", state_dir=tmp_path / "estate",
                        repos=[REPO],
                        repo_paths={REPO: str(dupe_repo["root"])},
                        whole_pr_ceiling_usd=2.0, semdup_enabled=True)
    run1 = tmp_path / "run1"
    run1.mkdir()
    first = ST.run_stage(
        repo_path=dupe_repo["root"], repo=REPO, pr=1,
        base_rev=dupe_repo["base"], head_rev=dupe_repo["head"],
        run_dir=run1, state_dir=tmp_path / "estate",
        allow_embeddings=False, ceiling_usd=0.0,
        assess_fn=_count_assess, edit_fn=_fake_edit)
    assert first["status"] == "ok", first
    assert calls["n"] == 0  # ceiling zero: nothing judged
    resumed = P.resume_semdup(
        cfg=cfg, repo=REPO, pr=1, base=dupe_repo["base"],
        head=dupe_repo["head"], run_dir=str(run1), ceiling_usd=2.0,
        assess_fn=_count_assess, edit_fn=_fake_edit)
    assert resumed["status"] == "ok", resumed
    assert calls["n"] >= 1  # new ceiling judges the held pairs
    assert resumed["prior_spend_usd"] == 0.0
    assert resumed["published"] >= 1
    # Second resume at the same ceiling repeats nothing.
    n_after_first = calls["n"]
    again = P.resume_semdup(
        cfg=cfg, repo=REPO, pr=1, base=dupe_repo["base"],
        head=dupe_repo["head"], run_dir=str(run1), ceiling_usd=2.0,
        assess_fn=_count_assess, edit_fn=_fake_edit)
    assert again["status"] == "ok", again
    assert calls["n"] == n_after_first
    assert again["new_spend_usd"] == 0.0


def test_cli_resume_semdup_validates_args(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch):
    """Resume refuses incomplete operator input without side effects."""
    from codatiel_i2.cli import build_parser, cmd_events
    monkeypatch.setenv("CODATIEL_ENV", "dev")
    monkeypatch.setenv("CODATIEL_DEV_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_DEV_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_DEV_SPEND_CAP_USD", "5")
    monkeypatch.setenv("CODATIEL_DEV_TO", "t@example.com")
    monkeypatch.setenv("CODATIEL_DEV_SENDER", "s@example.com")
    args = build_parser().parse_args(["events", "resume-semdup"])
    out = cmd_events(args)
    assert out["status"] == "failed"
    assert "needs --repo" in out["error"]
