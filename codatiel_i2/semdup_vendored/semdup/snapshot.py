"""Snapshot identity: deterministic digest over an analysis directory.

Implements the `semdup-fixture-tree-v1` adapter contract (M0 §3):
sorted path/sha256/bytes manifest, canonical JSON, SHA-256 hex.
Regular files only; `.git` and `__pycache__` ignored; symlinks rejected.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

ALGORITHM = "semdup-fixture-tree-v1"
IGNORE_DIRS = frozenset({".git", "__pycache__"})


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files_in(root: Path) -> list[Path]:
    """Enumerate regular files; reject symlinks rather than following them."""
    result: list[Path] = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        for name in list(dirs) + sorted(names):
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise ValueError(f"Symlink not permitted in analysis tree: {candidate}")
        for name in sorted(names):
            candidate = Path(directory) / name
            if candidate.is_file():
                result.append(candidate)
    return sorted(result, key=lambda p: p.relative_to(root).as_posix())


def tree_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": p.relative_to(root).as_posix(),
            "sha256": file_hash(p),
            "bytes": p.stat().st_size,
        }
        for p in files_in(root)
    ]


def tree_hash(root: Path) -> str:
    return digest({"algorithm": ALGORITHM, "files": tree_manifest(root)})


def verify_tree(root: Path, expected: str) -> bool:
    return tree_hash(root) == expected
