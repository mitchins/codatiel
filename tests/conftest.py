"""Shared fixture: deterministic two-commit git repo spanning all 12 categories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def two_commit_repo(tmp_path: Path):
    repo = tmp_path / "sample"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@local")
    _git(repo, "config", "user.name", "test")
    base_files = {
        "src/app.py": "def timeout():\n    return 30\n",
        "src/util.py": "def helper():\n    return 1\n",
        "tests/test_app.py": "def test_timeout():\n    assert True\n",
        "config.yaml": "timeout: 30\n",
        "docs/configuration.md": "# Config\n\ntimeout default is 30.\n",
        "schema/openapi.json": '{"openapi": "3.0.0"}\n',
        "migrations/0001_init.py": "def migrate():\n    pass\n",
        "snap.txt": "snapshot v1\n",
    }
    for rel, content in base_files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    head_files = {
        "src/app.py": "def timeout():\n    return 60\n",
        "src/new_feature.py": "def new_fn():\n    return 2\n",
        "docs/CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- bump timeout\n",
        "yarn.lock": "lockfile content\n",
        "Dockerfile": "FROM python:3.12\n",
        "docs/empty.md": "",
    }
    for rel, content in head_files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (repo / "assets" / "logo.bin").parent.mkdir(parents=True, exist_ok=True)
    (repo / "assets" / "logo.bin").write_bytes(bytes(range(256)) * 16)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "head: bump timeout 30->60, add feature")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "base_sha": base_sha, "head_sha": head_sha}
