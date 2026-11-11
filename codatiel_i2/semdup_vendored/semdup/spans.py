"""Source-span validation against a frozen snapshot root.

Spans carry POSIX relative paths, 1-based inclusive lines, and the SHA-256
of the complete original file bytes. Symbol names are decoration only.
Stdlib only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .snapshot import file_hash


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Expected a nonempty POSIX relative path")
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in (".", "..") for part in candidate.parts):
        raise ValueError(f"Unsafe path: {relative}")
    if candidate.as_posix() != relative:
        raise ValueError(f"Non-canonical path: {relative}")
    cur = root
    for part in candidate.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"Symlink path: {relative}")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Path escaped repository")
    return resolved


def validate_span(span: dict[str, Any], root: Path) -> list[str]:
    required = ("path", "start_line", "end_line", "source_sha256")
    if not isinstance(span, dict) or any(k not in span for k in required):
        return ["Missing span fields"]
    try:
        path = safe_path(root, span["path"])
    except (ValueError, OSError, TypeError) as exc:
        return [str(exc)]
    try:
        if not path.is_file():
            return [f'Missing source: {span["path"]}']
        if file_hash(path) != span["source_sha256"]:
            return [f'Stale source hash: {span["path"]}']
        start, end = span["start_line"], span["end_line"]
        count = len(path.read_text(encoding="utf-8").splitlines())
        if (
            type(start) is not int
            or type(end) is not int
            or not (1 <= start <= end <= count)
        ):
            return [f'Invalid source range: {span["path"]}:{start}-{end}']
        return []
    except (OSError, UnicodeError, TypeError) as exc:
        return [str(exc)]
