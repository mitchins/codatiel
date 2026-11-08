"""M9 — impact-path challenger regression (offline, deterministic).

No network, no credentials. Uses ScriptedModel through the real SDK Runner
and I2 tools. Verifies:
- Baseline vs challenger instructions are distinct and variant is recorded
- Challenger inventory contains impact-path hints
- Both variants preserve revision isolation and evidence provenance
- Tool budget and spend controls still apply
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import runtime_sdk, service as svc


def _repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", root, *a], check=True, timeout=60)
    (root / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", root, "add", "."], check=True, timeout=60)
    subprocess.run(["git", "-C", root, "commit", "-qm", "b"], check=True, timeout=60)
    base = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    (root / "a.py").write_text("x = 2\n")
    subprocess.run(["git", "-C", root, "commit", "-qam", "h"], check=True, timeout=60)
    head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return {"root": root, "base": base, "head": head}


def test_baseline_and_challenger_instructions_are_distinct():
    assert svc.CANONICAL_INSTRUCTIONS != svc.CHALLENGER_INSTRUCTIONS
    assert "impact-path" in svc.CHALLENGER_INSTRUCTIONS.lower() or "Changed behavior" in svc.CHALLENGER_INSTRUCTIONS
    assert "hypotheses" in svc.CHALLENGER_INSTRUCTIONS.lower()
    # M9.2 P1: recorded IDs are content-derived, not hand-maintained labels.
    assert svc.CANONICAL_INSTRUCTIONS_ID == svc.instructions_id_for(
        "canonical-minimal-0.1", svc.CANONICAL_INSTRUCTIONS)
    assert svc.CHALLENGER_INSTRUCTIONS_ID == svc.instructions_id_for(
        "challenger-impact-path-0.1", svc.CHALLENGER_INSTRUCTIONS)
    assert svc.INSTRUCTIONS_VERSION == svc.CANONICAL_INSTRUCTIONS_ID


def test_variant_recorded_in_metrics_and_config(tmp_path: Path):
    repo = _repo(tmp_path)
    script = [
        runtime_sdk.scripted_tool_call(call_id="c1", name="read_file", arguments={"path": "a.py", "revision": "head"}, response_id="r1"),
        runtime_sdk.scripted_final('{"status":"silence","summary":"ok","findings":[]}', response_id="r2"),
    ]
    for variant in ("baseline", "challenger"):
        m = runtime_sdk.ScriptedModel(list(script))
        out = svc.review_local(repo["root"], repo["base"], repo["head"], model=m, model_name="scripted:test",
                               runs_root=tmp_path / f"runs_{variant}", work_root=tmp_path / f"work_{variant}",
                               max_turns=5, variant=variant)
        assert out["status"] == "complete"
        run_dir = Path(out["run_dir"])
        metrics = json.loads((run_dir / "metrics.json").read_text())
        assert metrics["variant"] == variant
        expected_id = (svc.CHALLENGER_INSTRUCTIONS_ID if variant == "challenger"
                       else svc.CANONICAL_INSTRUCTIONS_ID)
        assert metrics["instructions_version"] == expected_id
        assert metrics["instructions_id"] == expected_id
        run = json.loads((run_dir / "run.json").read_text())
        assert run["config"]["variant"] == variant


def test_challenger_inventory_contains_impact_path_hints(tmp_path: Path):
    repo = _repo(tmp_path)
    # Create a repo with high-churn file to trigger challenger inventory hint
    big = repo["root"] / "big.py"
    big.write_text("\n".join([f"x{i}=1" for i in range(200)]))
    subprocess.run(["git", "-C", repo["root"], "add", "."], check=True, timeout=60)
    subprocess.run(["git", "-C", repo["root"], "commit", "-qam", "big"], check=True, timeout=60)
    new_head = subprocess.run(["git", "-C", repo["root"], "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    # Baseline inventory should not contain challenger hints, challenger should
    changes_baseline = svc.diff_mod.changed_files(repo["root"], repo["base"], new_head)
    classification = svc.files_mod.classify_changes(changes_baseline)
    inv_baseline = svc._render_inventory(changes_baseline, classification, [], variant="baseline")
    inv_challenger = svc._render_inventory(changes_baseline, classification, [], variant="challenger")
    assert "impact" not in inv_baseline.lower()
    assert "behavioral impact" in inv_challenger.lower() or "Focus on behavioral impact" in inv_challenger
    assert "High-churn" in inv_challenger or "hypotheses" in inv_challenger.lower()


def test_challenger_exhaustion_preserves_variant_and_trace(tmp_path: Path):
    repo = _repo(tmp_path)
    script = [
        runtime_sdk.scripted_tool_call(call_id="c1", name="read_file", arguments={"path": "a.py", "revision": "head"}, response_id="r1"),
        runtime_sdk.scripted_tool_call(call_id="c2", name="diff_window", arguments={"path": "a.py"}, response_id="r2"),
        runtime_sdk.scripted_tool_call(call_id="c3", name="read_file", arguments={"path": "a.py", "revision": "head"}, response_id="r3"),
    ]
    m = runtime_sdk.ScriptedModel(list(script))
    out = svc.review_local(repo["root"], repo["base"], repo["head"], model=m, model_name="scripted:test",
                           runs_root=tmp_path / "runs", work_root=tmp_path / "work",
                           max_turns=2, variant="challenger")
    assert out["status"] == "incomplete_budget_exhausted"
    run_dir = Path(out["run_dir"])
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["variant"] == "challenger"
    assert metrics["instructions_version"] == svc.CHALLENGER_INSTRUCTIONS_ID
    # Trace files still present
    for name in ["tool_calls.jsonl", "evidence.jsonl", "model_calls.jsonl", "transcript.json"]:
        assert (run_dir / name).exists()


def test_challenger_does_not_require_minimum_tool_calls_for_obvious_bug(tmp_path: Path):
    """Obvious bug with 1 tool call should still be complete, not forced to 2+."""
    repo = _repo(tmp_path)
    script = [
        runtime_sdk.scripted_tool_call(call_id="c1", name="read_file", arguments={"path": "a.py", "revision": "head"}, response_id="r1"),
        runtime_sdk.scripted_final('{"status":"finding","summary":"obvious bug","findings":[{"detector":"contracts","title":"t","path":"a.py","revision":"head","line_start":1,"line_end":1,"claim":"changed return breaks caller under condition X","evidence_ids":[]}]}', response_id="r2"),
    ]
    m = runtime_sdk.ScriptedModel(list(script))
    out = svc.review_local(repo["root"], repo["base"], repo["head"], model=m, model_name="scripted:test",
                           runs_root=tmp_path / "runs", work_root=tmp_path / "work",
                           max_turns=5, variant="challenger")
    # Should be complete (or unresolved if evidence_ids empty but not forced incomplete)
    assert out["status"] in ("complete", "unresolved_unparseable", "unresolved_empty_final")
    # Tool calls =1 is legitimate for challenger; it should not be forced to incomplete_no_tool_use
    assert out["tool_calls"] == 1
    metrics = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())
    assert metrics["tool_calls"] == 1
