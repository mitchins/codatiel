"""GitHub tailer: invokes `codatiel-i2 scan` and treats output as opaque.

Keeps Iteration-1 concepts (discover PRs, unchanged-head-SHA skip, immutable
per-run dirs, state/event log, report-only, no mutation by default) without
lifting the old review orchestration. Knows nothing about old phase-based
review status or legacy pipelines.
"""

from __future__ import annotations

import json
import subprocess
import shutil
from pathlib import Path
from typing import Any

from codatiel_i2.common.hashing import short_id
from codatiel_i2.common.jsonio import read_json, write_json
from codatiel_i2.common.time import utc_now_iso

STATE_SCHEMA = "codatiel_i2.tailer_state.v1"


def default_state() -> dict:
    return {"schema_version": STATE_SCHEMA, "prs": {}}


def load_state(path: Path) -> dict:
    if Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            pass
    return default_state()


def discover_prs(repo: str, limit: int = 20) -> tuple[list[dict] | None, str | None]:
    if shutil.which("gh") is None:
        return None, "gh CLI is not available"
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", str(limit),
         "--json", "number,title,headRefOid,baseRefOid,headRefName,baseRefName,url"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip()[:1000]
    try:
        rows = json.loads(proc.stdout)
        return rows if isinstance(rows, list) else [], None
    except Exception as exc:
        return None, f"parse error: {exc}"


def scan_pr(repo: str, pr_number: int, runs_root: Path, scanner: str = "codatiel-i2",
            repo_root: str = "", base: str = "", head: str = "") -> dict[str, Any]:
    """Invoke the scanner CLI for one PR. Output artifacts are opaque to the tailer."""
    runs_root = Path(runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    cmd = [scanner, "scan", "--repo", repo, "--pr", str(pr_number),
           "--runs-root", str(runs_root)]
    if repo_root:
        cmd += ["--repo-root", repo_root]
    if base:
        cmd += ["--base", base]
    if head:
        cmd += ["--head", head]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        # No scanner executable on PATH (e.g. no installed entry point):
        # report the misconfiguration instead of crashing the tail run.
        return {"returncode": 127, "stderr": f"scanner executable not found: {exc}",
                "run_dir": "", "status": "scanner_missing"}
    except subprocess.TimeoutExpired as exc:
        return {"returncode": 124, "stderr": f"scanner timeout: {exc}",
                "run_dir": "", "status": "timeout"}
    except OSError as exc:
        return {"returncode": 125, "stderr": f"scanner failed to launch: {type(exc).__name__}: {exc}",
                "run_dir": "", "status": "scanner_error"}
    payload: Any = {}
    try:
        if proc.stdout.strip():
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {"returncode": proc.returncode, "stderr": proc.stderr[-2000:],
            "run_dir": payload.get("run_dir", ""), "status": payload.get("status", "unknown")}


def scan_succeeded(result: dict[str, Any]) -> bool:
    """A scan counts only on clean exit + ready status + produced run dir."""
    return (result.get("returncode") == 0 and result.get("status") == "ready"
            and bool(result.get("run_dir")))


def note_seen(state: dict, repo: str, pr_number: int, head_sha: str) -> tuple[bool, str]:
    """Record observation. Scan iff no *successful* scan covers this head SHA."""
    key = f"{repo}#{pr_number}"
    entry = (state.get("prs") or {}).get(key, {})
    entry["last_seen_head_sha"] = head_sha
    entry["last_seen"] = utc_now_iso()
    state.setdefault("prs", {})[key] = entry
    if entry.get("last_successful_scan_head_sha") == head_sha and head_sha:
        return False, "unchanged_head_sha"
    return True, "new_or_changed"


def note_success(state: dict, repo: str, pr_number: int, head_sha: str) -> None:
    """Advance only after the scan succeeded. Failed scans stay retryable."""
    key = f"{repo}#{pr_number}"
    entry = (state.get("prs") or {}).get(key, {})
    entry["last_successful_scan_head_sha"] = head_sha
    # Keep legacy head_sha in sync for old readers.
    entry["head_sha"] = head_sha
    state.setdefault("prs", {})[key] = entry
