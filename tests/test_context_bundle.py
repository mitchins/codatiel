"""CONTEXT-01: prepared change bundle (Feature A) + citable diff
evidence (Feature B). Deterministic, no network, no inference.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codatiel_i2 import context_bundle as CB
from codatiel_i2.core.evidence import validate_envelope


def _run(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True,
                          timeout=60).stdout


def _repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, timeout=60)
    (root / "app.py").write_text(
        "LIMIT = 100\n"
        "\n"
        "def handler(request):\n"
        '    """Serve."""\n'
        "    return LIMIT\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "def test_handler():\n    assert handler(None)\n")
    (root / "README.md").write_text("demo\n")
    _run("add", ".", cwd=root)
    _run("commit", "-qm", "base", cwd=root)
    base = _run("rev-parse", "HEAD", cwd=root).strip()
    (root / "app.py").write_text(
        "LIMIT = 100\n"
        "MAX_RETRIES = 3\n"
        "\n"
        "def handler(request):\n"
        '    """Serve."""\n'
        "    return LIMIT\n"
        "\n"
        "def new_handler(request):\n"
        "    return MAX_RETRIES\n")
    (root / "tests" / "test_app.py").write_text(
        "def test_handler():\n    assert handler(None)\n\n\n"
        "def test_new_handler():\n    assert new_handler(None)\n")
    (root / "README.md").write_text("demo changed\n")
    (root / "compose.yaml").write_text(
        "services:\n  app:\n    environment:\n      YT2WIKI_ASR_URL: x\n")
    _run("add", ".", cwd=root)
    _run("commit", "-qm", "head", cwd=root)
    head = _run("rev-parse", "HEAD", cwd=root).strip()
    return {"root": root, "base": base, "head": head}


def _checkouts(repo: dict, tmp_path: Path) -> tuple[Path, Path, Path]:
    """base/head worktrees sharing the repo objects (like workspace prep)."""
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    _run("worktree", "add", "--detach", str(base_dir), repo["base"],
         cwd=repo["root"])
    _run("worktree", "add", "--detach", str(head_dir), repo["head"],
         cwd=repo["root"])
    return base_dir, head_dir, repo["root"]


def _changes(repo: dict) -> list[dict]:
    out = _run("diff", "--find-renames=50%", "--numstat", "-z",
               f"{repo['base']}..{repo['head']}", cwd=repo["root"])
    changes = []
    for record in out.split("\0"):
        if not record.strip():
            continue
        adds, dels, path = record.split("\t")
        changes.append({"path": path,
                        "additions": int(adds) if adds.isdigit() else 0,
                        "deletions": int(dels) if dels.isdigit() else 0,
                        "is_binary": False})
    return changes


# ------------------------------------------------------------- Feature A

def test_bundle_deterministic_and_path_ordered(tmp_path):
    repo = _repo(tmp_path)
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path)
    from codatiel_i2.core.files import classify_changes
    changes = _changes(repo)
    cls = classify_changes(changes)
    kw = dict(diff_repo=diff_repo, base_sha=repo["base"],
              head_sha=repo["head"], changes=changes, classification=cls,
              base_dir=base_dir, head_dir=head_dir)
    b1 = CB.build_change_bundle(**kw)
    b2 = CB.build_change_bundle(**kw)
    assert b1.text == b2.text
    # Neutral path order: compose.yaml < README.md < app.py
    inv = b1.text.split("## Changed files (path order)\n")[1]
    inv = inv.split("\n##")[0]
    paths = [ln.split()[1] for ln in inv.splitlines() if ln.startswith("- ")]
    assert paths == sorted(paths)


def test_bundle_lists_tests_and_generated_without_diff_text(tmp_path):
    repo = _repo(tmp_path)
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path)
    from codatiel_i2.core.files import classify_changes
    changes = _changes(repo)
    cls = classify_changes(changes)
    bundle = CB.build_change_bundle(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, classification=cls, base_dir=base_dir,
        head_dir=head_dir)
    marker = bundle.text.split(
        "## Tests, generated and non-source changes (listed, not quoted)\n")[1]
    marker = marker.split("\n##")[0]
    assert any("test_app.py" in ln and "not shown" in ln
               for ln in marker.splitlines())
    # The test diff text itself never appears in the bundle.
    assert "test_new_handler" not in bundle.text


def test_bundle_symbols_routes_env_keys(tmp_path):
    repo = _repo(tmp_path)
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path)
    from codatiel_i2.core.files import classify_changes
    changes = _changes(repo)
    cls = classify_changes(changes)
    bundle = CB.build_change_bundle(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, classification=cls, base_dir=base_dir,
        head_dir=head_dir)
    assert "def new_handler" in bundle.text  # new symbol listed
    assert "YT2WIKI_ASR_URL" in bundle.text  # compose key captured
    assert "app.py: class " not in bundle.text  # no phantom classes


def test_bundle_cap_produces_complete_omission_list(tmp_path):
    repo = _repo(tmp_path)
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path)
    from codatiel_i2.core.files import classify_changes
    changes = _changes(repo)
    cls = classify_changes(changes)
    bundle = CB.build_change_bundle(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, classification=cls, base_dir=base_dir,
        head_dir=head_dir, max_chars=600)
    assert bundle.truncated is True
    assert "## Omissions" in bundle.text
    assert any("retrievable" in o.get("reason", "") or o.get("reason")
               for o in bundle.omitted_files)
    # The omission list is part of the text and names excluded files.
    assert "app.py" in bundle.text.split("## Omissions")[1]


# ------------------------------------------------------------- Feature B

def test_line_evidence_binds_added_to_head_removed_to_base(tmp_path):
    repo = _repo(tmp_path)
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path)
    changes = _changes(repo)
    evidence, skipped = CB.bundle_line_evidence(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, base_dir=base_dir, head_dir=head_dir)
    assert skipped == 0
    assert evidence
    by_rev = {}
    for ev in evidence:
        assert validate_envelope(ev) == []
        assert ev["base_sha"] == repo["base"] and ev["head_sha"] == repo["head"]
        by_rev.setdefault(ev["revision"], []).append(ev)
    # app.py: "-def handler..." removal? No - handler stays. LIMIT area
    # changed lines are additions; README.md has a removed line ("demo").
    assert by_rev.get("head"), "added lines must bind to head"
    added_paths = {ev["path"] for ev in by_rev["head"]}
    assert "app.py" in added_paths
    # Verify one added line's bytes against the head checkout.
    for ev in by_rev["head"]:
        if ev["path"] == "app.py":
            lines = (head_dir / "app.py").read_text().split("\n")
            actual = "\n".join(
                lines[ev["line_start"] - 1:ev["line_end"]])
            assert actual == ev["excerpt"]
            assert "MAX_RETRIES" in actual or "new_handler" in actual


def test_removed_lines_bind_to_base_and_replay(tmp_path):
    repo = _repo(tmp_path)
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path)
    changes = _changes(repo)
    evidence, _skipped = CB.bundle_line_evidence(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, base_dir=base_dir, head_dir=head_dir)
    removed = [e for e in evidence if e["revision"] == "base"]
    assert removed, "README 'demo' removal must mint a base evidence"
    for ev in removed:
        lines = (base_dir / ev["path"]).read_text().split("\n")
        actual = "\n".join(lines[ev["line_start"] - 1:ev["line_end"]])
        assert actual == ev["excerpt"]


def test_line_evidence_respects_per_file_cap(tmp_path):
    repo = _repo(tmp_path)
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path)
    changes = _changes(repo)
    evidence, skipped = CB.bundle_line_evidence(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, base_dir=base_dir, head_dir=head_dir,
        max_runs_per_file=1)
    assert skipped >= 1  # runs beyond the cap are counted, never minted


def test_bundle_cite_includes_evidence_ids(tmp_path):
    repo = _repo(tmp_path)
    base_dir, head_dir, diff_repo = _checkouts(repo, tmp_path)
    from codatiel_i2.core.files import classify_changes
    changes = _changes(repo)
    cls = classify_changes(changes)
    bundle = CB.build_change_bundle(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, classification=cls, base_dir=base_dir,
        head_dir=head_dir, with_evidence=True)
    assert bundle.evidence
    for ev in bundle.evidence:
        assert f"[{ev['evidence_id']}]" in bundle.text
    plain = CB.build_change_bundle(
        diff_repo=diff_repo, base_sha=repo["base"], head_sha=repo["head"],
        changes=changes, classification=cls, base_dir=base_dir,
        head_dir=head_dir, with_evidence=False)
    assert "ev_" not in plain.text
