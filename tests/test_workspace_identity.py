"""Acceptance: exact base/head identity, head checkout, base addressable, determinism."""

from __future__ import annotations

from pathlib import Path

from codatiel_i2.common.git import rev_parse
from codatiel_i2.core import workspace as ws


def test_workspace_identity_and_rerun_deterministic(two_commit_repo, tmp_path):
    repo = two_commit_repo["repo"]
    base_sha = two_commit_repo["base_sha"]
    head_sha = two_commit_repo["head_sha"]
    work_root = tmp_path / "work"

    first = ws.prepare_local_workspace(repo, base_sha, head_sha, work_root)
    assert first.status == "ready", first.error
    assert first.base_sha == base_sha
    assert first.head_sha == head_sha

    # head checkout prepared: HEAD matches, changed content present
    assert rev_parse(Path(first.head_dir), "HEAD") == head_sha
    assert (Path(first.head_dir) / "src" / "app.py").read_text().strip().endswith("return 60")

    # base source remains addressable with old value
    assert rev_parse(Path(first.base_dir), "HEAD") == base_sha
    base_app = (Path(first.base_dir) / "src" / "app.py").read_text()
    assert "return 30" in base_app

    # rerun is deterministic: same SHAs, same dirs
    second = ws.prepare_local_workspace(repo, base_sha, head_sha, work_root)
    assert second.status == "ready"
    assert (second.base_sha, second.head_sha) == (base_sha, head_sha)
    assert second.base_dir == first.base_dir and second.head_dir == first.head_dir
