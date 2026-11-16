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
  One consistent no-regression rule governs every write: same-head
  lifecycle rank never decreases (deferred 0 < queued 1 < in_progress 2
  < completed/superseded/failed 3), and an obsolete head never replaces
  the live head's status (it joins the bounded history instead).
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

import json
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

_LABEL_TO_STATE = {
    "queued": "queued",
    "deferred": "deferred",
    "review running": "in_progress",
    "reviewed": "completed",
    "superseded": "superseded",
    "failed": "failed",
}

# Lifecycle rank: acknowledgement state must never regress for the same
# head. Terminal states share the top rank; only the live head's status
# may stay current (see _sync_ack).
_STATE_RANK = {
    "deferred": 0,
    "queued": 1,
    "in_progress": 2,
    "completed": 3,
    "superseded": 3,
    "failed": 3,
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEAD_RE = re.compile(r"`([0-9a-f]{7,40})`")
_STATUS_RE = re.compile(
    r"^(queued|deferred|review running|reviewed|superseded|failed)"
    r"\b[.:]?\s*(.*)$")


def marker_for(repo: str, pr: int) -> str:
    return f"<!-- {MARKER_KIND} {repo}#{pr} -->"


def _short(head: str) -> str:
    return head[:12]


def _strip_one_dot(text: str) -> str:
    """Remove exactly one trailing period. Render always appends exactly
    one, so strip-one/append-one is a fixed point for any text."""
    t = (text or "").strip()
    return t[:-1].rstrip() if t.endswith(".") else t


def _split_status(segment: str) -> tuple[str, str]:
    """Split a status segment into (state key, detail).

    Unknown labels are preserved verbatim with rank -1 (never block a
    write, never crash the parser).
    """
    m = _STATUS_RE.match((segment or "").strip())
    if not m:
        return ("", _strip_one_dot(_strip_suffix(segment)))
    return (_LABEL_TO_STATE[m.group(1)],
            _strip_one_dot(_strip_suffix(m.group(2))))


_FINDINGS_RE = re.compile(r"#issuecomment-(\d+)")
_SUPERSEDED_BY_RE = re.compile(
    r"^superseded by `([0-9a-f]{7,40})`\.?\s*(.*)$")

# Renderer-appended suffixes (see render_ack). Stripped at parse time so
# caller detail round-trips without accumulating suffix copies.
_RENDER_SUFFIXES = (
    "Findings are not ready — they appear only after the review finishes.",
    "No review has run on this commit.",
)


def _strip_suffix(detail: str) -> str:
    t = (detail or "").strip()
    for suffix in _RENDER_SUFFIXES:
        if t.endswith(suffix):
            return t[:-len(suffix)].rstrip()
    return t


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
            if s and not _same_head(h, head)][:MAX_HISTORY]
    if kept:
        lines.append("### Previous heads")
        lines.append("")
        for h, s, d in kept:
            label = _STATE_LABEL.get(s, s)
            entry = f"- `{_short(h)}` — {label}."
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
    """Extract (head, state key, detail, history) from our comment.

    State words normalize to lifecycle keys; details lose exactly the
    one trailing period the renderer appends, so render/parse round
    trips without punctuation drift. Returns None when the body is not
    ours.
    """
    if MARKER_KIND not in (body or ""):
        return None
    current: tuple[str, str, str] | None = None
    history: list[tuple[str, str, str]] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        head_m = _HEAD_RE.search(stripped)
        if stripped.startswith("**Current:**") and head_m:
            segment = stripped.split("—", 1)[1] if "—" in stripped else ""
            key, detail = _split_status(segment)
            current = (head_m.group(1), key, detail)
            continue
        if stripped.startswith("- `") and head_m:
            segment = stripped.split("—", 1)[1] if "—" in stripped else ""
            key, detail = _split_status(segment)
            history.append((head_m.group(1), key, detail))
    if current is None:
        return None
    head, key, detail = current
    findings_id, live = "", ""
    if key == "completed":
        m = _FINDINGS_RE.search(detail)
        if m:
            findings_id = m.group(1)
    if key == "superseded":
        m = _SUPERSEDED_BY_RE.match(detail)
        if m:
            live, detail = m.group(1), _strip_one_dot(m.group(2))
    return {"head": head, "state": key, "detail": detail,
            "history": history, "findings_comment_id": findings_id,
            "live_head": live}


