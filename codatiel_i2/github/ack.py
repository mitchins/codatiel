"""Early GitHub acknowledgement (CODATIEL-ACK-01, Track B fallback).

Check Runs are unavailable to the operator credential (the Checks API
requires a GitHub App installation token; the operator token gets
``403 You must authenticate via a GitHub App``), and no new App
permissions may be requested. This module is the approved fallback:
**one idempotent, editable PR comment per PR** carrying lightweight,
revision-specific review visibility.

Lifecycle (``state``):

- ``queued``     — head discovered; waiting for the review worker.
- ``deferred``  — held with a reason (draft, budget ceiling); no
  review has run on this head.
- ``in_progress``— a review is claimed and running on the exact pinned
  SHA. Emitted after the atomic claim, before paid inference.
- ``completed``  — review finished and sealed; links the published
  findings comment where one exists. The word "reviewed" appears only
  here, never before inference and completion.
- ``superseded`` — the head became obsolete (stale pre-claim or
  mid-flight); partial spend is stated, never hidden.
- ``failed``     — the review failed; the checkpoint was not advanced.

Guarantees (mirroring ``github.comments`` HOLD discipline):

- Same publication gates as findings comments: ``CODATIEL_GITHUB_COMMENTS=on``,
  no operator pause, and the arming watermark must exist. The ack never
  fires on a disarmed or paused instance, and never backfills merged or
  closed PRs (live lifecycle is re-checked before any write).
- One comment per PR (``codatiel-ack`` marker names repo+PR). State
  changes PATCH the same comment; the body is re-rendered deterministically
  and a write happens only when the body would change, so every poll tick
  reconciles for free and retries/crashes converge without duplicates.
- History is embedded in the comment body itself (bounded): a newer head
  demotes the previous head to a short history list. No new store tables;
  the store is only used for the arming watermark.
- Never raises into the pipeline: :func:`sync_ack` returns outcome dicts.
- Email and findings-publication paths are untouched; the ack is
  advisory text and can never be a required merge check.

The ``gh`` transport follows the ``github.comments`` dual-purpose
convention: ``gh(path)`` for GETs, ``gh("POST", repo, pr, body)`` for
creation, ``gh("PATCH", repo, comment_id, body)`` for updates. Tests
pass fakes; production shells out to the gh CLI.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable

from codatiel_i2.github import comments as comments_mod

MARKER_KIND = "codatiel-ack"
MAX_HISTORY = 4

STATES = ("queued", "deferred", "in_progress", "completed", "superseded",
          "failed")

_STATE_LABEL = {
    "queued": "queued",
    "deferred": "deferred",
    "in_progress": "review running",
    "completed": "reviewed",
    "superseded": "superseded",
    "failed": "failed",
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CURRENT_RE = re.compile(r"^\*\*Current:\*\* `([0-9a-f]{7,40})` — (.+?)(?:: (.*))?$")
_HISTORY_RE = re.compile(r"^- `([0-9a-f]{7,40})` — (.+?)(?:: (.*))?$")


def marker_for(repo: str, pr: int) -> str:
    return f"<!-- {MARKER_KIND} {repo}#{pr} -->"


def _short(head: str) -> str:
    return head[:12]


def render_ack(*, repo: str, pr: int, head: str, state: str,
               detail: str = "",
               findings_comment_id: str = "",
               live_head: str = "",
               history: tuple[tuple[str, str, str], ...] = ()) -> str:
    """Deterministic ack body. Pure function of its inputs (no clock)."""
    if state not in _STATE_LABEL:
        raise ValueError(f"unknown ack state {state!r}")
    lines = [marker_for(repo, pr), "",
             "## Codatiel review status", ""]
    label = _STATE_LABEL[state]
    if state == "completed" and findings_comment_id:
        link = (f"https://github.com/{repo}/pull/{pr}"
                f"#issuecomment-{findings_comment_id}")
        current = (f"**Current:** `{_short(head)}` — {label}: "
                   f"[published findings]({link}).")
    elif state == "superseded" and live_head:
        current = (f"**Current:** `{_short(head)}` — {label}: superseded "
                   f"by `{_short(live_head)}`.")
        if detail:
            current += f" {detail}"
    else:
        current = f"**Current:** `{_short(head)}` — {label}."
        if detail:
            current += f" {detail}"
    if state == "in_progress":
        current += (" Findings are not ready — they appear only after "
                    "the review finishes.")
    elif state in ("queued", "deferred"):
        current += " No review has run on this commit."
    lines.append(current)
    lines.append("")
    kept = [(h, s, d) for (h, s, d) in history
            if h != head][:MAX_HISTORY]
    if kept:
        lines.append("### Previous heads")
        lines.append("")
        for h, s, d in kept:
            entry = f"- `{_short(h)}` — {s}."
            if d:
                entry += f" {d}"
            lines.append(entry)
        lines.append("")
    lines += ["---",
              "*Automated Codatiel status. Advisory only: this comment "
              "approves nothing, requests no changes, modifies no code, "
              "and can never block merging.*"]
    return "\n".join(lines) + "\n"


def parse_ack_body(body: str) -> dict | None:
    """Extract (head, state-word, detail, history) from our comment.

    Returns None when the body is not ours.
    """
    if MARKER_KIND not in (body or ""):
        return None
    current: tuple[str, str, str] | None = None
    history: list[tuple[str, str, str]] = []
    for line in (body or "").splitlines():
        m = _CURRENT_RE.match(line.strip())
        if m:
            current = (m.group(1), m.group(2), (m.group(3) or "").strip())
            continue
        m = _HISTORY_RE.match(line.strip())
        if m:
            history.append(
                (m.group(1), m.group(2), (m.group(3) or "").strip()))
    if current is None:
        return None
    return {"head": current[0], "state": current[1],
            "detail": current[2], "history": history}


def find_ack_comment(comments: list[dict], repo: str,
                     pr: int) -> dict | None:
    needle = f"{MARKER_KIND} {repo}#{pr}"
    for c in comments:
        if isinstance(c, dict) and needle in str(c.get("body") or ""):
            return c
    return None


def gh_patch_comment(repo: str, comment_id: str | int, body: str,
                     runner: Callable | None = None) -> dict:
    """Update one issue comment body. Returns the updated comment."""
    if runner is not None:
        out = runner("PATCH", repo, comment_id, body)
        if not isinstance(out, dict):
            raise RuntimeError("comment patch runner misbehaved")
        return out
    cmd = ["gh", "api", "--method", "PATCH",
           f"repos/{repo}/issues/comments/{comment_id}",
           "-f", f"body={body}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120)
    except FileNotFoundError as exc:
        raise RuntimeError(f"gh CLI is not available: {exc}")
    except Exception as exc:
        raise RuntimeError(f"gh patch failed: {type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh comment patch failed: {proc.stderr.strip()[:300]}")
    try:
        import json as _json
        payload = _json.loads(proc.stdout or "null")
    except ValueError as exc:
        raise RuntimeError(f"gh patch response not JSON: {exc}")
    if not isinstance(payload, dict):
        raise RuntimeError("gh patch response not an object")
    return payload


def _patch_runner(gh: Callable | None) -> Callable | None:
    if gh is None:
        return None

    def _runner(method: str, repo: str, comment_id: str | int,
                body: str) -> dict:
        assert method == "PATCH"
        return gh("PATCH", repo, comment_id, body)

    return _runner


def _history_for(parsed: dict | None, head: str, state: str,
                detail: str) -> tuple[tuple[str, str, str], ...]:
    """Carry prior heads forward; demote a replaced current head."""
    prior: list[tuple[str, str, str]] = []
    if parsed is not None:
        if parsed["head"] != _short(head) and parsed["head"] != head:
            prior.append((parsed["head"], parsed["state"],
                          parsed["detail"]))
        prior.extend(parsed["history"])
    out: list[tuple[str, str, str]] = []
    for h, s, d in prior:
        if h in (head, _short(head)) or (h, s, d) in out:
            continue
        out.append((h, s, d))
        if len(out) >= MAX_HISTORY:
            break
    return tuple(out)


def sync_ack(*, repo: str, pr: int, head: str, state: str,
             detail: str = "", findings_comment_id: str = "",
             live_head: str = "", store: Any = None,
             gh: Callable | None = None) -> dict:
    """Create or update the PR ack comment. Never raises.

    Returns an outcome dict (``posted`` / ``updated`` / ``already-current`` /
    ``refused`` / ``error``). A failed lookup is ambiguous and never a
    license to write.
    """
    try:
        return _sync_ack(repo=repo, pr=pr, head=head, state=state,
                         detail=detail,
                         findings_comment_id=findings_comment_id,
                         live_head=live_head, store=store, gh=gh)
    except Exception as exc:  # noqa: BLE001 - ack never blocks review
        return {"outcome": "error",
                "reason": f"{type(exc).__name__}: {exc}"[:300],
                "repo": repo, "pr": pr, "head_sha": head}


def _sync_ack(*, repo: str, pr: int, head: str, state: str,
              detail: str, findings_comment_id: str,
              live_head: str, store: Any,
              gh: Callable | None) -> dict:
    if state not in _STATE_LABEL:
        return {"outcome": "refused", "reason": f"unknown state {state!r}",
                "repo": repo, "pr": pr, "head_sha": head}
    if not _SHA_RE.match(head or ""):
        return {"outcome": "refused", "reason": "head SHA not pinned",
                "repo": repo, "pr": pr, "head_sha": head}
    try:
        pr_int = int(pr)
    except (TypeError, ValueError):
        return {"outcome": "refused", "reason": "PR not numeric",
                "repo": repo, "pr": pr, "head_sha": head}
    if not comments_mod.is_enabled():
        return {"outcome": "refused",
                "reason": (f"{comments_mod.SWITCH_ENV} is "
                           f"{comments_mod.switch()!r}; ack HOLD"),
                "repo": repo, "pr": pr_int, "head_sha": head}
    op_hold = comments_mod.operator_pause_reason()
    if op_hold is not None:
        return {"outcome": "refused", "reason": op_hold,
                "repo": repo, "pr": pr_int, "head_sha": head}
    if store is None or comments_mod.armed_at(store) is None:
        return {"outcome": "refused",
                "reason": "GitHub comments not armed; ack HOLD",
                "repo": repo, "pr": pr_int, "head_sha": head}
    call = gh or comments_mod.real_gh
    try:
        comments = comments_mod.list_issue_comments(repo, pr_int, call
                                                    if gh is not None
                                                    else None)
    except RuntimeError as exc:
        return {"outcome": "error",
                "reason": f"comment list unreachable ({exc})",
                "repo": repo, "pr": pr_int, "head_sha": head}
    existing = find_ack_comment(comments, repo, pr_int)
    parsed = parse_ack_body(str((existing or {}).get("body") or "")) \
        if existing is not None else None
    history = _history_for(parsed, head, state, detail)
    desired = render_ack(repo=repo, pr=pr_int, head=head, state=state,
                         detail=detail,
                         findings_comment_id=findings_comment_id,
                         live_head=live_head, history=history)
    if existing is not None and \
            str(existing.get("body") or "").strip() == desired.strip():
        return {"outcome": "already-current",
                "comment_id": str(existing.get("id") or ""),
                "repo": repo, "pr": pr_int, "head_sha": head}
    try:
        live = comments_mod.get_pr_live(repo, pr_int,
                                        call if gh is not None else None)
    except RuntimeError as exc:
        return {"outcome": "error",
                "reason": f"live PR state unknown ({exc}); ambiguous",
                "repo": repo, "pr": pr_int, "head_sha": head}
    if not live.get("head_sha") or live.get("state") != "open" or \
            live.get("merged"):
        return {"outcome": "refused",
                "reason": (f"PR lifecycle is {live.get('state')!r} "
                           f"(merged={live.get('merged')}); no ack on "
                           f"closed/merged PRs"),
                "repo": repo, "pr": pr_int, "head_sha": head}
    if existing is None:
        try:
            posted = comments_mod.gh_post_comment(
                repo, pr_int, desired, runner=_post_runner(gh))
        except RuntimeError as exc:
            return {"outcome": "error",
                    "reason": f"ack post failed ({exc})",
                    "repo": repo, "pr": pr_int, "head_sha": head}
        return {"outcome": "posted",
                "comment_id": str(posted.get("id") or ""),
                "repo": repo, "pr": pr_int, "head_sha": head}
    try:
        updated = gh_patch_comment(
            repo, existing.get("id") or "", desired,
            runner=_patch_runner(gh))
    except RuntimeError as exc:
        return {"outcome": "error",
                "reason": f"ack patch failed ({exc})",
                "repo": repo, "pr": pr_int, "head_sha": head}
    return {"outcome": "updated",
            "comment_id": str(updated.get("id")
                              or existing.get("id") or ""),
            "repo": repo, "pr": pr_int, "head_sha": head}


def _post_runner(gh: Callable | None) -> Callable | None:
    if gh is None:
        return None

    def _runner(method: str, repo: str, pr: int, body: str) -> dict:
        assert method == "POST"
        return gh("POST", repo, pr, body)

    return _runner
