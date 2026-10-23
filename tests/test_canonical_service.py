"""M1 gate: scripted-model smoke through the full canonical service path.

Real workspace, real I2 tools via Runner, deterministic candidate prep,
disposition, trust validation, immutable run record, shadow result.
Proves the downstream path retains a useful grounded candidate and correctly
rejects unevidenced claims, silence abuse, and manufactured proof.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import service as service_mod  # noqa: E402
from codatiel_i2.core import workspace as workspace_mod  # noqa: E402
from codatiel_i2.core.runs import required_artifacts  # noqa: E402
from codatiel_i2.tools import diff_window as diff_tool  # noqa: E402
from codatiel_i2.tools import read_file as read_tool  # noqa: E402

BASE_APP = "LIMIT = 100\n\n\ndef handle(n):\n    return min(n, LIMIT)\n"
HEAD_APP = "LIMIT = 10\n\n\ndef handle(n):\n    return min(n, LIMIT)\n"


@pytest.fixture()
def repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "m1@test")
    _git(root, "config", "user.name", "m1")
    (root / "app.py").write_text(BASE_APP)
    (root / "README.md").write_text("# config\n\nLIMIT defaults to 100\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text(HEAD_APP)
    _git(root, "commit", "-qam", "head")
    head = _git(root, "rev-parse", "HEAD")
    return {"root": root, "base": base, "head": head}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                         text=True, check=True, timeout=60)
    return out.stdout.strip()


def _scenario(tmp_path: Path, name: str, turns: list, final: dict) -> str:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"turns": turns, "final": final}))
    return str(path)


def _cross_file_evidence_ids(repo: dict, work_root: Path) -> list[str]:
    """Precompute the exact evidence IDs the scripted tool turns will mint.

    Evidence IDs are content-addressed over pinned SHAs, revisions, paths,
    ranges, bytes, and creation parameters, so reading the same spans through
    the same tools before the run yields the IDs the run itself will return.
    """
    work = workspace_mod.prepare_local_workspace(repo["root"], repo["base"], repo["head"], work_root)
    assert work.status == "ready"
    read_app = read_tool.read_file(Path(work.head_dir), "app.py", base_sha=repo["base"],
                                   head_sha=repo["head"], revision="head",
                                   start_line=1, end_line=1)["evidence"]
    read_docs = read_tool.read_file(Path(work.head_dir), "README.md", base_sha=repo["base"],
                                    head_sha=repo["head"], revision="head",
                                    start_line=1, end_line=2)["evidence"]
    diff_app = diff_tool.diff_window(repo["root"], repo["base"], repo["head"], "app.py")["evidence"]
    return [read_app["evidence_id"], read_docs["evidence_id"], diff_app["evidence_id"]]


GROUNDED_TURNS = [
    {"tool": "read_file",
     "args": {"path": "app.py", "revision": "head", "start_line": 1,
              "end_line": 1}},
    {"tool": "read_file",
     "args": {"path": "README.md", "revision": "head", "start_line": 1,
              "end_line": 2}},
    {"tool": "diff_window", "args": {"path": "app.py"}},
]


def test_grounded_candidate_retained(repo: dict, tmp_path: Path):
    cited = _cross_file_evidence_ids(repo, tmp_path / "work")
    model = service_mod.scripted_model_from_scenario(_scenario(
        tmp_path, "grounded", GROUNDED_TURNS,
        {"status": "finding", "summary": "LIMIT lowered",
         "findings": [{"detector": "docs_code", "title": "limit changed",
                        "path": "app.py", "revision": "head",
                        "line_start": 1, "line_end": 1,
                        "claim": "LIMIT dropped 100 to 10",
                        "evidence_ids": cited}]}))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="scripted:m1", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    assert out["status"] == "complete"
    run_dir = Path(out["run_dir"])
    for name in required_artifacts(run_dir):
        assert (run_dir / name).exists(), name
    for name in ("run.json", "findings.jsonl", "evidence.jsonl",
                 "transcript.json", "shadow_result.json",
                 "model_findings_raw.jsonl"):
        assert (run_dir / name).exists(), name
    rows = [json.loads(line) for line in
            (run_dir / "findings.jsonl").read_text().splitlines() if line.strip()]
    agent_rows = [r for r in rows if r["finding_id"].startswith("agent-")]
    assert len(agent_rows) == 1
    assert agent_rows[0]["disposition"] == "advisory_grounded"
    assert agent_rows[0]["evidence_ids"] == cited
    assert agent_rows[0]["evidence_attestation"]["state"] == "ATTESTED"
    assert agent_rows[0]["substantive_assessment"] == "UNASSESSED"
    ledger_ids = {json.loads(line)["evidence_id"] for line in
                  (run_dir / "evidence.jsonl").read_text().splitlines() if line.strip()}
    assert set(agent_rows[0]["evidence_ids"]) <= ledger_ids
    shadow = json.loads((run_dir / "shadow_result.json").read_text())
    assert shadow["post_to_github"] is False
    assert shadow["counts"]["advisory_grounded"] >= 1
    sent: list[dict] = []

    def fixture_transport(to: list[str], subject: str, body: str) -> dict:
        sent.append({"to": to, "subject": subject, "body": body})
        return {"accepted_by_transport": True, "parsed": True, "fixture": True,
                "succeeded": len(to), "failed": 0, "email_id": "fixture-no-send"}

    service_mod.deliver_shadow(run_dir, to=["codatiel-test@mitchellcurrie.com"],
                               sender="codatiel@mitchellcurrie.com", transport=fixture_transport)
    assert len(sent) == 1
    body = sent[0]["body"]
    # Test the actual rendered finding line, not just the message as a whole:
    # a finding labelled UNASSESSED must never be described as a confirmed
    # defect on its own line.
    finding_lines = [line for line in body.splitlines() if "agent-000" in line]
    assert len(finding_lines) == 1
    rendered = finding_lines[0]
    assert "unverified review candidate" in rendered
    assert "UNASSESSED" in rendered
    assert "confirmed defect" not in rendered
    # Deterministic detector prep must fire on the LIMIT contradiction and
    # pass full trust validation with its registered proof.
    det_rows = [r for r in rows if not r["finding_id"].startswith("agent-")]
    assert det_rows, "docs_code detector produced no findings on a real contradiction"
    assert det_rows[0]["disposition"] == "verified_proof", det_rows[0].get("reason")
    assert det_rows[0]["publish_eligible"] is False


def test_missing_evidence_rejected_not_refuted(repo: dict, tmp_path: Path):
    # Submitted IDs use the valid syntax but were not returned by any tool in
    # this run. Under provenance-only validation they are rejected for
    # non-membership, never silently completed or substituted.
    fabricated = ["ev_" + "a" * 16, "ev_" + "b" * 16]
    model = service_mod.scripted_model_from_scenario(_scenario(
        tmp_path, "unevidenced",
        [{"tool": "diff_window", "args": {"path": "README.md"}}],
        {"status": "finding", "summary": "x",
         "findings": [{"detector": "contracts", "title": "t",
                        "path": "app.py", "revision": "head",
                        "line_start": 1, "line_end": 1,
                        "claim": "never read this span",
                        "evidence_ids": [fabricated[0]]},
                       {"detector": "contracts", "title": "ghost",
                        "path": "ghost.py", "revision": "head",
                        "line_start": 1, "line_end": 1,
                        "claim": "file outside the changed set",
                        "evidence_ids": [fabricated[1]]}]}))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="scripted:m1", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    assert out["status"] == "complete"
    rows = [json.loads(line) for line in
            (Path(out["run_dir"]) / "findings.jsonl").read_text().splitlines()
            if line.strip()]
    agent_rows = [r for r in rows if r["finding_id"].startswith("agent-")]
    assert len(agent_rows) == 2
    assert {r["disposition"] for r in agent_rows} == {"rejected_invalid"}
    assert [r["evidence_attestation"]["result_code"] for r in agent_rows] == [
        "EVIDENCE_NOT_RETURNED", "EVIDENCE_NOT_RETURNED"]
    assert agent_rows[0]["evidence_ids"] == [fabricated[0]]
    ghost = next(r for r in agent_rows
                 if r.get("changed_anchor", {}).get("path") == "ghost.py")
    assert "changed set" in ghost["reason"]


def test_zero_tool_run_is_incomplete(repo: dict, tmp_path: Path):
    model = service_mod.scripted_model_from_scenario(_scenario(
        tmp_path, "silent", [],
        {"status": "silence", "summary": "looked at nothing", "findings": []}))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="scripted:m1", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    assert out["status"] == "incomplete_no_tool_use"


def test_manufactured_proof_rejected(repo: dict, tmp_path: Path):
    model = service_mod.scripted_model_from_scenario(_scenario(
        tmp_path, "forged", GROUNDED_TURNS,
        {"status": "finding", "summary": "x",
         "findings": [{"detector": "docs_code", "title": "t",
                        "path": "app.py", "revision": "head",
                        "line_start": 1, "line_end": 1,
                        "claim": "verified!", "proof": "verified"}]}))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="scripted:m1", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    rows = [json.loads(line) for line in
            (Path(out["run_dir"]) / "findings.jsonl").read_text().splitlines()
            if line.strip()]
    agent_rows = [r for r in rows if r["finding_id"].startswith("agent-")]
    assert agent_rows[0]["disposition"] in ("rejected_invalid",
                                            "refuted_unsupported")
    assert agent_rows[0]["reason"].startswith("MODEL_VERIFIED_PROOF_REJECTED")
    assert "manufacture" in agent_rows[0]["reason"]
    assert "verified_proof" not in {
        r.get("disposition") for r in rows if r["finding_id"].startswith("agent-")}


def test_cli_review_local_end_to_end(repo: dict, tmp_path: Path):
    from codatiel_i2.cli import build_parser, cmd_review

    scenario = _scenario(tmp_path, "cli", GROUNDED_TURNS,
                         {"status": "silence", "summary": "checked",
                          "findings": []})
    args = build_parser().parse_args([
        "review", "--repo-root", str(repo["root"]), "--base", repo["base"],
        "--head", repo["head"], "--model", f"scripted:{scenario}",
        "--runs-root", str(tmp_path / "runs"),
        "--work-root", str(tmp_path / "work")])
    payload = cmd_review(args)
    assert payload["status"] == "complete"


def test_cli_review_rejects_unknown_model(repo: dict, tmp_path: Path):
    from codatiel_i2.cli import build_parser, cmd_review

    args = build_parser().parse_args([
        "review", "--repo-root", str(repo["root"]), "--base", repo["base"],
        "--head", repo["head"], "--model", "sol",
        "--runs-root", str(tmp_path / "runs"),
        "--work-root", str(tmp_path / "work")])
    payload = cmd_review(args)
    assert payload["status"] == "failed"
    assert "luna|haiku|gemma" in payload["error"]


def test_review_pr_failure_is_explicit(tmp_path: Path):
    # No network success required: an unresolvable repo must fail loudly at
    # metadata stage, never silently and never as a review.
    out = service_mod.review_pr(
        "no-such-owner-xyz/no-such-repo-xyz", 1,
        model=service_mod.scripted_model_from_scenario(
            _scenario(tmp_path, "pr", [],
                        {"status": "silence", "summary": "", "findings": []})),
        model_name="scripted:pr", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    assert out["status"] == "failed"
    assert out["stage"] in ("pr_metadata", "workspace")


def test_review_pr_enforces_frozen_shas(tmp_path: Path, monkeypatch):
    # M9.1 Phase A3: frozen roster SHAs abort on live-PR drift instead of
    # silently reviewing the wrong revision.
    import codatiel_i2.service as svc

    def _fake_meta(repo, pr):
        return ({"baseRefOid": "a" * 40, "headRefOid": "b" * 40,
                 "baseRefName": "main", "headRefName": "feat",
                 "state": "OPEN", "title": "t", "url": "u"}, None)

    monkeypatch.setattr(svc, "fetch_github_pr_metadata", _fake_meta)
    out = svc.review_pr(
        "o/r", 1,
        model=svc.scripted_model_from_scenario(
            _scenario(tmp_path, "pr", [],
                        {"status": "silence", "summary": "", "findings": []})),
        model_name="scripted:pr", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work",
        expected_base="c" * 40, expected_head="b" * 40)
    assert out["status"] == "failed"
    assert out["stage"] == "sha_mismatch"
    assert "frozen base" in out["error"]
    # Matching SHAs (including short prefixes) pass the gate to metadata
    # stage — here workspace prep fails loudly, which is the correct next
    # stage, proving the gate did not false-positive.
    out2 = svc.review_pr(
        "no-such-owner-xyz/no-such-repo-xyz", 1,
        model=svc.scripted_model_from_scenario(
            _scenario(tmp_path, "pr", [],
                        {"status": "silence", "summary": "", "findings": []})),
        model_name="scripted:pr", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work",
        expected_base="a" * 8, expected_head="b" * 8)
    assert out2["status"] == "failed"
    assert out2["stage"] in ("pr_metadata", "workspace")


def test_turn_budget_exhaustion_is_incomplete(repo: dict, tmp_path: Path):
    from codatiel_i2.runtime_sdk import ScriptedModel

    class _TurnsExceeded(Exception):
        pass
    _TurnsExceeded.__name__ = "MaxTurnsExceeded"

    class _Raising(ScriptedModel):
        async def get_response(self, *a, **k):
            raise _TurnsExceeded("too many turns")

    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=_Raising([]),
        model_name="scripted:m1x", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work")
    assert out["status"] == "incomplete_budget_exhausted"
    assert out["stage"] == "budget_exhausted"
