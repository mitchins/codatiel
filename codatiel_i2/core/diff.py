"""Diff helpers: changed-file enumeration with +/- counts.

Uses NUL-separated git output (`-z`, `core.quotePath=false`) so TABs,
newlines, quotes and unicode paths parse deterministically. Rename
detection is pinned explicitly (`--find-renames=50%`) rather than relying
on machine-local gitconfig.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codatiel_i2.common.git import GitError

STATUS_MAP = {"A": "added", "M": "modified", "D": "deleted",
              "R": "renamed", "C": "copied", "T": "typechange", "U": "unmerged"}


def changed_files(repo: Path, base_sha: str, head_sha: str) -> list[dict]:
    """Return one row per changed file: path, status, additions, deletions, is_binary.

    Every changed file appears exactly once (100% accounting upstream).
    """
    name_status = _git_bytes(repo, "diff", "--find-renames=50%",
                             "--name-status", "-z", f"{base_sha}..{head_sha}")
    numstat = _git_bytes(repo, "diff", "--find-renames=50%",
                         "--numstat", "-z", f"{base_sha}..{head_sha}")
    additions: dict[str, int] = {}
    deletions: dict[str, int] = {}
    binary: dict[str, bool] = {}
    toks = numstat.split(b"\x00")
    i = 0
    while i < len(toks):
        head = toks[i].decode("utf-8", "replace")
        i += 1
        if not head.strip():
            continue
        parts = head.split("\t")
        if len(parts) != 3:
            continue
        add, dele, path = parts
        if path == "":
            # Rename/copy with -z: head path slot is empty, then
            # bare OLD NUL NEW NUL tokens follow. Counts belong to NEW.
            bare: list[str] = []
            while i < len(toks) and toks[i] and b"\t" not in toks[i] and len(bare) < 2:
                bare.append(toks[i].decode("utf-8", "replace"))
                i += 1
            if bare:
                path = bare[-1]
            else:
                continue
        is_bin = (add == "-" or dele == "-")
        binary[path] = is_bin
        additions[path] = int(add) if add != "-" else 0
        deletions[path] = int(dele) if dele != "-" else 0
    rows: list[dict] = []
    toks = name_status.split(b"\x00")
    i = 0
    while i < len(toks):
        status_tok = toks[i].decode("utf-8", "replace")
        i += 1
        if not status_tok.strip():
            continue
        status = status_tok.strip()[0]
        if status in ("R", "C"):
            # -z layout: "R100" NUL "old" NUL "new" NUL (score in status token)
            old = toks[i].decode("utf-8", "replace") if i < len(toks) else ""
            i += 1
            new = toks[i].decode("utf-8", "replace") if i < len(toks) else old
            i += 1
            path = new or old
        else:
            # layout: "M" NUL "path" NUL (score form "R100" handled above)
            if i >= len(toks):
                break
            path = toks[i].decode("utf-8", "replace")
            i += 1
        # name-status -z emits status and path as separate NUL tokens in
        # modern git ("M\\0path\\0"); handle legacy combined token too.
        if not path and status_tok.strip()[1:].strip():
            path = status_tok.strip()[1:].strip()
        if "\t" in path:
            path = path.split("\t")[-1].strip()
        rows.append({
            "path": path,
            "status": STATUS_MAP.get(status, "unknown"),
            "additions": additions.get(path, 0),
            "deletions": deletions.get(path, 0),
            "is_binary": binary.get(path, False),
        })
    seen: dict[str, dict] = {}
    for row in rows:
        if row["path"]:
            seen[row["path"]] = row
    return list(seen.values())


def _git_bytes(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false", "-c", "diff.renames=true", *args],
                          capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise GitError((proc.stderr.decode(errors="replace").strip() or "git diff failed")[:2000])
    return proc.stdout
