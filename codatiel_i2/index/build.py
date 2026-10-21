"""Index build: content-hash incremental contract.

SHA integrity (Phase A.5): the caller names the exact commit being indexed
and build_index verifies the checkout actually contains it BEFORE reading a
single file. Indexing a HEAD working tree under a BASE label is a hard
error, not a silent mislabel — otherwise future code leaks into the base
partition and every counterfactual comparison is compromised.

Contract (to be proven in Phase B):
- index same SHA twice -> same logical content
- change one file -> only impacted entities reindexed
- historical base SHA queryable
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from codatiel_i2.common.git import GitError, rev_parse
from codatiel_i2.common.hashing import sha256_text
from codatiel_i2.common.time import utc_now_iso
from codatiel_i2.core.workspace import SHA_MARKER


def verify_checkout_sha(checkout_dir: Path, expected_sha: str) -> str:
    """Return expected_sha iff the checkout provably contains it. Else raise."""
    expected_sha = expected_sha.strip().lower()
    checkout_dir = Path(checkout_dir)
    marker = checkout_dir / SHA_MARKER
    if marker.exists() and not (checkout_dir / ".git").exists():
        recorded = marker.read_text(encoding="utf-8").strip().lower()
        if recorded != expected_sha:
            raise GitError(f"checkout SHA mismatch: labelled {expected_sha[:8]} but holds {recorded[:8]}")
        return expected_sha
    try:
        actual = rev_parse(checkout_dir, "HEAD")
    except GitError as exc:
        raise GitError(f"cannot prove checkout SHA (need {expected_sha[:8]}): {exc}")
    if actual != expected_sha:
        raise GitError(
            f"checkout SHA mismatch: labelled {expected_sha[:8]} but holds {actual[:8]}. "
            "Index the materialised base/head checkout, not the working repo."
        )
    dirty = subprocess.run(["git", "-C", str(checkout_dir), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=60)
    if dirty.returncode == 0 and dirty.stdout.strip():
        raise GitError(f"checkout {expected_sha[:8]} has uncommitted changes; refusing to index dirty bytes")
    return actual


def build_index(checkout_dir: Path, repo_id: str, expected_sha: str, conn) -> dict:
    """Index the materialised checkout holding expected_sha. Verifies first."""
    verify_checkout_sha(Path(checkout_dir), expected_sha)
    commit_sha = expected_sha
    repo_dir = Path(checkout_dir)
    files = 0
    symbols = 0
    for path in sorted(repo_dir.rglob("*")):
        if path.is_symlink():
            continue
        if path.name == SHA_MARKER:
            continue
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            rel = str(path.relative_to(repo_dir))
        except ValueError:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except Exception:
            continue
        digest = sha256_text(text)
        lines = text.count("\n") + 1
        conn.execute(
            "INSERT OR REPLACE INTO file (repo_id, commit_sha, path, content_hash, language, line_count)"
            " VALUES (?,?,?,?,?,?)",
            (repo_id, commit_sha, rel, digest, "", lines),
        )
        files += 1
        if path.suffix == ".py":
            for ent, start, end, snippet in _py_symbols(text):
                # NOTE: symbol.text is a DB convenience field, not an evidence
                # envelope. Any future detector citing it must re-read the
                # exact lines through revision-bound repo tools.
                conn.execute(
                    "INSERT OR REPLACE INTO symbol (repo_id, commit_sha, path, entity_type, entity_id,"
                    " content_hash, line_start, line_end, language, text, metadata_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (repo_id, commit_sha, rel, ent["type"], ent["id"],
                     sha256_text(snippet), start, end, "python", snippet, "{}"),
                )
                symbols += 1
    conn.execute(
        "INSERT OR IGNORE INTO repository_version (repo_id, commit_sha, created_at) VALUES (?,?,?)",
        (repo_id, commit_sha, utc_now_iso()),
    )
    conn.commit()
    return {"repo_id": repo_id, "commit_sha": commit_sha, "files": files, "symbols": symbols}


def _py_symbols(text: str):
    try:
        tree = ast.parse(text)
    except Exception:
        return
    lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            snippet = "\n".join(lines[start - 1:end])[:4000]
            kind = "function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "class"
            yield {"type": kind, "id": node.name}, start, end, snippet
