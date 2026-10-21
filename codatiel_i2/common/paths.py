"""Path helpers (independent, no legacy dependencies)."""

from __future__ import annotations

from pathlib import Path


def safe_join(repo_root: Path, rel: str | Path) -> Path:
    """Join rel to repo_root, rejecting escapes. Returns resolved absolute path."""
    root = Path(repo_root).resolve()
    candidate = (root / Path(str(rel))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes repo root: {rel!r}")
    return candidate


def repo_relpath(repo_root: Path, path: Path) -> str:
    root = Path(repo_root).resolve()
    abs_path = Path(path).resolve()
    try:
        return str(abs_path.relative_to(root))
    except ValueError:
        raise ValueError(f"path is outside repo root: {path!r}")
