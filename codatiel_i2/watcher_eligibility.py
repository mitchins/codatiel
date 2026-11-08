"""Codatiel v0.1.1 eligibility + validated GitHub connector.

Activation contract: automatic review eligibility requires the PR's
GitHub `createdAt` strictly later than the store's immutable
`activated_at`. Historical PRs are permanently excluded from automatic
review — new commits, reopening, metadata edits and restarts cannot
re-admit them (their createdAt never changes).

No field is ever defaulted to a safe value: missing/invalid identity,
lifecycle or SHA metadata rejects the PR before queue admission.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ValidatedPR:
    repo: str
    number: int
    url: str
    created_at: str
    state: str            # "OPEN" expected
    is_draft: bool
    merged_at: str        # "" when unmerged
    base_sha: str
    head_sha: str


def validate_pr_metadata(repo: str, raw: dict) -> tuple[ValidatedPR | None, str]:
    """Validate one raw PR object. Returns (validated, "") or (None, reason).

    Every required field must be present and well-formed; absent fields
    reject, never default.
    """
    try:
        number = int(raw.get("number") or 0)
    except (TypeError, ValueError):
        return None, "missing/invalid number"
    url = str(raw.get("url") or "")
    created = str(raw.get("createdAt") or "")
    state = str(raw.get("state") or "")
    merged = str(raw.get("mergedAt") or "")
    base = str(raw.get("baseRefOid") or "")
    head = str(raw.get("headRefOid") or "")
    is_draft = raw.get("isDraft")
    if not number:
        return None, "missing/invalid number"
    if not (url.startswith("https://github.com/") and f"/pull/{number}" in url):
        return None, f"invalid url {url!r}"
    if not created:
        return None, "missing createdAt"
    if state != "OPEN":
        return None, f"state {state!r} is not OPEN"
    if is_draft is not True and is_draft is not False:
        return None, "missing isDraft"
    if not FULL_SHA.match(base) or not FULL_SHA.match(head):
        return None, "missing/malformed base/head SHA"
    if not str(raw.get("baseRefName") or "") or not str(
            raw.get("headRefName") or ""):
        return None, "missing branch names"
    return ValidatedPR(repo=repo, number=number, url=url,
                       created_at=created, state=state,
                       is_draft=bool(is_draft), merged_at=merged,
                       base_sha=base, head_sha=head), ""


PR_FIELDS = ("number,title,headRefOid,baseRefOid,headRefName,baseRefName,"
             "url,createdAt,updatedAt,state,isDraft,mergedAt")


def discover_open_prs(repo: str, limit: int = 100) -> tuple[list[dict] | None, str | None]:
    """Read-only open-PR discovery. Failures fail closed.

    Installed gh has no per-page flag for `pr list`, so one bounded
    request (default 100) is used. Hitting the bound exactly is reported
    as a warning (possible truncation) rather than silently accepted;
    the rows are still processed. A non-empty warning string accompanies
    possibly-truncated results.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--limit", str(limit), "--json", PR_FIELDS],
            capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, proc.stderr.strip()[:1000]
    try:
        rows = json_loads(proc.stdout)
    except Exception as exc:
        return None, f"parse error: {exc}"
    if not isinstance(rows, list):
        return None, "unexpected response shape"
    if len(rows) >= limit:
        return rows, f"warning: {repo} hit the {limit}-PR inventory bound"
    return rows, ""


def json_loads(text: str) -> Any:
    import json
    return json.loads(text)


def fetch_pr(repo: str, number: int) -> tuple[dict | None, str | None]:
    """Read-only single-PR fetch for revalidation. Failures fail closed."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", repo,
             "--json", PR_FIELDS],
            capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, proc.stderr.strip()[:1000]
    try:
        row = json_loads(proc.stdout)
    except Exception as exc:
        return None, f"parse error: {exc}"
    return (row if isinstance(row, dict) else None,
            "" if isinstance(row, dict) else "unexpected response shape")


def _parse_ts(value: str) -> Any:
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def check_eligible(pr: ValidatedPR, activated_at: str) -> tuple[bool, str]:
    """Activation + lifecycle predicate. First sighting is never sufficient.

    Timestamps compare as parsed instants (fractional-second safe), not
    raw strings; unparseable values fail closed.
    """
    created = _parse_ts(pr.created_at)
    activated = _parse_ts(activated_at)
    if created is None or activated is None:
        return False, "unparseable timestamps"
    if not (created > activated):
        return False, "historical (createdAt <= activated_at)"
    if pr.is_draft:
        return False, "draft"
    if pr.merged_at:
        return False, "merged"
    return True, ""
