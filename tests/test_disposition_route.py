"""M3 gate: single disposition route over real detector output.

Real I2 evidence and verification determine the final claim:
- a tampered detector finding must downgrade to unresolved, never verified;
- an examined-but-agreeing pair must surface as observed_not_material, never
  promoted to a finding;
- silence and parse failures must be recorded, not lost.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import service as service_mod  # noqa: E402
from codatiel_i2.core import diff as diff_mod  # noqa: E402
from codatiel_i2.core import runs as runs_mod  # noqa: E402
from codatiel_i2.core.workspace import prepare_local_workspace  # noqa: E402
from codatiel_i2.detectors.docs_code import detector as det_mod  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                         text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture()
def contradicting(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "m3@t")
    _git(root, "config", "user.name", "m3")
    (root / "app.py").write_text("LIMIT = 100\n")
    (root / "README.md").write_text("LIMIT defaults to 100\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 10\n")
    _git(root, "commit", "-qam", "head")
    return {"root": root, "base": _git(root, "rev-parse", base),
            "head": _git(root, "rev-parse", "HEAD")}


@pytest.fixture()
def agreeing(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "m3@t")
    _git(root, "config", "user.name", "m3")
    (root / "app.py").write_text("LIMIT = 100\n")
    (root / "README.md").write_text("LIMIT defaults to 100\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 100  # still 100\n")
    _git(root, "commit", "-qam", "head")
    return {"root": root, "base": _git(root, "rev-parse", base),
            "head": _git(root, "rev-parse", "HEAD")}


def _scan(fix: dict, tmp_path: Path) -> dict:
    ws = prepare_local_workspace(fix["root"], fix["base"], fix["head"],
                                 tmp_path / "work")
    assert ws.status == "ready", ws.error
    changes = diff_mod.changed_files(fix["root"], ws.base_sha, ws.head_sha)
    scan = det_mod.scan_pair(
        base_dir=ws.base_dir, head_dir=ws.head_dir, base_sha=ws.base_sha,
        head_sha=ws.head_sha, repo="m3", changes=changes)
    return {"ws": ws, "changes": changes, "scan": scan}


def test_tampered_detector_finding_downgrades(contradicting: dict,
                                              tmp_path: Path):
    parts = _scan(contradicting, tmp_path)
    ws, changes, scan = parts["ws"], parts["changes"], parts["scan"]
    assert scan["findings"], "fixture must yield a real detector finding"
    tampered = [dict(f) for f in scan["findings"]]
    tampered[0]["head_state"] = "TAMPERED"
    rows, _ = service_mod._dispose_detector_findings(
        tampered, scan["evidence"], scan["verifications"], scan["facts"],
        scan["proofs"], scan["candidates"], Path(ws.base_dir),
        Path(ws.head_dir), {c["path"] for c in changes},
        ws.base_sha, ws.head_sha)
    assert rows
    assert rows[0]["disposition"] == "unresolved"
    assert "verified_proof" not in {r["disposition"] for r in rows}


def test_agreeing_pair_is_observed_not_finding(agreeing: dict,
                                               tmp_path: Path):
    parts = _scan(agreeing, tmp_path)
    ws, changes, scan = parts["ws"], parts["changes"], parts["scan"]
    rows, observations = service_mod._dispose_detector_findings(
        scan["findings"], scan["evidence"], scan["verifications"],
        scan["facts"], scan["proofs"], scan["candidates"],
        Path(ws.base_dir), Path(ws.head_dir),
        {c["path"] for c in changes}, ws.base_sha, ws.head_sha)
    assert not [r for r in rows if r.get("disposition") == "verified_proof"]
    assert observations, "examined pair must be observed, not dropped"
    assert all(o["disposition"] == "observed_not_material" for o in observations)


def test_service_records_observation_silence_and_notes(agreeing: dict,
                                                      tmp_path: Path):
    scenario = tmp_path / "m3.json"
    scenario.write_text(json.dumps({
        "turns": [{"tool": "read_file",
                   "args": {"path": "app.py", "revision": "head"}}],
        "final": {"status": "silence", "summary": "values agree",
                  "findings": []}}))
    out = service_mod.review_local(
        agreeing["root"], agreeing["base"], agreeing["head"],
        model=service_mod.scripted_model_from_scenario(scenario),
        model_name="scripted:m3", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    assert out["status"] == "complete"
    run_dir = Path(out["run_dir"])
    obs = [json.loads(line) for line in
           (run_dir / "observations.jsonl").read_text().splitlines() if line.strip()]
    assert obs and all(o["disposition"] == "observed_not_material" for o in obs)
    shadow = json.loads((run_dir / "shadow_result.json").read_text())
    assert shadow["silence"] == {"summary": "values agree", "tool_calls": 1}
    assert shadow["counts"]["observed_not_material"] == len(obs)


def test_parse_failure_is_noted_not_lost(contradicting: dict,
                                         tmp_path: Path):
    scenario = tmp_path / "m3bad.json"
    scenario.write_text(json.dumps({
        "turns": [{"tool": "read_file",
                   "args": {"path": "app.py", "revision": "head"}}],
        "final": "not json at all"}))
    out = service_mod.review_local(
        contradicting["root"], contradicting["base"],
        contradicting["head"],
        model=service_mod.scripted_model_from_scenario(scenario),
        model_name="scripted:m3", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    assert out["status"] == "unresolved_unparseable"
    # Unparseable finals must be recorded as unresolved, not as correct silence
    # (POST-02: model/provider output failure, not silence)
    shadow = json.loads(
        (Path(out["run_dir"]) / "shadow_result.json").read_text())
    assert shadow["investigation_notes"], "parse failure must be recorded"
    assert shadow["silence"] is None
    assert shadow["status"] == "unresolved_unparseable"
