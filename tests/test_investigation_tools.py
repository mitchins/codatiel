"""Investigation-tool behavior through the canonical SDK runtime.

Covers the M2-consolidated wrappers: real dispatch, honest limits
(find_callers is call-syntax-only; related_files carries no evidence;
lookup_docs is cache-only), and evidence binding where applicable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import runtime_sdk  # noqa: E402
from codatiel_i2.core.evidence import validate_envelope  # noqa: E402

BASE_PY = "LIMIT = 100\n\n\ndef handle(n):\n    return min(n, LIMIT)\n\n\ndef run(items):\n    return [handle(x) for x in items]\n"


@pytest.fixture()
def ctx(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True,
                   timeout=60)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True, timeout=60)
    (repo / "app.py").write_text(BASE_PY)
    (repo / "util.py").write_text("HELPER = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                   timeout=60)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"],
                   check=True, timeout=60)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True,
                         timeout=60).stdout.strip()
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach",
                    str(base_dir), sha], check=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach",
                    str(head_dir), sha], check=True, timeout=60)
    (base_dir / ".codatiel_i2_sha").write_text(sha)
    (head_dir / ".codatiel_i2_sha").write_text(sha)
    return runtime_sdk.ReviewContext(
        repo="tools/repo", base_sha=sha, head_sha=sha, base_dir=base_dir,
        head_dir=head_dir, diff_repo=repo)


def _run(ctx, turns, final="done"):
    script = [runtime_sdk.scripted_tool_call(
        call_id=f"c{i}", name=name, arguments=args,
        response_id=f"r{i}") for i, (name, args) in enumerate(turns)]
    script.append(runtime_sdk.scripted_final(final, response_id="rf"))
    model = runtime_sdk.ScriptedModel(script)
    result = runtime_sdk.run_review(
        instructions="Use the tools.", user_input="investigate",
        tools=runtime_sdk.build_i2_tools(ctx), model=model, max_turns=8)
    outputs = {}
    for item in result.new_items:
        if getattr(item, "type", "") == "tool_call_output_item":
            outputs[getattr(item, "call_id", "")] = json.loads(str(item.output))
    return result, outputs


def test_find_definition_and_callers(ctx):
    _, outputs = _run(ctx, [
        ("find_definition", {"symbol": "handle", "revision": "head"}),
        ("find_callers", {"symbol": "handle", "revision": "head"}),
        ("find_callers", {"symbol": "LIMIT", "revision": "head"}),
        ("search_code", {"query": "LIMIT", "revision": "head"}),
    ])
    defs = outputs["c0"]
    assert defs["definitions"]
    assert all(validate_envelope(e) == [] for e in defs["evidence"])
    callers = outputs["c1"]
    matches = [c.get("match", "") for c in callers["callers"]]
    assert any("handle(" in m and not m.startswith("def ") for m in matches), matches
    # Documented limit: constant reads are invisible to find_callers...
    assert outputs["c2"]["callers"] == []
    # ...while search_code sees them (use it for constants/imports).
    assert len(outputs["c3"]["results"]) >= 2


def test_related_files_and_lookup_docs_limits(ctx):
    _, outputs = _run(ctx, [
        ("related_files", {"path": "app.py"}),
        ("lookup_docs", {"query": "limits", "package": "", "version": ""}),
    ])
    related = outputs["c0"]["related"]
    assert any(r["path"] == "util.py" for r in related)
    assert "evidence" not in outputs["c0"]  # navigation aid: no envelope claimed
    docs = outputs["c1"]
    assert docs["status"] == "not_found"
    assert any("fallback disabled" in w for w in docs["warnings"])
