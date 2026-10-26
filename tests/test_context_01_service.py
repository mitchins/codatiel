"""CONTEXT-01 service arms: bundle-cite citations validate under the
M7.1 contract, foreign IDs are rejected, arm B carries no evidence IDs,
and the fixed prompt stays byte-identical across arms. No inference.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import runtime_sdk
from codatiel_i2 import service as service_mod
from codatiel_i2 import context_bundle as CB
from codatiel_i2.core.files import classify_changes


def _run(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True,
                          timeout=60).stdout


def _repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, timeout=60)
    (root / "app.py").write_text(
        "LIMIT = 100\n"
        "\n"
        "def handler(request):\n"
        '    """Serve."""\n'
        "    return LIMIT\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "def test_handler():\n    assert handler(None)\n")
    (root / "README.md").write_text("demo\n")
    _run("add", ".", cwd=root)
    _run("commit", "-qm", "base", cwd=root)
    base = _run("rev-parse", "HEAD", cwd=root).strip()
    (root / "app.py").write_text(
        "LIMIT = 100\n"
        "MAX_RETRIES = 3\n"
        "\n"
        "def handler(request):\n"
        '    """Serve."""\n'
        "    return LIMIT\n"
        "\n"
        "def new_handler(request):\n"
        "    return MAX_RETRIES\n")
    (root / "tests" / "test_app.py").write_text(
        "def test_handler():\n    assert handler(None)\n\n\n"
        "def test_new_handler():\n    assert new_handler(None)\n")
    (root / "README.md").write_text("demo changed\n")
    (root / "compose.yaml").write_text(
        "services:\n  app:\n    environment:\n      YT2WIKI_ASR_URL: x\n")
    _run("add", ".", cwd=root)
    _run("commit", "-qm", "head", cwd=root)
    head = _run("rev-parse", "HEAD", cwd=root).strip()
    return {"root": root, "base": base, "head": head}


def _checkouts(repo: dict, tmp_path: Path) -> tuple[Path, Path, Path]:
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    _run("worktree", "add", "--detach", str(base_dir), repo["base"],
         cwd=repo["root"])
    _run("worktree", "add", "--detach", str(head_dir), repo["head"],
         cwd=repo["root"])
    return base_dir, head_dir, repo["root"]


def _changes(repo: dict) -> list[dict]:
    out = _run("diff", "--find-renames=50%", "--numstat", "-z",
               f"{repo['base']}..{repo['head']}", cwd=repo["root"])
    changes = []
    for record in out.split("\0"):
        if not record.strip():
            continue
        adds, dels, path = record.split("\t")
        changes.append({"path": path,
                        "additions": int(adds) if adds.isdigit() else 0,
                        "deletions": int(dels) if dels.isdigit() else 0,
                        "is_binary": False})
    return changes


def _final(evidence_id: str) -> dict:
    return {"status": "finding", "summary": "s",
            "findings": [{"detector": "contracts", "title": "t",
                          "path": "app.py", "revision": "head",
                          "line_start": 8, "line_end": 9,
                          "claim": "new handler ignores LIMIT",
                          "evidence_ids": [evidence_id]}]}


def _run_arm(tmp_path: Path, strategy: str, final: dict,
             with_tool: bool = True):
    case = tmp_path / strategy.replace("-", "_") / ("cite" if final[
        "findings"] else "silence")
    case.mkdir(parents=True)
    repo = _repo(case)
    base_dir, head_dir, _diff = _checkouts(repo, case / "wt")
    script = []
    if with_tool:
        script.append(runtime_sdk.scripted_tool_call(
            call_id="a", name="read_file",
            arguments={"path": "app.py", "revision": "head"},
            response_id="r1"))
    script.append(runtime_sdk.scripted_final(
        json.dumps(final), response_id="r2"))
    model = runtime_sdk.ScriptedModel(script)
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="scripted:ctx01", runs_root=case / "runs",
        work_root=case / "work", inventory_strategy=strategy)
    return out, repo


def test_bundle_cite_finding_attests(tmp_path):
    # Pre-compute the deterministic bundle to learn a real evidence ID,
    # then cite it in the scripted final.
    repo = _repo(tmp_path / "pre")
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path / "pre" / "wt")
    changes = _changes(repo)
    cls = classify_changes(changes)
    bundle = CB.build_change_bundle(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, classification=cls, base_dir=base_dir,
        head_dir=head_dir, with_evidence=True)
    target = next(e for e in bundle.evidence
                  if e["revision"] == "head" and e["path"] == "app.py")
    final = _final(target["evidence_id"])
    out, repo2 = _run_arm(tmp_path, "bundle-cite-0.1", final)
    assert out["status"] == "complete", out
    run_dir = Path(out["run_dir"])
    findings = [json.loads(x) for x in
                (run_dir / "findings.jsonl").read_text().splitlines()
                if x.strip()]
    model_rows = [f for f in findings
                  if f.get("finding_id", "").startswith("agent-")]
    assert model_rows, "the model finding must be disposed"
    row = model_rows[0]
    assert row["disposition"] == "advisory_grounded"
    att = row["evidence_attestation"]
    assert att["state"] == "ATTESTED"
    assert "bundle_evidence.jsonl" in [p.name for p in run_dir.iterdir()]
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["bundle"]["evidence_count"] == len(bundle.evidence)
    assert metrics["bundle"]["truncated"] is False


def test_bundle_cite_foreign_id_rejected(tmp_path):
    final = _final("ev_" + "f" * 16)
    out, _repo_d = _run_arm(tmp_path, "bundle-cite-0.1", final)
    assert out["status"] == "complete"
    run_dir = Path(out["run_dir"])
    findings = [json.loads(x) for x in
                (run_dir / "findings.jsonl").read_text().splitlines()
                if x.strip()]
    model_rows = [f for f in findings
                  if f.get("finding_id", "").startswith("agent-")]
    assert model_rows[0]["disposition"] == "rejected_invalid"


def test_bundle_arm_b_has_no_evidence_ids(tmp_path):
    out, _repo_d = _run_arm(
        tmp_path, "bundle-0.1",
        {"status": "silence", "summary": "checked", "findings": []})
    assert out["status"] == "complete"
    run_dir = Path(out["run_dir"])
    prov = json.loads((run_dir / "prompt_provenance.json").read_text())
    assert "CHANGE BUNDLE" in prov["inventory"]
    assert "ev_" not in prov["inventory"]
    assert not (run_dir / "bundle_evidence.jsonl").exists()
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["bundle"]["evidence_count"] == 0


def test_instructions_identical_across_arms(tmp_path):
    ids = []
    for strategy in ("file-list-0.1", "bundle-0.1", "bundle-cite-0.1"):
        out, _r = _run_arm(
            tmp_path, strategy,
            {"status": "silence", "summary": "checked", "findings": []})
        prov = json.loads(
            (Path(out["run_dir"]) / "prompt_provenance.json").read_text())
        ids.append(prov["instructions_id"])
    assert ids[0] == ids[1] == ids[2]


def test_unknown_strategy_still_fails(tmp_path):
    repo = _repo(tmp_path)
    model = runtime_sdk.ScriptedModel([])
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="scripted:ctx01", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", inventory_strategy="bundle-nonsense")
    assert out["status"] == "failed"
    assert out["stage"] == "inventory_strategy"
