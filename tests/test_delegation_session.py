"""REVIEW-DELEGATE-01 context-retention proof (no paid inference).

Proves, with scripted models driving the real SDK Runner, real I2 tools
and a real file-backed SQLiteSession:
1. a subagent investigates a file;
2. a second question continues the SAME session with the earlier
   investigation available (no restart from an empty conversation);
3. a separate PR/tag receives no previous context (isolation + cleanup);
4. follow-up caps refuse without inference;
5. subagent timeouts surface as errors, not hangs;
6. interruption (cancellation) leaves no partial ledger entry;
7. end-to-end lead wiring: a scripted lead calling delegate_investigation
   gets the scripted subagent's retained-context answer, recorded in
   metrics.json.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codatiel_i2 import delegate as delegate_mod
from codatiel_i2 import runtime_sdk
from codatiel_i2 import service as service_mod
from codatiel_i2.core.spend import SpendLedger


def _scripted_pair(tool_turns, final_text, extra_finals=()):
    script = list(tool_turns)
    script.append(runtime_sdk.scripted_final(
        final_text, response_id="scn_final"))
    for i, text in enumerate(extra_finals):
        script.append(runtime_sdk.scripted_final(
            text, response_id=f"scn_final{i + 2}"))
    return runtime_sdk.ScriptedModel(script)


def _read_turn(path="src/app.py"):
    return [runtime_sdk.scripted_tool_call(
        call_id="scn_0", name="read_file",
        arguments={"path": path, "revision": "head",
                   "start_line": 1, "end_line": 0},
        response_id="scn_r0")]


def _sub_session(two_commit_repo, tmp_path, tag, model, role="luna", **over):
    import os
    from unittest import mock
    from codatiel_i2 import providers as providers_mod
    # Dummy credentials: all models here are scripted (no inference).
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test",
                                       "ANTHROPIC_API_KEY": "test"}):
        resolved = providers_mod.resolve_backend(role)
    spec = dict(delegate_mod.DEFAULT_SPEC)
    spec.update(over)
    repo = two_commit_repo
    root = Path(repo["repo"])
    art = tmp_path / "art" / tag
    ledger = SpendLedger(rate=resolved["rate_usd_per_mtok"],
                         rate_status="test", ceiling_usd=1.0)
    from agents.model_settings import ModelSettings
    sess = delegate_mod.SubagentSession(
        role=role, backend=role, tag=tag, artifact_dir=art,
        repo="test/repo", base_sha=repo["base_sha"],
        head_sha=repo["head_sha"],
        base_dir=root, head_dir=root, diff_repo=root,
        model=model, model_settings=ModelSettings(store=False),
        rate=resolved["rate_usd_per_mtok"], rate_status="test", spec=spec)
    return sess, ledger


def test_session_retains_investigation_across_questions(two_commit_repo,
                                                        tmp_path):
    agents = runtime_sdk.require_sdk()
    db = tmp_path / "sess.db"
    sess = agents.SQLiteSession("proof-thread", db_path=str(db))
    repo = two_commit_repo
    ctx = runtime_sdk.ReviewContext(
        repo="t/r", base_sha=repo["base_sha"], head_sha=repo["head_sha"],
        base_dir=Path(repo["repo"]), head_dir=Path(repo["repo"]),
        diff_repo=Path(repo["repo"]), tool_budget=10)
    tools = runtime_sdk.build_i2_tools(ctx)
    m1 = _scripted_pair(
        _read_turn(), "INVESTIGATION MARKER ALPHA: timeout 30->60 in "
        "src/app.py:1, unrelated new file src/new_feature.py noted.")
    m2 = _scripted_pair(
        [], "Follow-up: building on ALPHA, the riskiest span is "
        "src/app.py:1 (callers unchecked).")
    from agents.model_settings import ModelSettings
    ag = agents.Agent(name="proof-sub", instructions="investigate",
                      tools=tools, model=m1,
                      model_settings=ModelSettings(store=False))
    r1 = asyncio.run(agents.Runner.run(
        ag, "Q1: investigate the timeout change", session=sess,
        max_turns=4))
    n1 = asyncio.run(sess.get_items())
    assert "ALPHA" in r1.final_output
    ag2 = agents.Agent(name="proof-sub", instructions="investigate",
                       tools=tools, model=m2,
                       model_settings=ModelSettings(store=False))
    # Second question from the lead on the SAME session object.
    r2 = asyncio.run(agents.Runner.run(
        ag2, "Q2: which span is riskiest given the above?", session=sess,
        max_turns=4))
    n2 = asyncio.run(sess.get_items())
    assert len(n2) > len(n1) > 0  # no restart from empty
    blob = json.dumps(n2, default=str)
    assert "ALPHA" in blob  # earlier investigation still available
    assert "riskiest" in r2.final_output


def test_subagent_sweep_then_followup_retains_context(two_commit_repo,
                                                      tmp_path):
    model = _scripted_pair(
        _read_turn(),
        "SWEEP MARKER BETA: timeout 30->60 src/app.py:1; no other defects.",
        extra_finals=("FOLLOWUP MARKER GAMMA: per BETA, callers of "
                      "timeout() unchecked.",))
    sess, ledger = _sub_session(two_commit_repo, tmp_path, "pr1-r1", model)
    user_input = "Review this change. Repository: t/r."
    sweep = asyncio.run(sess.sweep(user_input, ledger))
    assert sweep["status"] == "ok" and "BETA" in sweep["text"]
    n1 = asyncio.run(sess._item_count())
    assert n1 > 0
    follow = asyncio.run(sess.ask("which callers?", "callers unchecked",
                                  "src/", ledger))
    assert follow["status"] == "ok" and "GAMMA" in follow["text"]
    entry = follow["entry"]
    assert entry["resumption_ok"] is True
    assert entry["session_items_after"] > entry["session_items_before"]
    ledger_lines = (sess.artifact_dir / "ledger.jsonl").read_text(
        encoding="utf-8").strip().split("\n")
    assert len(ledger_lines) == 2  # sweep + followup, append-only


def test_separate_tag_gets_no_context_and_reinit_cleans(two_commit_repo,
                                                        tmp_path):
    model = _scripted_pair([], "MARKER DELTA here.")
    sess, ledger = _sub_session(two_commit_repo, tmp_path, "prA-r1", model)
    asyncio.run(sess.sweep("Review.", ledger))
    assert asyncio.run(sess._item_count()) > 0
    # A separate PR/tag starts empty.
    other, _ = _sub_session(two_commit_repo, tmp_path, "prB-r1", model)
    assert asyncio.run(other._item_count()) == 0
    # Re-initialising the same tag cleans up (no stale inheritance).
    again, _ = _sub_session(two_commit_repo, tmp_path, "prA-r1", model)
    assert asyncio.run(again._item_count()) == 0


def test_followup_cap_refuses_without_inference(two_commit_repo, tmp_path):
    model = _scripted_pair([], "only sweep.")
    sess, ledger = _sub_session(two_commit_repo, tmp_path, "cap-r1", model, max_followups=0)
    asyncio.run(sess.sweep("Review.", ledger))
    out = asyncio.run(sess.ask("q?", "h", "", ledger))
    assert out["status"] == "cap_refused"


def test_subagent_timeout_surfaces_error(two_commit_repo, tmp_path,
                                         monkeypatch):
    import asyncio as _aio

    async def _slow(*a, **k):
        await _aio.sleep(30)
        raise AssertionError("should have timed out")
    agents = runtime_sdk.require_sdk()
    monkeypatch.setattr(agents.Runner, "run", staticmethod(_slow))
    model = _scripted_pair([], "unused")
    sess, ledger = _sub_session(two_commit_repo, tmp_path, "to-r1", model,
                                 followup_timeout_s=0.2)
    out = asyncio.run(sess.ask("q?", "h?", "", ledger))
    assert out["status"].startswith("error:")
    assert "imeout" in out["status"] or "imeout" in out["text"]


def test_interruption_leaves_no_partial_entry(two_commit_repo, tmp_path,
                                              monkeypatch):
    import asyncio as _aio
    started = _aio.Event()

    async def _hang(*a, **k):
        started.set()
        await _aio.sleep(60)
        return None
    agents = runtime_sdk.require_sdk()
    monkeypatch.setattr(agents.Runner, "run", staticmethod(_hang))
    model = _scripted_pair([], "unused")
    sess, ledger = _sub_session(two_commit_repo, tmp_path, "int-r1", model,
                                 followup_timeout_s=30.0)
    async def _main():
        task = _aio.create_task(
            sess.ask("q?", "h?", "", ledger))
        await started.wait()
        task.cancel()
        with pytest.raises(_aio.CancelledError):
            await task
    asyncio.run(_main())
    # No partial ledger entry was recorded; session still queryable.
    assert asyncio.run(sess._item_count()) >= 0
    assert len(sess.calls) == 0


def test_lead_wiring_end_to_end_scripted(two_commit_repo, tmp_path,
                                         monkeypatch):
    from codatiel_i2 import providers as providers_mod
    repo = two_commit_repo
    sub_model = _scripted_pair(
        [], "SUB SWEEP: timeout bump src/app.py:1, P3.",
        extra_finals=("SUB FOLLOWUP: callers unchecked, P2 candidate "
                      "src/app.py:1.",))
    real_build = providers_mod.build_model

    def _fake_build(name):
        if name == "luna":
            from agents.model_settings import ModelSettings
            return sub_model, ModelSettings(store=False)
        return real_build(name)
    monkeypatch.setattr(providers_mod, "build_model", _fake_build)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    lead_final = {"status": "finding", "summary": "s",
                  "findings": [{"title": "t",
                                "severity": "P2",
                                "anchor": "src/app.py:1",
                                "evidence": "SUB FOLLOWUP",
                                "rationale": "via subagent"}]}
    lead_model = _scripted_pair(
        [runtime_sdk.scripted_tool_call(
            call_id="lead_0", name="read_file",
            arguments={"path": "src/app.py", "revision": "head",
                       "start_line": 1, "end_line": 0},
            response_id="lead_r0"),
         runtime_sdk.scripted_tool_call(
            call_id="lead_1", name="delegate_investigation",
            arguments={"question": "check callers",
                       "hypothesis": "unchecked",
                       "scope_paths": "src/"},
            response_id="lead_r1")],
        json.dumps(lead_final))
    spec = delegate_mod.DelegationSpec(
        tag="B-e2e-r1", artifact_dir=tmp_path / "deleg",
        subs=("luna",))
    out = service_mod.review_local(
        repo["repo"], repo["base_sha"], repo["head_sha"],
        model=lead_model, model_name="scripted:lead",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        max_turns=6, model_settings=None,
        rate={"input": 0.0, "output": 0.0}, rate_status="test",
        max_usd_per_review=1.0, tool_budget=10,
        inventory_strategy="bundle-0.1", enable_final=False,
        delegation=spec)
    assert out["status"] == "complete", out
    run_dir = Path(out["run_dir"])
    metrics = json.loads((run_dir / "metrics.json").read_text(
        encoding="utf-8"))
    deleg = metrics.get("delegation", {})
    assert deleg["subs"]["luna"]["followups_used"] == 1
    calls = deleg["subs"]["luna"]["calls"]
    assert calls[0]["kind"] == "sweep" and calls[0]["status"] == "ok"
    assert calls[1]["kind"] == "followup"
    assert calls[1].get("resumption_ok") is True
    ledger_lines = (tmp_path / "deleg" / "ledger.jsonl").read_text(
        encoding="utf-8").strip().split("\n")
    assert len(ledger_lines) == 2
    # Parent final carried the subagent's follow-up evidence.
    raws = (run_dir / "model_findings_raw.jsonl").read_text(encoding="utf-8")
    assert "SUB FOLLOWUP" in raws
