"""Archive-fallback workspace path (when `git worktree add` is unavailable)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from codatiel_i2.core import workspace as ws


def test_archive_fallback_materialises_and_reuses(two_commit_repo, tmp_path, monkeypatch):
    repo = two_commit_repo["repo"]
    base_sha = two_commit_repo["base_sha"]
    head_sha = two_commit_repo["head_sha"]
    work_root = tmp_path / "work"

    real_run = subprocess.run

    def fail_worktree_add(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "worktree" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "worktree unavailable")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_worktree_add)
    first = ws.prepare_local_workspace(repo, base_sha, head_sha, work_root)
    assert first.status == "ready", first.error
    assert (Path(first.head_dir) / ws.SHA_MARKER).read_text().strip() == head_sha
    assert "return 60" in (Path(first.head_dir) / "src" / "app.py").read_text()

    # rerun reuses the archive marker instead of rebuilding
    second = ws.prepare_local_workspace(repo, base_sha, head_sha, work_root)
    assert second.status == "ready"
    assert second.head_dir == first.head_dir
