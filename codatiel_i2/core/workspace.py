"""PR workspace handling (lifted from Iteration 1, rewritten).

Provenance: tools/codatiel_pr_workspace.py (Iteration 1) — functionality
lifted and rewritten into a clean Iteration-2 module with no review-specific
behaviour and no legacy imports.

Improvements vs Iteration 1:
- generic repository names (no Kinglet/SmolRouter assumptions)
- explicit base worktree + head worktree support
- immutable base/head SHA identity (resolved via rev-parse, verified)
- local-repo mode for deterministic tests (no GitHub API required)
- GitHub PR mode preserved via `gh` where available
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from codatiel_i2.common.git import GitError, git, rev_parse
from codatiel_i2.common.hashing import short_id

SCHEMA_VERSION = "codatiel_i2.pr_workspace.v1"


@dataclass
class Workspace:
    schema_version: str
    repo: str
    base_sha: str
    head_sha: str
    base_ref: str | None
    head_ref: str | None
    base_dir: str
    head_dir: str
    status: str
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "repo": self.repo,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "base_dir": self.base_dir,
            "head_dir": self.head_dir,
            "status": self.status,
            "error": self.error,
        }


def repo_slug(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.scheme and parsed.netloc:
        slug = parsed.path.strip("/")
    else:
        slug = repo_url.strip().replace("git@", "").split(":", 1)[-1]
    if slug.endswith(".git"):
        slug = slug[:-4]
    parts = [p for p in slug.split("/") if p]
    if not parts:
        return "repo"
    if len(parts) >= 2:
        return f"{parts[-2]}__{parts[-1]}"
    return parts[-1]


def prepare_local_workspace(
    repo_root: Path,
    base: str,
    head: str,
    work_root: Path,
) -> Workspace:
    """Prepare deterministic base/head worktrees for a local git repo.

    - resolves exact base/head SHAs via rev-parse (immutable identity)
    - materialises head checkout + base checkout as separate dirs
    - rerunning with same SHAs is deterministic
    """
    repo_root = Path(repo_root).resolve()
    if not (repo_root / ".git").exists() and not _is_worktree_root(repo_root):
        # allow bare check: git rev-parse must succeed
        try:
            git(repo_root, "rev-parse", "--git-dir")
        except GitError as exc:
            return Workspace(SCHEMA_VERSION, str(repo_root), "", "", None, None, "", "", "failed", str(exc))

    try:
        base_sha = rev_parse(repo_root, base)
        head_sha = rev_parse(repo_root, head)
    except GitError as exc:
        return Workspace(SCHEMA_VERSION, str(repo_root), "", "", None, None, "", "", "failed", str(exc))

    slug = f"{repo_root.name}__{short_id(str(repo_root.resolve()))[:8]}"
    run_dir = Path(work_root) / f"{slug}__{base_sha[:8]}__{head_sha[:8]}"
    base_dir = run_dir / "base"
    head_dir = run_dir / "head"
    try:
        _materialise_checkout(repo_root, base_sha, base_dir)
        _materialise_checkout(repo_root, head_sha, head_dir)
    except (GitError, OSError) as exc:
        return Workspace(
            SCHEMA_VERSION, str(repo_root), base_sha, head_sha, None, None,
            str(base_dir), str(head_dir), "failed", str(exc),
        )
    # verify identity (git worktrees have .git; archive fallback leaves a sha marker)
    try:
        actual_head = _checkout_sha(head_dir, head_sha)
        actual_base = _checkout_sha(base_dir, base_sha)
    except GitError as exc:
        return Workspace(
            SCHEMA_VERSION, str(repo_root), base_sha, head_sha, None, None,
            str(base_dir), str(head_dir), "failed", f"checkout identity check failed: {exc}",
        )
    if actual_head != head_sha or actual_base != base_sha:
        return Workspace(
            SCHEMA_VERSION, str(repo_root), base_sha, head_sha, None, None,
            str(base_dir), str(head_dir), "failed",
            f"SHA mismatch: base {actual_base} != {base_sha} or head {actual_head} != {head_sha}",
        )
    return Workspace(
        SCHEMA_VERSION, str(repo_root), base_sha, head_sha, None, None,
        str(base_dir), str(head_dir), "ready", None,
    )


SHA_MARKER = ".codatiel_i2_sha"


def _checkout_sha(checkout_dir: Path, expected_sha: str) -> str:
    marker = Path(checkout_dir) / SHA_MARKER
    if marker.exists() and not (Path(checkout_dir) / ".git").exists():
        recorded = marker.read_text(encoding="utf-8").strip()
        if recorded != expected_sha:
            raise GitError(f"archive checkout SHA mismatch: {recorded} != {expected_sha}")
        return recorded
    return rev_parse(Path(checkout_dir), "HEAD")


def _is_worktree_root(path: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=30,
        )
        return out.returncode == 0
    except Exception:
        return False


def _materialise_checkout(repo_root: Path, sha: str, dest: Path) -> None:
    if dest.exists():
        marker = dest / SHA_MARKER
        if marker.exists() and not (dest / ".git").exists():
            # Archive checkouts are never reused: a pre-existing directory
            # with a matching marker could hold attacker-planted bytes, so
            # always rebuild from trusted git objects.
            shutil.rmtree(dest, ignore_errors=True)
        else:
            try:
                if rev_parse(dest, "HEAD") == sha:
                    # Git worktree: HEAD is authoritative; still refuse dirty trees.
                    dirty = subprocess.run(["git", "-C", str(dest), "status", "--porcelain"],
                                           capture_output=True, text=True, timeout=60)
                    if dirty.returncode == 0 and not dirty.stdout.strip():
                        return
            except GitError:
                pass
            # Drop git's worktree registration before removing the directory,
            # otherwise `worktree add` later fails with "already used".
            subprocess.run(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(dest)],
                           capture_output=True, timeout=60)
            subprocess.run(["git", "-C", str(repo_root), "worktree", "prune"],
                           capture_output=True, timeout=60)
            shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Use git worktree if possible for speed, else plain clone+checkout of object store.
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "add", "--detach", str(dest), sha],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode == 0:
        return
    # Fallback: git archive + extract (does not require worktree support).
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo_root), "archive", sha],
        capture_output=True, timeout=120,
    )
    if archive.returncode != 0:
        raise GitError(f"failed to materialise {sha}: {archive.stderr.decode()[:500]}")
    import tarfile
    import io

    # NOTE: Phase A runs one scan at a time per work_root (no lock yet).
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r") as tf:
        members = tf.getmembers()
        for member in members:
            name = member.name
            if name.startswith(("/", "\\")) or ".." in name.split("/"):
                raise GitError(f"unsafe archive member: {name!r}")
            if member.issym() or member.islnk():
                raise GitError(f"refusing symlink archive member: {name!r}")
        # Members pre-validated above; fully_trusted keeps 3.12 quiet.
        tf.extractall(path=str(dest), filter="fully_trusted")
    # record expected sha for identity (archive has no .git)
    (dest / SHA_MARKER).write_text(sha + "\n", encoding="utf-8")


def _looks_like_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value)


def fetch_github_pr_metadata(repo: str, pr: int) -> tuple[dict | None, str | None]:
    """Fetch base/head SHAs for a GitHub PR via `gh`. Returns (metadata, error)."""
    if shutil.which("gh") is None:
        return None, "gh CLI is not available"
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", repo, "--json",
             "baseRefName,baseRefOid,headRefName,headRefOid,state,title,url"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"PR metadata fetch failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "failed to fetch PR metadata"
        return None, err
    try:
        return json.loads(proc.stdout), None
    except Exception as exc:
        return None, f"failed to parse PR metadata: {exc}"


def prepare_github_workspace(repo: str, pr: int, work_root: Path, clone_url: str | None = None) -> Workspace:
    """GitHub-backed workspace prep (report-only; no review behaviour)."""
    metadata, error = fetch_github_pr_metadata(repo, pr)
    if metadata is None:
        status = "auth_required" if error and any(
            k in error.lower() for k in ("authentication", "permission", "auth")
        ) else "failed"
        return Workspace(SCHEMA_VERSION, repo, "", "", None, None, "", "", status, error)
    base_sha = str(metadata.get("baseRefOid") or "")
    head_sha = str(metadata.get("headRefOid") or "")
    base_ref = str(metadata.get("baseRefName") or "")
    head_ref = str(metadata.get("headRefName") or "")
    for label, sha in (("base", base_sha), ("head", head_sha)):
        if not _looks_like_sha(sha):
            return Workspace(SCHEMA_VERSION, repo, base_sha, head_sha, base_ref, head_ref,
                             "", "", "failed", f"GitHub returned invalid {label} SHA: {sha!r}")
    slug = repo.replace("/", "__")
    checkout_dir = Path(work_root) / slug / f"pr-{pr}"
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not (checkout_dir / ".git").exists():
            url = clone_url or f"https://github.com/{repo}.git"
            proc = subprocess.run(["git", "clone", url, str(checkout_dir)], capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                return Workspace(SCHEMA_VERSION, repo, base_sha, head_sha, base_ref, head_ref,
                                 "", str(checkout_dir), "failed", proc.stderr.strip()[:2000])
        for refspec in (f"pull/{pr}/head:refs/remotes/origin/pr-{pr}",):
            subprocess.run(["git", "-C", str(checkout_dir), "fetch", "origin", refspec],
                           capture_output=True, text=True, timeout=300)
        # Fetch both endpoints: a reused clone predates the base advance,
        # and materialising base_sha from stale objects would fail or,
        # worse, resolve the wrong history.
        for sha in (head_sha, base_sha):
            if sha:
                subprocess.run(["git", "-C", str(checkout_dir), "fetch", "origin", sha],
                               capture_output=True, text=True, timeout=300)
        target = head_sha or f"origin/pr-{pr}"
        proc = subprocess.run(["git", "-C", str(checkout_dir), "checkout", "--detach", target],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return Workspace(SCHEMA_VERSION, repo, base_sha, head_sha, base_ref, head_ref,
                             "", str(checkout_dir), "failed", proc.stderr.strip()[:2000])
    except (subprocess.SubprocessError, OSError) as exc:
        return Workspace(SCHEMA_VERSION, repo, base_sha, head_sha, base_ref, head_ref,
                         "", str(checkout_dir), "failed",
                         f"github checkout failed: {type(exc).__name__}: {exc}")
    try:
        actual = rev_parse(checkout_dir, "HEAD")
        if head_sha and actual != head_sha:
            return Workspace(SCHEMA_VERSION, repo, base_sha, head_sha, base_ref, head_ref,
                             "", str(checkout_dir), "failed",
                             f"checkout HEAD {actual[:8]} != PR head {head_sha[:8]}; refusing to scan the wrong revision")
        head_sha = actual
    except GitError as exc:
        return Workspace(SCHEMA_VERSION, repo, base_sha, head_sha, base_ref, head_ref,
                         "", str(checkout_dir), "failed", str(exc))
    base_dir = Path(work_root) / slug / f"pr-{pr}-base-{base_sha[:8]}"
    try:
        _materialise_checkout(checkout_dir, base_sha, base_dir)
        _checkout_sha(base_dir, base_sha)
    except (GitError, OSError) as exc:
        return Workspace(SCHEMA_VERSION, repo, base_sha, head_sha, base_ref, head_ref,
                         str(base_dir), str(checkout_dir), "failed", str(exc))
    return Workspace(SCHEMA_VERSION, repo, base_sha, head_sha, base_ref, head_ref,
                     str(base_dir), str(checkout_dir), "ready", None)
