"""M9.2 P1: truthful prompt provenance regression tests.

1. If a run record declares a playbook, the submitted instructions contain
   that playbook's content (hash match). Otherwise the test fails.
2. The stored instructions hash equals the hash of what was actually passed
   to Agent(instructions=...) — captured at the SDK boundary, not from the
   constant.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import service as svc  # noqa: E402
from codatiel_i2 import runtime_sdk  # noqa: E402


def _repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", root, "init", "-q"], check=True, timeout=60)
    subprocess.run(["git", "-C", root, "config", "user.email", "t@t"],
                   check=True, timeout=60)
    subprocess.run(["git", "-C", root, "config", "user.name", "t"],
                   check=True, timeout=60)
    (root / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", root, "add", "."], check=True, timeout=60)
    subprocess.run(["git", "-C", root, "commit", "-qm", "b"], check=True,
                   timeout=60)
    base = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    (root / "a.py").write_text("x = 2\n")
    subprocess.run(["git", "-C", root, "commit", "-qam", "h"], check=True,
                   timeout=60)
    head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    return {"root": root, "base": base, "head": head}


def _silence_script():
    return [
        runtime_sdk.scripted_tool_call(
            call_id="c1", name="read_file",
            arguments={"path": "a.py", "revision": "head"},
            response_id="r1"),
        runtime_sdk.scripted_final(
            '{"status":"silence","summary":"ok","findings":[]}',
            response_id="r2"),
    ]


def test_playbook_declaration_requires_content_match(tmp_path: Path):
    """Declaring a playbook binds the record to its exact content."""
    repo = _repo(tmp_path)
    playbook = ("# Test playbook\n\n## 1. Orient\nRead the changed hunks.\n")
    playbook_sha = hashlib.sha256(playbook.encode()).hexdigest()
    m = runtime_sdk.ScriptedModel(list(_silence_script()))
    out = svc.review_local(
        repo["root"], repo["base"], repo["head"], model=m,
        model_name="scripted:prov", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", max_turns=5,
        playbook_name="test-playbook-0.1", playbook_text=playbook)
    assert out["status"] == "complete"
    prov = json.loads((Path(out["run_dir"]) / "prompt_provenance.json")
                      .read_text())
    assert prov["declared_playbook"] == "test-playbook-0.1"
    assert prov["declared_playbook_sha256"] == playbook_sha
    # The submitted instructions contain the playbook content (hash match).
    assert prov["playbook_content_match"] is True
    assert playbook in prov["instructions"]
    assert prov["instructions_sha256"] == hashlib.sha256(
        prov["instructions"].encode()).hexdigest()
    # Tamper check on the helper: wrong hash or absent content fails.
    assert svc.verify_playbook_containment(
        prov["instructions"], playbook, playbook_sha) is True
    assert svc.verify_playbook_containment(
        prov["instructions"], playbook, "0" * 64) is False
    assert svc.verify_playbook_containment(
        "unrelated text", playbook, playbook_sha) is False


def test_stored_hash_matches_sdk_boundary(tmp_path: Path):
    """Stored hash == hash of instructions object Agent received."""
    repo = _repo(tmp_path)
    m = runtime_sdk.ScriptedModel(list(_silence_script()))
    out = svc.review_local(
        repo["root"], repo["base"], repo["head"], model=m,
        model_name="scripted:prov", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", max_turns=5)
    assert out["status"] == "complete"
    prov = json.loads((Path(out["run_dir"]) / "prompt_provenance.json")
                      .read_text())
    # Baseline (no playbook): exact minimal prompt bytes, content-derived ID.
    assert prov["instructions"] == svc.CANONICAL_INSTRUCTIONS
    assert prov["instructions_id"] == svc.CANONICAL_INSTRUCTIONS_ID
    assert prov["declared_playbook"] is None
    # Captured at the SDK boundary, not copied from the constant.
    assert prov["sdk_boundary_instructions_sha256"] is not None
    assert (prov["sdk_boundary_instructions_sha256"]
            == prov["instructions_sha256"])
    assert prov["boundary_match"] is True
    assert prov["instructions_sha256"] == hashlib.sha256(
        svc.CANONICAL_INSTRUCTIONS.encode()).hexdigest()
    # First user message (inventory) stored with its own hash.
    assert "Repository:" in prov["user_input_first_message"]
    assert prov["user_input_sha256"] == hashlib.sha256(
        prov["user_input_first_message"].encode()).hexdigest()
    assert prov["inventory_strategy"] == svc.INVENTORY_STRATEGY
    assert prov["inventory_sha256"] == hashlib.sha256(
        prov["inventory"].encode()).hexdigest()


def test_failed_run_still_records_provenance(tmp_path: Path):
    """Budget-exhausted runs carry the same provenance record."""
    repo = _repo(tmp_path)
    script = [
        runtime_sdk.scripted_tool_call(
            call_id="c1", name="read_file",
            arguments={"path": "a.py", "revision": "head"},
            response_id="r1"),
        runtime_sdk.scripted_tool_call(
            call_id="c2", name="diff_window",
            arguments={"path": "a.py"}, response_id="r2"),
    ]
    m = runtime_sdk.ScriptedModel(list(script))
    out = svc.review_local(
        repo["root"], repo["base"], repo["head"], model=m,
        model_name="scripted:prov", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", max_turns=1, variant="challenger")
    assert out["status"] == "incomplete_budget_exhausted"
    prov = json.loads((Path(out["run_dir"]) / "prompt_provenance.json")
                      .read_text())
    assert prov["instructions"] == svc.CHALLENGER_INSTRUCTIONS
    assert prov["instructions_id"] == svc.CHALLENGER_INSTRUCTIONS_ID
    assert prov["sdk_boundary_instructions_sha256"] is None
    assert prov["boundary_match"] is None
