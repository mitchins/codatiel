"""Subprocess + git helpers (no review behaviour)."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    import os as _os

    env = dict(_os.environ)
    # A scanner must never let caller environment reformat git output:
    # GIT_EXTERNAL_DIFF can substitute arbitrary bytes into diffs. Repo-
    # locating variables (GIT_DIR etc.) would let a caller redirect which
    # object store `-C <repo>` actually reads. GIT_CEILING_DIRECTORIES is
    # deliberately kept: it only restricts upward discovery (safer).
    for var in ("GIT_EXTERNAL_DIFF", "GIT_DIR", "GIT_WORK_TREE",
                "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
                "GIT_NAMESPACE"):
        env.pop(var, None)
    env["GIT_PAGER"] = "cat"
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, timeout=timeout, env=env)


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    proc = run(["git", "-C", str(repo), *args], cwd=None, timeout=timeout)
    if proc.returncode != 0:
        raise GitError((proc.stderr.strip() or proc.stdout.strip() or "git command failed")[:2000])
    return proc.stdout.strip()


def rev_parse(repo: Path, rev: str) -> str:
    out = git(repo, "rev-parse", rev)
    sha = out.strip().splitlines()[0].strip()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha.lower()):
        raise GitError(f"not a valid SHA: {rev!r} -> {sha!r}")
    return sha.lower()


def is_git_repo(path: Path) -> bool:
    try:
        git(Path(path), "rev-parse", "--git-dir")
        return True
    except Exception:
        return False
