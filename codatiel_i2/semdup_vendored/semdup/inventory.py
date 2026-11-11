"""Repository inventory: walk, exclude, hash, classify language.

Model-free. Exclusions are owner-controlled patterns only; no
benchmark-shaped shortcuts live here.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .snapshot import file_hash

DEFAULT_EXCLUDE = (
    ".git/**",
    "__pycache__/**",
    ".venv/**",
    ".hg/**",
    "node_modules/**",
    "vendor/**",
    "dist/**",
    "build/**",
)

EXT_LANG = {".py": "python", ".php": "php", ".swift": "swift"}


@dataclass(frozen=True)
class FileRecord:
    path: str  # POSIX rel
    sha256: str
    bytes: int
    language: str | None  # python/php/swift or None (non-target)


def _excluded(rel: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/**") + "/*") for p in patterns)


def inventory(
    root: Path, exclude: tuple[str, ...] = DEFAULT_EXCLUDE
) -> tuple[list[FileRecord], list[str]]:
    """Return (eligible target records, excluded rel paths)."""
    root = root.resolve()
    eligible: list[FileRecord] = []
    excluded: list[str] = []
    for dirpath, dirnames, filenames in __import__("os").walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in (".git", "__pycache__") and not (Path(dirpath) / d).is_symlink()
        )
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            if not full.is_file():
                continue
            rel = full.relative_to(root).as_posix()
            if _excluded(rel, exclude):
                excluded.append(rel)
                continue
            lang = EXT_LANG.get(full.suffix.lower())
            if lang is None:
                continue
            eligible.append(
                FileRecord(
                    path=rel,
                    sha256=file_hash(full),
                    bytes=full.stat().st_size,
                    language=lang,
                )
            )
    eligible.sort(key=lambda r: r.path)
    return eligible, sorted(excluded)
