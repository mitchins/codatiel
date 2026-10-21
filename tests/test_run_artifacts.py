"""Acceptance: immutable config-aware runs + end-to-end no-op scan.

Determinism scope (documented): same repo+base+head+config must yield the
same scan_key and identical semantic snapshots. Each execution gets a fresh
run_id and never mutates a previous run. Excluded from comparison:
wall-clock `run.created_at`, `run.run_id`, `metrics.runtime_seconds`, and
absolute worktree paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from codatiel_i2.cli import cmd_scan
from codatiel_i2.common.jsonio import canonical_json
from codatiel_i2.core import runs as runs_mod


class Args:
    def __init__(self, repo, base, head, runs_root, work_root, detectors="none"):
        self.repo = ""
        self.pr = ""
        self.repo_root = str(repo)
        self.base = base
        self.head = head
        self.runs_root = str(runs_root)
        self.work_root = str(work_root)
        self.detectors = detectors


def _deterministic_snapshot(run_dir: Path) -> dict:
    """All content artifacts with clock/identity fields normalised.

    Ledger rows carry wall-clock `at` + `latency_ms`: those are excluded
    from byte comparison (call presence/count still compared).
    """
    snap: dict = {}
    for name in ("pr.json", "changes.json", "file_classification.json",
                 "detector_coverage.json", "evidence.jsonl", "verifications.jsonl",
                 "findings.jsonl", "publish_preview.json", "dashboard.md"):
        snap[name] = (run_dir / name).read_text(encoding="utf-8")
    for det in runs_mod.CANDIDATE_FILES:
        snap[f"candidates/{det}.jsonl"] = (run_dir / "candidates" / f"{det}.jsonl").read_text(encoding="utf-8")
    for name in ("tool_calls.jsonl", "model_calls.jsonl"):
        rows = [json.loads(line) for line in (run_dir / name).read_text().splitlines() if line.strip()]
        for row in rows:
            row.pop("at", None)
            row.pop("latency_ms", None)
        snap[name] = canonical_json(rows)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run.pop("created_at", None)
    run.pop("run_id", None)
    ws = run.get("workspace") or {}
    ws.pop("base_dir", None)
    ws.pop("head_dir", None)
    run["workspace"] = ws
    snap["run.json"] = canonical_json(run)
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    metrics.pop("runtime_seconds", None)
    snap["metrics.json"] = canonical_json(metrics)
    return snap


def test_noop_scan_artifacts_and_determinism(two_commit_repo, tmp_path):
    repo = two_commit_repo["repo"]
    runs_root = tmp_path / "runs"

    first = cmd_scan(Args(repo, two_commit_repo["base_sha"], two_commit_repo["head_sha"],
                          runs_root, tmp_path / "work-a"))
    assert first["status"] == "ready", first
    run_dir_a = Path(first["run_dir"])
    for rel in runs_mod.required_artifacts(run_dir_a):
        assert (run_dir_a / rel).exists(), f"missing artifact: {rel}"
    findings = [json.loads(line) for line in (run_dir_a / "findings.jsonl").read_text().splitlines() if line.strip()]
    assert findings == []
    preview = json.loads((run_dir_a / "publish_preview.json").read_text())
    assert preview["mode"] == "report_only"
    assert preview["validation"] == "syntactic_only_never_publish"
    assert preview["publishable"] == []
    # call ledgers exist now (empty: the no-op path makes no calls)
    assert json.loads((run_dir_a / "tool_calls.jsonl").read_text() or "[]") == []
    snap_a = _deterministic_snapshot(run_dir_a)

    # Rescan: same scan_key, fresh run_id, previous run untouched.
    second = cmd_scan(Args(repo, two_commit_repo["base_sha"], two_commit_repo["head_sha"],
                           runs_root, tmp_path / "work-b"))
    assert second["status"] == "ready"
    assert second["scan_key"] == first["scan_key"]
    assert second["run_id"] != first["run_id"]
    assert run_dir_a.exists()
    snap_b = _deterministic_snapshot(Path(second["run_dir"]))
    assert snap_b == snap_a

    # Different detector config must not collide with the default arm.
    third = cmd_scan(Args(repo, two_commit_repo["base_sha"], two_commit_repo["head_sha"],
                          runs_root, tmp_path / "work-c", detectors="noop"))
    assert third["status"] == "ready"
    assert third["scan_key"] != first["scan_key"]

    # Unknown future knobs also diverge the scan key (no silent arm mixing).
    from codatiel_i2.core.runs import scan_key_for

    key_a = scan_key_for("r", "a" * 40, "b" * 40, {"detectors": []})
    key_b = scan_key_for("r", "a" * 40, "b" * 40, {"detectors": [], "temperature": 0.9})
    assert key_a != key_b


def test_immutable_run_dir_rejects_overwrite(tmp_path):
    from codatiel_i2.core import runs as runs_mod

    scan_key = runs_mod.scan_key_for("r", "a" * 40, "b" * 40, {})
    run_dir = tmp_path / "runs" / scan_key / "run1"
    kwargs = {
        "run_dir": run_dir, "scan_key": scan_key, "run_id": "run1",
        "repo": "r", "base_sha": "a" * 40, "head_sha": "b" * 40,
        "config": {}, "workspace": {}, "changes": [], "classification": {},
        "coverage": {}, "candidates": {}, "evidence": [], "verifications": [],
        "findings": [], "metrics": {},
    }
    runs_mod.write_run(**kwargs)
    import pytest

    with pytest.raises(FileExistsError):
        runs_mod.write_run(**kwargs)
    # scan_key binding: mismatched identity refused
    with pytest.raises(ValueError):
        runs_mod.write_run(**{**kwargs, "run_dir": tmp_path / "runs" / "WRONG" / "run1",
                                       "scan_key": "WRONG"})


def test_populated_ledger_determinism(two_commit_repo):
    """Real tool calls compare equal after clock-field normalization."""
    from codatiel_i2.core.calls import new_ledger
    from codatiel_i2.tools import read_file as rf

    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    ledgers = []
    for _ in range(2):
        ledger = new_ledger()
        rf.read_file(two_commit_repo["repo"], "src/app.py", base_sha=base, head_sha=head,
                     revision="head", ledger=ledger)
        for row in ledger:
            row.pop("at", None)
            row.pop("latency_ms", None)
        ledgers.append(canonical_json(ledger))
    assert ledgers[0] == ledgers[1]
    assert json.loads(ledgers[0])[0]["evidence_ids"] != []
