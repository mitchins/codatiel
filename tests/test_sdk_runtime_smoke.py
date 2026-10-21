"""Mock-inference smoke test for the canonical SDK runtime.

A scripted model (no paid inference, no network) drives REAL I2 source tools
against pinned BASE/HEAD fixtures through ``Runner.run``. This proves the
canonical loop — offer -> model-directed request -> execute -> bound
evidence -> continue — works end to end. It is a runtime smoke signal, not a
review result and not qualification evidence.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2.core.evidence import (  # noqa: E402
    replay_source_evidence,
    validate_envelope,
)
from codatiel_i2.runtime_sdk import (  # noqa: E402
    ReviewContext,
    ScriptedModel,
    build_i2_tools,
    ensure_local_tracing,
    run_review,
    scripted_final,
    scripted_tool_call,
)

BASE_PY = (
    "THRESHOLD = 2\n"
    "\n"
    "def should_recover(failing):\n"
    "    return len(failing) < THRESHOLD\n"
)
HEAD_PY = (
    "THRESHOLD = 2\n"
    "\n"
    "def should_recover(failing):\n"
    "    if len(failing) >= THRESHOLD:\n"
    "        return False\n"
    "    return True\n"
)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture()
def ctx(tmp_path: Path) -> ReviewContext:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "smoke@test")
    _git(repo, "config", "user.name", "smoke")
    (repo / "policy.py").write_text(BASE_PY)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "policy.py").write_text(HEAD_PY)
    _git(repo, "commit", "-qam", "head")
    head_sha = _git(repo, "rev-parse", "HEAD")

    # Production-faithful checkouts: real worktrees holding the pinned SHAs,
    # with marker files as the fallback binding path.
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    _git(repo, "worktree", "add", "--detach", str(base_dir), base_sha)
    _git(repo, "worktree", "add", "--detach", str(head_dir), head_sha)
    (base_dir / ".codatiel_i2_sha").write_text(base_sha)
    (head_dir / ".codatiel_i2_sha").write_text(head_sha)
    return ReviewContext(repo="smoke/repo", base_sha=base_sha,
                         head_sha=head_sha, base_dir=base_dir,
                         head_dir=head_dir, diff_repo=repo)


def test_sdk_runtime_mock_smoke(ctx: ReviewContext):
    ensure_local_tracing()
    tools = build_i2_tools(ctx)
    assert {t.name for t in tools} == {"read_file", "diff_window",
                                        "search_code", "find_definition",
                                        "find_callers", "related_files",
                                        "lookup_docs"}

    model = ScriptedModel([
        scripted_tool_call(call_id="call_1", name="read_file",
                           arguments={"path": "policy.py", "revision": "head"},
                           response_id="r1"),
        scripted_tool_call(call_id="call_2", name="diff_window",
                           arguments={"path": "policy.py"},
                           response_id="r2"),
        scripted_tool_call(call_id="call_3", name="search_code",
                           arguments={"query": "THRESHOLD", "revision": "head"},
                           response_id="r3"),
        scripted_final("MOCK complete: threshold guard added.", response_id="r4"),
    ])
    assert ScriptedModel.is_scripted is True
    result = run_review(
        instructions="You investigate code changes with the available tools.",
        user_input="Review the change to policy.py between BASE and HEAD.",
        tools=tools, model=model, max_turns=8)

    assert model.calls == 4
    assert [e["tool"] for e in ctx.tool_log] == ["read_file", "diff_window",
                                                "search_code"]
    assert result.final_output is not None
    assert "MOCK complete" in result.final_output

    read_out = json.loads(_tool_output_by_call(result, "call_1"))
    assert "if len(failing) >= THRESHOLD" in read_out["evidence"]["excerpt"]
    assert read_out["evidence"]["revision"] == "head"
    assert read_out["evidence"]["revision_sha"] == ctx.head_sha
    assert validate_envelope(read_out["evidence"]) == []
    assert replay_source_evidence(
        read_out["evidence"], base_checkout=ctx.base_dir,
        head_checkout=ctx.head_dir) == []

    diff_out = json.loads(_tool_output_by_call(result, "call_2"))
    assert diff_out["evidence"]["kind"] == "diff_hunk"
    assert validate_envelope(diff_out["evidence"]) == []
    assert replay_source_evidence(
        diff_out["evidence"], base_checkout=ctx.base_dir,
        head_checkout=ctx.head_dir, changed_paths={"policy.py"}) == []

    search_out = json.loads(_tool_output_by_call(result, "call_3"))
    assert isinstance(search_out["evidence"], list) and search_out["evidence"]
    assert {e["revision_sha"] for e in search_out["evidence"]} == {ctx.head_sha}
    for item in search_out["evidence"]:
        assert validate_envelope(item) == []

    # Negative: excerpt tampering must fail envelope validation (the digest
    # binds the exact bytes), not pass vacuously.
    tampered = dict(read_out["evidence"])
    tampered["excerpt"] = tampered["excerpt"].replace("THRESHOLD", "TAMPERED")
    assert validate_envelope(tampered) != []

    assert _tracing_disabled()


def test_sdk_runtime_rejects_wrong_checkout_sha(ctx: ReviewContext,
                                                tmp_path: Path):
    """SHA binding is enforced, not merely recorded: a checkout holding the
    wrong SHA must fail the read instead of returning evidence."""
    import shutil
    bad_head = tmp_path / "bad_head"
    shutil.copytree(ctx.head_dir, bad_head,
                    ignore=shutil.ignore_patterns(".git"))
    (bad_head / ".codatiel_i2_sha").write_text("0" * 40)
    bad_ctx = ReviewContext(repo=ctx.repo, base_sha=ctx.base_sha,
                            head_sha=ctx.head_sha, base_dir=ctx.base_dir,
                            head_dir=bad_head, diff_repo=ctx.diff_repo)
    tools = build_i2_tools(bad_ctx)
    model = ScriptedModel([
        scripted_tool_call(call_id="call_9", name="read_file",
                           arguments={"path": "policy.py", "revision": "head"},
                           response_id="r9"),
        scripted_final("MOCK done.", response_id="r10"),
    ])
    result = run_review(
        instructions="You investigate code changes with the available tools.",
        user_input="Review the change to policy.py.",
        tools=tools, model=model, max_turns=4)
    out = json.loads(_tool_output_by_call(result, "call_9"))
    assert out.get("status") == "error"
    assert "SHA" in out.get("error", "").upper() or "sha" in out.get("error", "")
    assert bad_ctx.tool_log[0]["summary"].get("evidence_ids") is None


def _tool_output_by_call(result, call_id: str) -> str:
    for item in result.new_items:
        if getattr(item, "type", "") == "tool_call_output_item" \
                and getattr(item, "call_id", None) == call_id:
            return str(item.output)
    raise AssertionError(f"no transcript output found for {call_id}")


def _tracing_disabled() -> bool:
    import agents.tracing as tracing
    provider = tracing.get_trace_provider()
    return bool(getattr(provider, "_disabled", False))
