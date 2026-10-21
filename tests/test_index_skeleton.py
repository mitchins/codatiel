"""Index SHA integrity: the index can never label HEAD bytes with a BASE SHA."""

from __future__ import annotations

import subprocess

import pytest

from codatiel_i2.common.git import GitError
from codatiel_i2.index.build import build_index, verify_checkout_sha
from codatiel_i2.index.exact import lookup_file, symbol_text
from codatiel_i2.index.overlay import overlay_status
from codatiel_i2.index.store import open_index
from codatiel_i2.core import workspace as ws


def test_index_same_sha_twice_same_content(two_commit_repo, tmp_path):
    conn = open_index(tmp_path / "index.sqlite")
    first = build_index(two_commit_repo["repo"], "sample", two_commit_repo["head_sha"], conn)
    second = build_index(two_commit_repo["repo"], "sample", two_commit_repo["head_sha"], conn)
    assert first == second
    row = lookup_file(conn, "sample", two_commit_repo["head_sha"], "src/app.py")
    assert row and row["path"] == "src/app.py"
    conn.close()


def test_index_refuses_mislabelled_checkout(two_commit_repo, tmp_path):
    """Working repo sits at HEAD: indexing it under BASE must fail loudly."""
    repo = two_commit_repo["repo"]
    base_sha = two_commit_repo["base_sha"]
    conn = open_index(tmp_path / "index.sqlite")
    with pytest.raises(GitError):
        build_index(repo, "sample", base_sha, conn)
    conn.close()


def test_base_and_head_partitions_stay_separate(two_commit_repo, tmp_path):
    """The user's A5.1 regression: base query returns base bytes, always."""
    repo = two_commit_repo["repo"]
    base_sha = two_commit_repo["base_sha"]
    head_sha = two_commit_repo["head_sha"]
    # Working repo is at HEAD throughout.
    work = ws.prepare_local_workspace(repo, base_sha, head_sha, tmp_path / "work")
    assert work.status == "ready"
    conn = open_index(tmp_path / "index.sqlite")
    build_index(work.base_dir, "sample", base_sha, conn)
    assert "return 30" in (symbol_text(conn, "sample", base_sha, "timeout") or "")
    assert "return 60" not in (symbol_text(conn, "sample", base_sha, "timeout") or "")
    build_index(work.head_dir, "sample", head_sha, conn)
    assert "return 60" in (symbol_text(conn, "sample", head_sha, "timeout") or "")
    # Base partition is uncontaminated after indexing head.
    assert "return 30" in (symbol_text(conn, "sample", base_sha, "timeout") or "")
    assert "return 60" not in (symbol_text(conn, "sample", base_sha, "timeout") or "")
    conn.close()


def test_verify_checkout_sha_rejects_wrong_sha(two_commit_repo):
    with pytest.raises(GitError):
        verify_checkout_sha(two_commit_repo["repo"], two_commit_repo["base_sha"])


def test_overlay_contract():
    status = overlay_status("a" * 40, "b" * 40, ["src/app.py"])
    assert status["base_sha"] == "a" * 40
    assert status["strategy"] == "base_index_plus_head_overlay"


def test_nested_scopes_do_not_overwrite_each_other(tmp_path):
    """Same-named methods in different classes (and nested defs under
    control flow) must not collide on INSERT OR REPLACE."""
    from codatiel_i2.index import build as build_mod

    text = ("class A:\n    def run(self):\n        return 1\n"
            "class B:\n    def run(self):\n        return 2\n"
            "if True:\n    def guarded(self):\n        return 3\n")
    got = {}
    for ent, start, end, snippet in build_mod._py_symbols(text):
        got.setdefault(ent["id"], []).append((start, end))
    assert got["A.run"] == [(2, 3)]
    assert got["B.run"] == [(5, 6)]
    assert got["guarded"] == [(8, 9)]
