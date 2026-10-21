"""Frozen fixture-store behaviour (M7.1 §9.1, shareable synthetic part)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "eval"))

from m7_fixture_store import SHA_MARKER, _snapshot_manifest, verify_snapshot
from codatiel_i2.core.evidence import checkout_holds_sha


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                         text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture()
def synthetic_snapshot(tmp_path: Path) -> dict:
    tree = tmp_path / "snapshot"
    tree.mkdir()
    (tree / "a.txt").write_bytes(b"alpha\n")
    (tree / "nested").mkdir()
    (tree / "nested" / "b.txt").write_bytes(b"beta\n")
    (tree / SHA_MARKER).write_text("0" * 40 + "\n", encoding="utf-8")
    files = []
    for name in ("a.txt", "nested/b.txt"):
        data = (tree / name).read_bytes()
        files.append({"path": name, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "git_mode": "100644"})
    entry = _snapshot_manifest(
        {"key": "synthetic", "repo_identity": "synthetic", "role": "head",
         "claimed_sha": "0" * 40, "provenance": "synthetic test fixture"},
        {"files": files, "skipped": {"git": 0, "link": 0, "special": 0}})
    return {"tree": tree, "entry": entry}


def test_marker_and_manifest_pass(synthetic_snapshot):
    assert verify_snapshot(synthetic_snapshot["tree"], synthetic_snapshot["entry"]) == []


def test_disposable_copy_passes(synthetic_snapshot, tmp_path):
    copied = tmp_path / "disposable-copy"
    shutil.copytree(synthetic_snapshot["tree"], copied, symlinks=False)
    assert verify_snapshot(copied, synthetic_snapshot["entry"]) == []


def test_edited_file_without_manifest_update_fails(synthetic_snapshot):
    (synthetic_snapshot["tree"] / "a.txt").write_bytes(b"edited\n")
    errors = verify_snapshot(synthetic_snapshot["tree"], synthetic_snapshot["entry"])
    assert any("manifest" in error for error in errors)


def test_escaping_symlink_is_rejected(synthetic_snapshot, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    (synthetic_snapshot["tree"] / "escape").symlink_to(outside)
    errors = verify_snapshot(synthetic_snapshot["tree"], synthetic_snapshot["entry"])
    assert any("symlink" in error for error in errors)


def test_git_pointer_is_rejected(synthetic_snapshot):
    (synthetic_snapshot["tree"] / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    errors = verify_snapshot(synthetic_snapshot["tree"], synthetic_snapshot["entry"])
    assert any(".git" in error for error in errors)


def test_live_git_checkouts_still_use_git_sha_verification(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@test")
    _git(root, "config", "user.name", "fixture")
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    sha = _git(root, "rev-parse", "HEAD")
    assert checkout_holds_sha(root, sha) is True
    assert checkout_holds_sha(root, "1" * 40) is False
