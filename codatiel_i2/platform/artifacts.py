"""Crash-consistent artifact staging: stage, fsync, atomic rename.

A completed review bundle is written to a staging directory, every file
fsynced, a manifest (name -> sha256) written and fsynced, then
atomically renamed onto the same filesystem as an immutable run
directory; the parent directory entries are fsynced before the
database reference commits. Atomic rename alone is not power-loss
durability; the fsync chain is the guarantee.

Startup recovery validates manifests: a fully staged bundle with no
database reference is attached through the normal ownership/currentness
gates or quarantined; an incomplete bundle is quarantined, never
trusted. Orphaned prepared artifacts are diagnostics, never permission
to rerun paid inference.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

MANIFEST_NAME = "manifest.json"
STAGING_SUFFIX = ".staging"


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_name(name: str, what: str) -> str:
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or "\x00" in name):
        raise ValueError(f"bad {what} {name!r}")
    if (os.sep in name or (os.altsep and os.altsep in name)
            or name != os.path.basename(name)):
        raise ValueError(f"bad {what} {name!r}")
    return name


def stage(staging_root: Path, run_id: str, files: dict[str, bytes]) -> Path:
    """Write bundle files + manifest into a staging directory. Fs synced."""
    _safe_name(run_id, "run id")
    staging = Path(staging_root) / f"{run_id}{STAGING_SUFFIX}"
    if staging.exists():
        raise RuntimeError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True)
    manifest: dict[str, str] = {}
    for name, content in files.items():
        _safe_name(name, "artifact name")
        if name == MANIFEST_NAME:
            raise ValueError(f"reserved artifact name {name!r}")
        target = staging / name
        target.write_bytes(content)
        _fsync_file(target)
        manifest[name] = hashlib.sha256(content).hexdigest()
    blob = json.dumps({"run_id": run_id, "files": manifest},
                      sort_keys=True).encode()
    (staging / MANIFEST_NAME).write_bytes(blob)
    _fsync_file(staging / MANIFEST_NAME)
    _fsync_dir(staging)
    _fsync_dir(staging.parent)
    return staging


def commit(staging: Path, runs_root: Path) -> Path:
    """Atomically publish a staged bundle as an immutable run directory."""
    if not staging.name.endswith(STAGING_SUFFIX):
        raise ValueError(f"not a staging directory: {staging}")
    if not validate(staging):
        raise RuntimeError(f"staging bundle invalid: {staging}")
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        runs_root.mkdir(parents=True, exist_ok=True)
        _fsync_dir(runs_root.parent)
    dest = runs_root / staging.name[: -len(STAGING_SUFFIX)]
    if dest.exists():
        raise RuntimeError(f"run directory already exists: {dest}")
    os.rename(staging, dest)
    _fsync_dir(dest)
    _fsync_dir(Path(runs_root))
    return dest


def validate(run_dir: Path) -> bool:
    """True when every manifest entry exists with the recorded hash.

    A pure predicate: never raises. Malformed manifests (not JSON, not
    an object, files not a string->string map) are invalid, never
    trusted — and never abort validation of other directories.
    """
    try:
        manifest = json.loads((Path(run_dir) / MANIFEST_NAME).read_text(
            encoding="utf-8"))
        if not isinstance(manifest, dict):
            return False
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            return False
        for name, want in files.items():
            if not isinstance(name, str) or not isinstance(want, str):
                return False
            _safe_name(name, "manifest entry")
            if name == MANIFEST_NAME:
                return False
            data = (Path(run_dir) / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != want:
                return False
        return True
    except (OSError, ValueError, AttributeError, TypeError,
            RecursionError):
        return False


def manifest_sha(run_dir: Path) -> str:
    """Content hash binding a run directory to its database reference.

    Refuses anything validate() rejects: a hash over an unvalidatable
    bundle would bind the ledger to evidence that can never seal.
    """
    if not validate(run_dir):
        raise RuntimeError(f"run directory invalid: {run_dir}")
    try:
        blob = (Path(run_dir) / MANIFEST_NAME).read_bytes()
    except (OSError, ValueError, RecursionError) as exc:
        raise RuntimeError(f"run has no readable manifest: {run_dir}: "
                           f"{exc}")
    return hashlib.sha256(blob).hexdigest()


def recover_runs(staging_root: Path, runs_root: Path) -> dict[str, list[str]]:
    """Classify leftover staging/run directories after a restart.

    Returns {"complete_staging": [...], "quarantine": [...]}. Complete
    staging bundles are evidence awaiting a database reference (attach
    through the normal gates, or quarantine explicitly); everything
    incomplete or manifest-invalid is quarantine, never trusted.
    """
    out: dict[str, list[str]] = {"complete_staging": [], "quarantine": []}
    root = Path(staging_root)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name.endswith(STAGING_SUFFIX):
                if validate(child):
                    out["complete_staging"].append(child.name)
                else:
                    out["quarantine"].append(child.name)
    runs = Path(runs_root)
    if runs.is_dir():
        for child in sorted(runs.iterdir()):
            if child.is_dir() and not validate(child):
                out["quarantine"].append(str(child))
    return out
