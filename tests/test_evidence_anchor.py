"""Acceptance: safe source anchoring (no escapes, valid ranges, digest integrity)."""

from __future__ import annotations

import pytest

from pathlib import Path

from codatiel_i2.common.hashing import sha256_text
from codatiel_i2.core import evidence as ev_mod
from codatiel_i2.tools import read_file as rf
from codatiel_i2.verify import anchor as anchor_mod


def test_read_file_anchor_and_escape_rejection(two_commit_repo, tmp_path):
    repo = two_commit_repo["repo"]
    base = two_commit_repo["base_sha"]
    head = two_commit_repo["head_sha"]

    out = rf.read_file(repo, "src/app.py", base_sha=base, head_sha=head, start_line=1, end_line=2)
    env = out["evidence"]
    assert env["path"] == "src/app.py"
    assert (env["line_start"], env["line_end"]) == (1, 2)
    assert env["content_digest"] == sha256_text(env["excerpt"])
    assert ev_mod.validate_envelope(env) == []

    with pytest.raises(ValueError):
        rf.read_file(repo, "../outside.py", base_sha=base, head_sha=head)
    with pytest.raises(ValueError):
        rf.read_file(repo, "src/app.py", base_sha=base, head_sha=head, start_line=0)
    with pytest.raises((FileNotFoundError, ValueError)):
        rf.read_file(repo, "does/not_exist.py", base_sha=base, head_sha=head)

    # empty files produce file_state facts, not empty excerpts
    empty = rf.read_file(repo, "docs/empty.md", base_sha=base, head_sha=head)
    assert empty["total_lines"] == 0
    assert empty["evidence"]["kind"] == "file_state"
    assert empty["evidence"]["fact"] == "empty"
    assert ev_mod.validate_envelope(empty["evidence"]) == []


def test_base_and_head_reads_have_distinct_provenance(two_commit_repo, tmp_path):
    """Unchanged bytes, distinct ledger entries: revision is part of identity."""
    from codatiel_i2.core import workspace as ws_mod

    repo = two_commit_repo["repo"]
    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    work = ws_mod.prepare_local_workspace(repo, base, head, tmp_path / "work")
    assert work.status == "ready"
    from pathlib import Path as _P

    base_out = rf.read_file(_P(work.base_dir), "src/util.py", base_sha=base, head_sha=head, revision="base")
    head_out = rf.read_file(_P(work.head_dir), "src/util.py", base_sha=base, head_sha=head, revision="head")
    assert base_out["evidence"]["excerpt"] == head_out["evidence"]["excerpt"]
    assert base_out["evidence"]["evidence_id"] != head_out["evidence"]["evidence_id"]
    assert base_out["evidence"]["revision_sha"] == base
    assert head_out["evidence"]["revision_sha"] == head
    # both replay cleanly against their own revision
    assert ev_mod.replay_source_evidence(base_out["evidence"], base_checkout=_P(work.base_dir)) == []
    assert ev_mod.replay_source_evidence(head_out["evidence"], head_checkout=_P(work.head_dir)) == []
    # cross-revision replay fails (excerpt belongs to the other SHA lineage)
    base_ev_at_head = dict(head_out["evidence"])
    base_ev_at_head["revision"] = "base"
    base_ev_at_head["revision_sha"] = base
    assert ev_mod.replay_source_evidence(base_ev_at_head, base_checkout=_P(work.head_dir)) != []


def test_truncated_read_file_evidence_replays(tmp_path):
    """A ~100KB read truncates on line boundaries and still replays byte-for-byte."""
    from codatiel_i2.core import workspace as ws_mod

    repo = tmp_path / "bigrepo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@l"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    big = repo / "big.txt"
    big.write_text("".join(f"line {i:05d} padding text here\n" for i in range(4000)))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
    (repo / "other.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "head"], check=True, capture_output=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD~1"],
                          check=True, capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    work = ws_mod.prepare_local_workspace(repo, base, head, tmp_path / "work")
    assert work.status == "ready"
    out = rf.read_file(Path(work.head_dir), "big.txt", base_sha=base, head_sha=head, revision="head")
    assert out["total_lines"] == 4000
    assert out["truncated"] is True
    env = out["evidence"]
    # line_end names the last fully-included line (never a mid-line prefix)
    assert env["line_end"] < 4000
    assert env["excerpt"].splitlines()[-1].startswith("line ")
    assert ev_mod.validate_envelope(env) == []
    assert ev_mod.replay_source_evidence(env, head_checkout=Path(work.head_dir)) == []


