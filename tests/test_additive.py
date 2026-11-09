"""REVIEW-ADDITIVE-01: deterministic consolidation + graceful degradation.

All free (scripted models, real SDK/tools/sessions where they matter).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codatiel_i2 import additive as additive_mod
from codatiel_i2 import delegate as delegate_mod
from codatiel_i2 import runtime_sdk


def _scripted_final(text, rid="f1"):
    return runtime_sdk.scripted_final(text, response_id=rid)


def _scripted_model(parts):
    script = list(parts)
    return runtime_sdk.ScriptedModel(script)


def _sweep_text(findings):
    body = "\n".join(f"### {f['title']}\n{f['evidence']}" for f in findings)
    return body + "\n```json\n" + json.dumps({"findings": findings}) + "\n```\n"


HEAD_APP = "def timeout():\n    return 60\n"


def test_parse_ok():
    f = [{"title": "t", "severity": "P2", "path": "a.py",
          "revision": "head", "line_start": 1, "line_end": 2,
          "evidence": "x", "why_pr_introduced": "y"}]
    out, err = additive_mod.parse_sweep_findings(_sweep_text(f))
    assert err is None and out == f


def test_parse_missing_block():
    out, err = additive_mod.parse_sweep_findings("no findings here")
    assert out == [] and err


def test_parse_unparseable():
    out, err = additive_mod.parse_sweep_findings("```json\n{oops\n```\n")
    assert out == [] and err


def test_validate_anchor_ok_and_bad(two_commit_repo, tmp_path):
    repo = Path(two_commit_repo["repo"])
    good = {"title": "t", "severity": "P2", "path": "src/app.py",
            "revision": "head", "line_start": 1, "line_end": 2,
            "evidence": "return 60", "why_pr_introduced": "y"}
    ok, note = additive_mod.validate_anchor(good, repo, repo)
    assert ok, note
    bad_path = dict(good, path="nope.py")
    ok, reason = additive_mod.validate_anchor(bad_path, repo, repo)
    assert not ok and reason
    bad_ev = dict(good, evidence="return 9999")
    ok, reason = additive_mod.validate_anchor(bad_ev, repo, repo)
    assert not ok and reason
    bad_sev = dict(good, severity="P9")
    ok, _ = additive_mod.validate_anchor(bad_sev, repo, repo)
    assert not ok
    # Excerpt-assembled evidence (non-adjacent lines) validates with note.
    asm = dict(good, path="src/new_feature.py", line_start=1, line_end=2,
               evidence="def new_fn\n    return 2")
    ok, note = additive_mod.validate_anchor(asm, repo, repo)
    assert ok and "assembled" in note, (ok, note)
    # Mostly-fabricated evidence still rejected.
    fab = dict(good, path="src/new_feature.py", line_start=1, line_end=2,
               evidence="def new_fn\n    launch_missiles\n    delete_all\n    return 2")
    ok, reason = additive_mod.validate_anchor(fab, repo, repo)
    assert not ok, reason


def test_is_duplicate():
    terra = [{"changed_anchor": {"path": "src/app.py", "line_start": 1,
                                 "line_end": 2},
              "bad_outcome": "timeout lowered from 30 to 60 without updating "
                             "callers or documentation"}]
    luna = {"title": "timeout changed without caller updates",
            "path": "src/app.py", "line_start": 1, "line_end": 2,
            "evidence": "return 60"}
    dup, _ = additive_mod.is_duplicate(luna, terra)
    assert dup
    other = dict(luna, path="src/other.py")
    dup, _ = additive_mod.is_duplicate(other, terra)
    assert not dup
    far = dict(luna, line_start=100, line_end=110)
    dup, _ = additive_mod.is_duplicate(far, terra)
    assert not dup


def test_consolidate_counts(two_commit_repo, tmp_path):
    repo = Path(two_commit_repo["repo"])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    terra_rows = [
        {"changed_anchor": {"path": "src/app.py", "line_start": 1,
                            "line_end": 2},
         "bad_outcome": "timeout lowered from 30 to 60 without updating "
                        "callers or documentation"},
        {"changed_anchor": {"path": "src/app.py", "line_start": 1,
                            "line_end": 1},
         "bad_outcome": "unrelated header comment stale"},
    ]
    (run_dir / "shadow_result.json").write_text(json.dumps(
        {"status": "complete", "advisory": terra_rows}))
    luna = [
        {"title": "timeout changed without caller updates", "severity": "P2", "path": "src/app.py",
         "revision": "head", "line_start": 1, "line_end": 2,
         "evidence": "return 60", "why_pr_introduced": "y"},  # dup
        {"title": "new file untested", "severity": "P3",
         "path": "src/new_feature.py", "revision": "head",
         "line_start": 1, "line_end": 2, "evidence": "def new_fn",
         "why_pr_introduced": "y"},  # added (file exists in fixture)
        {"title": "ghost", "severity": "P2", "path": "missing.py",
         "revision": "head", "line_start": 1, "line_end": 1,
         "evidence": "x", "why_pr_introduced": "y"},  # rejected
    ]
    out = additive_mod.consolidate(terra_run_dir=run_dir, luna_findings=luna,
                                   base_dir=repo, head_dir=repo)
    assert out["terra_findings"] == 2
    assert len(out["luna_added"]) == 1
    assert out["luna_added"][0]["provenance"] == "luna-blind-sweep"
    assert len(out["duplicates"]) == 1
    assert len(out["rejected"]) == 1


def _run_additive_with(two_commit_repo, tmp_path, monkeypatch,
                       terra_final, luna_text):
    from codatiel_i2 import providers as providers_mod
    repo = two_commit_repo
    terra_model = _scripted_model([
        runtime_sdk.scripted_tool_call(
            call_id="t_0", name="read_file",
            arguments={"path": "src/app.py", "revision": "head",
                       "start_line": 1, "end_line": 0},
            response_id="t_r0"),
        _scripted_final(json.dumps(terra_final))])
    luna_model = _scripted_model([_scripted_final(luna_text)])
    real_build = providers_mod.build_model

    def _fake(name):
        from agents.model_settings import ModelSettings
        if name == "terra":
            return terra_model, ModelSettings(store=False)
        if name == "luna":
            return luna_model, ModelSettings(store=False, max_tokens=8000)
        return real_build(name)
    monkeypatch.setattr(providers_mod, "build_model", _fake)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    target = {"mode": "local", "case": "T1",
              "root": str(repo["repo"]), "base": repo["base_sha"],
              "head": repo["head_sha"]}
    return additive_mod.run_additive(
        mode="additive", target=target, runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")


def test_run_additive_happy_and_sealed(two_commit_repo, tmp_path,
                                      monkeypatch):
    silence = {"status": "silence", "summary": "clean",
               "findings": []}
    luna_f = [{"title": "timeout lowered", "severity": "P2",
               "path": "src/app.py", "revision": "head",
               "line_start": 1, "line_end": 2,
               "evidence": "return 60", "why_pr_introduced": "changed"}]
    out = _run_additive_with(two_commit_repo, tmp_path, monkeypatch,
                             silence, _sweep_text(luna_f))
    assert out["mode"] == "additive", out
    assert out["terra_findings"] == 0 and out["added"] == 1
    assert out["sub_usd"] == 0.0  # scripted: usage without rates
    run_dir = Path(out["run_dir"])
    assert (run_dir / "shadow_result.json").exists()  # Terra sealed
    merged = json.loads((run_dir / "additive.json").read_text())
    assert len(merged["luna_added"]) == 1
    # Session DB persists for post-seal follow-ups.
    assert list((tmp_path / "work" / "sub").glob("session-*.db"))


def test_run_additive_degrades_on_bad_sweep(two_commit_repo, tmp_path,
                                            monkeypatch):
    silence = {"status": "silence", "summary": "clean", "findings": []}
    out = _run_additive_with(two_commit_repo, tmp_path, monkeypatch,
                             silence, "free text, no findings block")
    assert out["mode"] == "terra-only-degraded", out
    assert "reason" in out and Path(out["run_dir"]).exists()


def test_explicit_clean_counts_as_additive_zero(two_commit_repo, tmp_path,
                                                monkeypatch):
    silence = {"status": "silence", "summary": "clean", "findings": []}
    out = _run_additive_with(
        two_commit_repo, tmp_path, monkeypatch, silence,
        "## Findings\n\nNo genuine defects found in the reviewed changes.")
    assert out["mode"] == "additive", out
    assert out["added"] == 0


def test_session_continues_post_seal(two_commit_repo, tmp_path):
    from codatiel_i2 import providers as providers_mod
    import os
    from unittest import mock
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
        resolved = providers_mod.resolve_backend("luna")
    from agents.model_settings import ModelSettings
    art = tmp_path / "sub"
    repo = Path(two_commit_repo["repo"])
    kw = dict(role="luna", backend="luna", tag="ADD-T9",
              artifact_dir=art, repo="t/r",
              base_sha=two_commit_repo["base_sha"],
              head_sha=two_commit_repo["head_sha"],
              base_dir=repo, head_dir=repo, diff_repo=repo,
              model=_scripted_model(
                  [_scripted_final("sweep done"),
                   _scripted_final("followup done")]),
              model_settings=ModelSettings(store=False),
              rate=resolved["rate_usd_per_mtok"], rate_status="test",
              spec=dict(delegate_mod.DEFAULT_SPEC), fresh=False)
    from codatiel_i2.core.spend import SpendLedger
    led = SpendLedger(rate=resolved["rate_usd_per_mtok"],
                      rate_status="test", ceiling_usd=1.0)
    s1 = delegate_mod.SubagentSession(**kw)
    r1 = asyncio.run(s1.sweep("Review.", led))
    assert r1["status"] == "ok"
    # A later process reattaches to the same PR-scoped thread.
    s2 = delegate_mod.SubagentSession(**kw)
    r2 = asyncio.run(s2.ask("and then?", "h", "", led))
    assert r2["status"] == "ok"
    assert r2["entry"]["resumption_ok"] is True
