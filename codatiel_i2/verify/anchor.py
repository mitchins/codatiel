"""Anchor validation: every finding anchor must resolve to exact source."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from codatiel_i2.common.paths import safe_join


def _normalise(p: str) -> str:
    return PurePosixPath(p.replace("\\", "/")).as_posix()


def validate_anchor(repo_dir: Path, anchor: dict, changed_paths: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    path = anchor.get("path", "")
    ls, le = anchor.get("line_start"), anchor.get("line_end")
    if not path:
        return ["anchor.path is required"]
    try:
        target = safe_join(Path(repo_dir), path)
    except ValueError:
        return [f"anchor escapes repo: {path!r}"]
    if not target.is_file():
        return [f"anchor file not found: {path!r}"]
    if not isinstance(ls, int) or not isinstance(le, int) or ls < 1 or le < ls:
        return [f"invalid anchor line range: {ls}-{le}"]
    try:
        total = len(target.read_text(encoding="utf-8", errors="strict").splitlines())
    except Exception:
        return [f"anchor file unreadable: {path!r}"]
    if ls > total:
        errors.append(f"anchor start line {ls} beyond EOF ({total} lines in {path})")
    if le > total:
        errors.append(f"anchor end line {le} beyond EOF ({total} lines in {path})")
    if changed_paths is not None and _normalise(path) not in {_normalise(p) for p in changed_paths}:
        errors.append(f"changed_anchor {path!r} is not in the changed set")
    return errors
