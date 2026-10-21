"""Acceptance: 100% changed-file accounting across the 12-category taxonomy."""

from __future__ import annotations

from codatiel_i2.core import diff as diff_mod
from codatiel_i2.core import files as files_mod


def test_changed_file_accounting_complete(two_commit_repo):
    repo = two_commit_repo["repo"]
    changes = diff_mod.changed_files(repo, two_commit_repo["base_sha"], two_commit_repo["head_sha"])
    assert len(changes) >= 3
    classification = files_mod.classify_changes(changes)
    assert classification["accounting_complete"] is True
    assert classification["total_changed"] == classification["classified"] == len(changes)
    # every diff path appears exactly once in classification
    diff_paths = sorted(r["path"] for r in changes)
    classified_paths = sorted(f["path"] for f in classification["files"])
    assert classified_paths == diff_paths
    # taxonomy closed: only known categories
    assert set(f["category"] for f in classification["files"]) <= set(files_mod.CATEGORIES)
    # spot checks on expanded taxonomy
    by_path = {f["path"]: f for f in classification["files"]}
    assert by_path["src/app.py"]["category"] == "source"
    assert by_path["yarn.lock"]["category"] == "lockfile"
    assert by_path["docs/CHANGELOG.md"]["category"] == "docs"
    assert by_path["Dockerfile"]["category"] == "config"
    assert by_path["assets/logo.bin"]["category"] == "binary"
    assert by_path["docs/empty.md"]["category"] == "docs"
    # enrichment present
    for f in classification["files"]:
        assert isinstance(f["estimated_tokens"], int)
        assert isinstance(f["language"], str)
        assert isinstance(f["detectors"], list)