def test_long_search_match_evidence_replays(tmp_path):
    """A 3K-character line: evidence carries the full line and replays."""
    from codatiel_i2.core import workspace as ws_mod
    from codatiel_i2.tools import search as search_mod

    repo = tmp_path / "longrepo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@l"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    long_line = "needle " + "x" * 3000 + " needle tail"
    (repo / "docs" / "long.md").parent.mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "long.md").write_text(long_line + "\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
    (repo / "other.txt").write_text("y\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "head"], check=True, capture_output=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD~1"],
                          check=True, capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    work = ws_mod.prepare_local_workspace(repo, base, head, tmp_path / "work")
    assert work.status == "ready"
    out = search_mod.search(Path(work.head_dir), "needle", base_sha=base, head_sha=head,
                            revision="head", regex=False)
    assert out["results"], "expected a hit on the long line"
    assert len(out["results"][0]["match"]) > 2000
    for env in out["evidence"]:
        assert ev_mod.validate_envelope(env) == []
        assert ev_mod.replay_source_evidence(env, head_checkout=Path(work.head_dir)) == []


def test_search_blank_regex_never_crashes(two_commit_repo):
    """Blank-line regex hits carry no excerpt: skipped, not minted, both backends."""
    from codatiel_i2.tools import search as search_mod

    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    for force_fallback in (False, True):
        if force_fallback:
            orig = search_mod._has_rg
            search_mod._has_rg = lambda: False
        try:
            out = search_mod.search(two_commit_repo["repo"], "^$", base_sha=base, head_sha=head,
                                    revision="head")
        finally:
            if force_fallback:
                search_mod._has_rg = orig
        for env in out["evidence"]:
            assert ev_mod.validate_envelope(env) == []
            assert env["excerpt"].strip() != ""


def test_search_phantom_context_path_rejected(tmp_path):
    """A colon-bearing context body must not mint a phantom path."""
    from codatiel_i2.tools.search import _parse_rg_output

    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    rows = _parse_rg_output(root, "./a.py-2-foo:123:bar\n", 50)
    assert rows == []
    rows = _parse_rg_output(root, "./a.py:1:x = 1\n", 50)
    assert len(rows) == 1 and rows[0]["path"] == "a.py"


def test_indented_caller_evidence_replays(tmp_path):
    """Indented call sites mint full lines that replay."""
    from codatiel_i2.core import workspace as ws_mod
    from codatiel_i2.tools.symbols import find_callers

    repo = tmp_path / "callrepo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@l"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    (repo / "a.py").write_text("def foo():\n    return 1\n\n\nclass B:\n    def run(self):\n        foo()\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
    (repo / "b.py").write_text("y = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "head"], check=True, capture_output=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD~1"],
                          check=True, capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    work = ws_mod.prepare_local_workspace(repo, base, head, tmp_path / "work")
    assert work.status == "ready"
    out = find_callers(Path(work.head_dir), "foo", base_sha=base, head_sha=head, revision="head")
    indented = [r for r in out["callers"] if r["line"] == 7]
    assert indented and indented[0]["match"] == "        foo()"
    for env in out["evidence"]:
        assert ev_mod.validate_envelope(env) == []
        assert ev_mod.replay_source_evidence(env, head_checkout=Path(work.head_dir)) == []


def test_anchor_validation_against_changed_set(two_commit_repo):
    from codatiel_i2.core import diff as diff_mod

    repo = two_commit_repo["repo"]
    changes = diff_mod.changed_files(repo, two_commit_repo["base_sha"], two_commit_repo["head_sha"])
    changed = {r["path"] for r in changes}
    good = {"path": "src/app.py", "line_start": 1, "line_end": 2}
    assert anchor_mod.validate_anchor(repo, good, changed) == []
    bad_range = {"path": "src/app.py", "line_start": 9999, "line_end": 9999}
    assert anchor_mod.validate_anchor(repo, bad_range, changed) != []
    outside = {"path": "src/util.py", "line_start": 1, "line_end": 1}
    # util.py unchanged in fixture -> flagged when changed-set is enforced
    assert anchor_mod.validate_anchor(repo, outside, changed) != []
    escape = {"path": "../evil.py", "line_start": 1, "line_end": 1}
    assert anchor_mod.validate_anchor(repo, escape, changed) != []