def _enrich_history(store: Any, repo: str, pr: int,
                    history: tuple[tuple[str, str, str], ...]
                    ) -> tuple[tuple[str, str, str], ...]:
    """Upgrade history entries whose heads completed in the store.

    A head recorded as deferred/queued/superseded/failed that later
    completed (findings published) renders as completed with its
    findings link. Lookups are local indexed reads; any failure keeps
    the original entry. Never raises.
    """
    out: list[tuple[str, str, str]] = []
    for h, s, d in history:
        if s == "completed":
            out.append((h, s, d))
            continue
        try:
            row = store.delivery_get_completed(repo, int(pr), h)
        except Exception:
            row = None
        if row is None:
            out.append((h, s, d))
            continue
        try:
            detail = json.loads(row.get("detail") or "{}")
            run_id = str(detail.get("run_id") or "")
            pub = store.publication_get(
                repo, int(pr), str(row.get("head_sha") or ""), run_id) \
                if run_id else None
            cid = str((pub or {}).get("comment_id") or "")
        except Exception:
            cid = ""
        link = (f"[published findings](https://github.com/{repo}"
                f"/pull/{pr}#issuecomment-{cid})" if cid else "")
        out.append((str(row.get("head_sha") or h), "completed", link))
    return tuple(out)


def _render_keep_current(*, repo: str, pr: int, parsed: dict,
                         extra: tuple[str, str, str],
                         store: Any = None) -> str:
    """Re-render preserving the existing current head exactly (including
    its findings link), with one more head folded into history."""
    merged = list(parsed["history"]) + [extra]
    kept: list[tuple[str, str, str]] = []
    for h, s, d in merged:
        if not s or _same_head(h, parsed["head"]) or (h, s, d) in kept:
            continue
        kept.append((h, s, d))
        if len(kept) >= MAX_HISTORY:
            break
    if store is not None:
        kept = list(_enrich_history(store, repo, pr, tuple(kept)))
    return render_ack(
        repo=repo, pr=pr, head=parsed["head"],
        state=parsed["state"] or "queued", detail=parsed["detail"],
        findings_comment_id=parsed.get("findings_comment_id") or "",
        live_head=parsed.get("live_head") or "", history=tuple(kept))


def _same_head(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a == b or a.startswith(b)
                                   or b.startswith(a))


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
    """Carry prior heads forward; demote a replaced current head.

    History entries are (head, state key, detail); the renderer maps
    keys back to display labels.
    """
    prior: list[tuple[str, str, str]] = []
    if parsed is not None:
        if not _same_head(parsed["head"], head):
            prior.append((parsed["head"], parsed["state"],
                          parsed["detail"]))
        prior.extend(parsed["history"])
    out: list[tuple[str, str, str]] = []
    for h, s, d in prior:
        if not s or _same_head(h, head) or (h, s, d) in out:
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
    history = _enrich_history(store, repo, pr_int, history)
    desired = render_ack(repo=repo, pr=pr_int, head=head, state=state,
                         detail=detail,
                         findings_comment_id=findings_comment_id,
                         live_head=live_head, history=history)
    if existing is not None and \
            str(existing.get("body") or "").strip() == desired.strip():
        return {"outcome": "already-current",
                "comment_id": str(existing.get("id") or ""),
                "repo": repo, "pr": pr_int, "head_sha": head}
    # No-regression rule, same head: a lower lifecycle rank never
    # replaces a higher one (a deferred gate event must not wipe a
    # completed or in-progress status for the same head).
    if parsed is not None and _same_head(parsed["head"], head):
        if _STATE_RANK.get(parsed["state"], -1) > \
                _STATE_RANK.get(state, -1):
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
    live_sha = str(live.get("head_sha") or "")
    # Currency rule: an obsolete head never replaces the live head's
    # status. The obsolete outcome joins the history instead; the live
    # head stays current (re-rendered losslessly, link included).
    if live_sha and not _same_head(live_sha, head) and parsed is not None \
            and _same_head(parsed["head"], live_sha):
        keep_body = _render_keep_current(
            repo=repo, pr=pr_int, parsed=parsed,
            extra=(head, state, _strip_one_dot(detail)), store=store)
        if str(existing.get("body") or "").strip() == keep_body.strip():
            return {"outcome": "already-current",
                    "comment_id": str(existing.get("id") or ""),
                    "repo": repo, "pr": pr_int, "head_sha": head}
        try:
            updated = gh_patch_comment(
                repo, existing.get("id") or "", keep_body,
                runner=_patch_runner(gh))
        except RuntimeError as exc:
            return {"outcome": "error",
                    "reason": f"ack patch failed ({exc})",
                    "repo": repo, "pr": pr_int, "head_sha": head}
        return {"outcome": "updated",
                "comment_id": str(updated.get("id")
                                  or existing.get("id") or ""),
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
