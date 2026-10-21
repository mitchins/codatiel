"""Tailer retry: failed scans never mark a head SHA successfully processed."""

from __future__ import annotations

from codatiel_i2.github import tailer as tailer_mod


def test_failed_scan_stays_retryable():
    state = tailer_mod.default_state()
    should, _ = tailer_mod.note_seen(state, "o/r", 7, "a" * 40)
    assert should is True
    # scan fails: no success recorded
    should, _ = tailer_mod.note_seen(state, "o/r", 7, "a" * 40)
    assert should is True  # still retryable, not "unchanged_head_sha"
    tailer_mod.note_success(state, "o/r", 7, "a" * 40)
    should, reason = tailer_mod.note_seen(state, "o/r", 7, "a" * 40)
    assert (should, reason) == (False, "unchanged_head_sha")
    # new head re-arms
    should, _ = tailer_mod.note_seen(state, "o/r", 7, "b" * 40)
    assert should is True


def test_success_gate_requires_ready_status_and_run_dir():
    assert tailer_mod.scan_succeeded({"returncode": 0, "status": "ready", "run_dir": "runs/x"}) is True
    # exit 0 with failed/unknown status must NOT mark success
    assert tailer_mod.scan_succeeded({"returncode": 0, "status": "failed", "run_dir": "runs/x"}) is False
    assert tailer_mod.scan_succeeded({"returncode": 0, "status": "unknown", "run_dir": ""}) is False
    assert tailer_mod.scan_succeeded({"returncode": 1, "status": "ready", "run_dir": "runs/x"}) is False


def test_cmd_tail_zero_exit_failed_scan_stays_retryable(tmp_path, monkeypatch):
    from codatiel_i2.cli import cmd_tail

    prs = [{"number": 7, "headRefOid": "a" * 40}]
    monkeypatch.setattr(tailer_mod, "discover_prs", lambda repo, limit=20: (prs, None))
    calls = {"n": 0}

    def fake_scan(repo, pr_number, runs_root, **kwargs):
        calls["n"] += 1
        return {"returncode": 0, "stderr": "", "run_dir": "", "status": "failed"}

    monkeypatch.setattr(tailer_mod, "scan_pr", fake_scan)

    class TailArgs:
        repo = "o/r"
        runs_root = str(tmp_path / "runs")
        state = str(tmp_path / "state.json")
        limit = 20

    first = cmd_tail(TailArgs())
    assert first["status"] == "ok"
    second = cmd_tail(TailArgs())
    assert second["status"] == "ok"
    # second tail retried instead of skipping as unchanged
    assert calls["n"] == 2


def test_scan_pr_coerces_garbage_payload(tmp_path, monkeypatch):
    import subprocess

    class Proc:
        returncode = 0
        stdout = "[1,2]\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc())
    result = tailer_mod.scan_pr("o/r", 7, tmp_path / "runs")
    assert result["status"] == "unknown"
    assert result["run_dir"] == ""
    assert tailer_mod.scan_succeeded(result) is False


def test_docs_code_hinted_for_source_changes():
    from codatiel_i2.core.files import classify_file

    assert "docs_code" in classify_file("src/app.py", additions=1)["detectors"]
    assert "docs_code" in classify_file("schema/openapi.json")["detectors"]
    assert "docs_code" in classify_file("migrations/0001_x.py")["detectors"]


def test_ledgers_populate_mechanically(two_commit_repo):
    from codatiel_i2.core.calls import new_ledger
    from codatiel_i2.tools import read_file as rf

    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    ledger = new_ledger()
    out = rf.read_file(two_commit_repo["repo"], "src/app.py", base_sha=base, head_sha=head,
                       revision="head", ledger=ledger)
    assert len(ledger) == 1
    assert ledger[0]["tool"] == "read_file"
    assert ledger[0]["status"] == "ok"
    assert ledger[0]["evidence_ids"] == [out["evidence"]["evidence_id"]]
    # errors are recorded too, then re-raised
    import pytest

    with pytest.raises(FileNotFoundError):
        rf.read_file(two_commit_repo["repo"], "nope.py", base_sha=base, head_sha=head,
                     revision="head", ledger=ledger)
    assert len(ledger) == 2
    assert ledger[1]["status"] == "error"


def test_project_docs_are_revision_bound(two_commit_repo, tmp_path):
    """Project README/docs evidence is base/head source evidence, never external."""
    from pathlib import Path as _P

    from codatiel_i2.core import workspace as ws_mod
    from codatiel_i2.core.evidence import replay_source_evidence, validate_envelope
    from codatiel_i2.tools import read_file as rf

    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    work = ws_mod.prepare_local_workspace(two_commit_repo["repo"], base, head, tmp_path / "work")
    assert work.status == "ready"
    for revision, checkout in (("base", work.base_dir), ("head", work.head_dir)):
        out = rf.read_file(_P(checkout), "docs/configuration.md", base_sha=base, head_sha=head,
                           revision=revision)
        ev = out["evidence"]
        assert ev["kind"] == "source_excerpt", ev
        assert ev["revision"] == revision
        assert validate_envelope(ev) == []
        assert replay_source_evidence(ev, base_checkout=_P(work.base_dir),
                                      head_checkout=_P(work.head_dir)) == []
    # vendor lookup without a cache claims nothing and mints no evidence
    from codatiel_i2.tools.lookup_docs import lookup_docs

    empty = lookup_docs("timeout configuration", package="acme-docs")
    assert empty["status"] == "not_found"
    assert empty["evidence"] == []
    # with an immutable cache artifact, vendor claims bind to the artifact
    cache = tmp_path / "vendor_cache" / "acme-docs" / "2.1"
    cache.mkdir(parents=True)
    (cache / "config.md").write_text("# Config\n\ntimeout default is 30 seconds.\n")
    vendored = lookup_docs("timeout default", package="acme-docs", version="2.1",
                           vendor_cache_dir=tmp_path / "vendor_cache")
    assert vendored["status"] == "ok"
    assert len(vendored["evidence"]) == 1
    assert vendored["evidence"][0]["kind"] == "external_document"
    assert vendored["evidence"][0]["revision"] == "external"
    assert validate_envelope(vendored["evidence"][0]) == []


def test_lookup_docs_traversal_blocked(tmp_path):
    from codatiel_i2.tools.lookup_docs import lookup_docs

    cache = tmp_path / "cache"
    (cache / "real" / "1.0").mkdir(parents=True)
    (cache / "real" / "1.0" / "doc.md").write_text("timeout configuration notes\n")
    (tmp_path / "outside.md").write_text("secret timeout data\n")
    for package, version in (("../..", ""), ("real", "../.."), ("..", ""), ("real", "/tmp")):
        result = lookup_docs("timeout", package=package, version=version or None,
                             vendor_cache_dir=cache)
        assert result["status"] == "not_found", (package, version)
        assert result["evidence"] == []
        assert any("refusing unsafe" in w for w in result["warnings"])


def test_scan_pr_missing_scanner_is_explicit_failure(tmp_path):
    out = tailer_mod.scan_pr("o/r", 1, tmp_path, scanner="definitely-not-a-real-binary-xyz")
    assert out["status"] == "scanner_missing"
    assert out["run_dir"] == ""
