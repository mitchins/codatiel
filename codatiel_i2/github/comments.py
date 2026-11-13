"""GitHub PR comment publisher (PROD-01 Track A). HOLD by default.

One consolidated top-level PR comment per qualifying review. Posting is
a deliberate arming action, never a side effect of reviewing:

- Global disable switch: ``CODATIEL_GITHUB_COMMENTS=off|on`` (default
  ``off``). Anything but ``on`` refuses before any network or render.
- Explicit arming watermark in the event store
  (``github_comments_armed_at``). Only reviews ingested AFTER arming
  (reviews-table ``finished_ts`` >= watermark) may publish; historical
  backlog never auto-publishes.
- Immutable review identity: the (repo, PR, SHAs, run) binding is
  re-verified from stored artifacts on every call; a label never
  overrides it.
- Live revalidation immediately before publish: the PR must be open
  and unmerged, and its live head must still equal the reviewed head.
  Stale/superseded/incomplete/budget-refused reviews are refused.
- Idempotent retries: the comment carries an HTML marker naming the
  review run; before posting we search existing PR comments for the
  marker. A crash between post and persist reconciles to the existing
  comment instead of double-posting. Outcomes persist in the
  ``publications`` table keyed by (repo, PR, head, run).
- Email is independent: this module never sends email and a publish
  refusal/failure must never block the digest email (callers notify
  first, publish second, in try/except).

The comment is a plain top-level issue comment
(``POST /repos/{repo}/issues/{pr}/comments``). That endpoint cannot
approve, request changes, or modify code — the format is advisory by
construction. Comment bodies reuse the delivery-independent digest
model (``digest_model.render_markdown``) so the GitHub comment, the
email, and the stored artifacts always agree.

Per-claim ``publication_eligibility()`` from ``github.publish`` is the
eligibility authority for the review-level verdict (supersession, head
match, known current head, known changed set). Per-claim path
routings are recorded in the publication row for audit; the
consolidated comment itself is pinned to the reviewed commit (every
source link names the reviewed SHA), so it stays accurate for that
commit regardless of later movement.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from codatiel_i2.github import publish as publish_gate_mod

SWITCH_ENV = "CODATIEL_GITHUB_COMMENTS"
ARMED_META_KEY = "github_comments_armed_at"
MARKER_KIND = "codatiel-review"
FILES_CAP = 300  # mirrors the compare API changed-file cap
COMPLETE_STATUSES = ("complete",)


def switch() -> str:
    """Raw switch value. Anything but 'on' means HOLD."""
    return (os.environ.get(SWITCH_ENV, "off") or "").strip().lower()


def is_enabled() -> bool:
    return switch() == "on"


def armed_at(store: Any) -> float | None:
    raw = store.meta_get(ARMED_META_KEY)
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def arm(store: Any, now: float | None = None) -> dict:
    """Write the arming watermark. Immutable: re-arming refuses.

    Only reviews with reviews-table finished_ts >= the watermark may
    publish, so enabling comments never replays historical backlog.
    """
    if armed_at(store) is not None:
        from codatiel_i2 import event_store as store_mod
        raise store_mod.StoreError(
            f"{ARMED_META_KEY} already set at "
            f"{store.meta_get(ARMED_META_KEY)}; arming is immutable")
    ts = time.time() if now is None else now
    store.meta_set(ARMED_META_KEY, str(float(ts)))
    return {"status": "ok", "armed_at": float(ts)}


def marker_for(run_id: str, head_sha: str) -> str:
    return (f"<!-- {MARKER_KIND} run={run_id} "
            f"head={head_sha} -->")


def find_our_comment(comments: list[dict], run_id: str) -> dict | None:
    needle = f"{MARKER_KIND} run={run_id}"
    for c in comments:
        if isinstance(c, dict) and needle in str(c.get("body") or ""):
            return c
    return None


# -- gh access (injectable for tests) ------------------------------------

def real_gh(path: str) -> Any:
    """Read-only gh passthrough (GET). Raises RuntimeError on failure."""
    cmd = ["gh", "api", "--paginate", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120)
    except FileNotFoundError as exc:
        raise RuntimeError(f"gh CLI is not available: {exc}")
    except Exception as exc:
        raise RuntimeError(f"gh call failed: {type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout or "null")
    except ValueError as exc:
        raise RuntimeError(f"gh response not JSON: {exc}")


def gh_post_comment(repo: str, pr: int, body: str,
                    runner: Callable | None = None) -> dict:
    """Post one top-level issue comment. Returns the created comment."""
    if runner is not None:
        out = runner("POST", repo, pr, body)
        if not isinstance(out, dict):
            raise RuntimeError("comment post runner misbehaved")
        return out
    cmd = ["gh", "api", "--method", "POST",
           f"repos/{repo}/issues/{pr}/comments", "-f", f"body={body}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120)
    except FileNotFoundError as exc:
        raise RuntimeError(f"gh CLI is not available: {exc}")
    except Exception as exc:
        raise RuntimeError(f"gh post failed: {type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh comment post failed: {proc.stderr.strip()[:300]}")
    try:
        payload = json.loads(proc.stdout or "null")
    except ValueError as exc:
        raise RuntimeError(f"gh post response not JSON: {exc}")
    if not isinstance(payload, dict):
        raise RuntimeError("gh post response not an object")
    return payload


def get_pr_live(repo: str, pr: int,
                gh: Callable[[str], Any] | None = None) -> dict:
    """Fresh PR truth: head/base SHAs, open state, merge flag."""
    call = gh or real_gh
    payload = call(f"repos/{repo}/pulls/{pr}")
    if not isinstance(payload, dict):
        raise RuntimeError("gh PR response not an object")
    try:
        head = str((payload.get("head") or {}).get("sha") or "")
        base = str((payload.get("base") or {}).get("sha") or "")
    except AttributeError:
        raise RuntimeError("gh PR response malformed")
    return {"head_sha": head, "base_sha": base,
            "state": str(payload.get("state") or ""),
            "merged": bool(payload.get("merged")),
            "draft": bool(payload.get("draft"))}


def get_changed_paths(repo: str, pr: int,
                      gh: Callable[[str], Any] | None = None,
                      limit: int = FILES_CAP) -> set[str] | None:
    """Current PR changed set, or None when it cannot be enumerated.

    None (API failure or >limit truncation mirroring the compare cap)
    is ambiguous — callers must refuse, never assume.
    """
    call = gh or real_gh
    payload = call(f"repos/{repo}/pulls/{pr}/files?per_page=100")
    if not isinstance(payload, list):
        return None
    paths = [str(f.get("filename") or "") for f in payload
             if isinstance(f, dict)]
    paths = [p for p in paths if p]
    if len(payload) >= limit and len(paths) >= limit:
        return None
    return set(paths)


def list_issue_comments(repo: str, pr: int,
                        gh: Callable[[str], Any] | None = None,
                        limit: int = 1000) -> list[dict]:
    call = gh or real_gh
    payload = call(f"repos/{repo}/issues/{pr}/comments?per_page=100")
    if not isinstance(payload, list):
        raise RuntimeError("gh comments response not a list")
    return [c for c in payload[:limit] if isinstance(c, dict)]


# -- review loading ---------------------------------------------------------

def _load_model(run_dir: Path) -> tuple[dict, dict, dict, dict]:
    """Bound model + raw review pieces. Raises RuntimeError on refusal."""
    from codatiel_i2 import digest_model as dm_mod
    from codatiel_i2 import review_identity as ri_mod
    try:
        ident = ri_mod.require_bound(run_dir, None)
    except ValueError as exc:
        raise RuntimeError(f"identity refused: {exc}")
    if ident is None:
        raise RuntimeError("run has no bound identity; refusing")
    try:
        run = json.loads((run_dir / "run.json").read_text(
            encoding="utf-8"))
        shadow = json.loads((run_dir / "shadow_result.json").read_text(
            encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"run artifacts unreadable: {exc}")
    model = dm_mod.build_review_model(run_dir)
    return model, ident, run, shadow


def _eligibility_claims(shadow: dict, model: dict) -> list[dict]:
    """Correctness + reuse claims in publication_eligibility() shape."""
    claims: list[dict] = []
    for f in (shadow.get("advisory") or []):
        if isinstance(f, dict):
            claims.append(f)
    for p in (model.get("semdup", {}).get("published", [])
              if isinstance(model.get("semdup"), dict) else []):
        if not isinstance(p, dict):
            continue
        ref = str(p.get("target_ref") or "")
        path = ref.split(":")[0] if ":" in ref else ref
        lines = ref.split(":")[1] if ":" in ref else ""
        try:
            start_s, _, end_s = lines.partition("-")
            start, end = int(start_s or 0), int(end_s or start_s or 0)
        except (TypeError, ValueError):
            start, end = 0, 0
        claims.append({
            "claim_id": f"reuse:{p.get('target_symbol')}:{path}",
            "changed_anchor": {"path": path, "line_start": start,
                               "line_end": end or start},
            "raw": {"title": str(p.get("comment") or "")[:120]},
            "title": str(p.get("target_symbol") or ""),
        })
    return claims


def build_comment_body(model: dict, ident: dict) -> str:
    """Marker + digest markdown + advisory footer. No review action."""
    from codatiel_i2 import digest_model as dm_mod
    lines = [marker_for(str(ident["run_id"]),
                        str(ident["head_sha"])), ""]
    lines.append(dm_mod.render_markdown(model).rstrip("\n"))
    lines += ["", "---",
              f"*Automated Codatiel review of commit "
              f"`{str(ident['head_sha'])[:12]}` "
              f"(run `{ident['run_id']}`). Advisory only: this comment "
              f"approves nothing, requests no changes, and modifies no "
              f"code. Uncorroborated items need human judgment — see "
              f"Review status labels. The full digest was emailed "
              f"separately; email delivery is independent of this "
              f"comment.*"]
    return "\n".join(lines) + "\n"


# -- production caller ------------------------------------------------------

def post_review_comment(*, run_dir: Path | str, store: Any,
                           gh: Callable | None = None,
                           now: float | None = None,
                           superseded_by: int | None = None,
                           ) -> dict:
    """Publish one consolidated comment for a qualifying review.

    ``gh`` is the injectable transport: ``gh(path)`` for GETs and
    ``gh("POST", repo, pr, body)`` for the comment post (tests pass
    fakes; production shells out to the gh CLI).
    """
    run_dir = Path(run_dir)
    if not is_enabled():
        return {"outcome": "refused", "reason":
                f"{SWITCH_ENV} is {switch()!r}; GitHub comments HOLD "
                f"(needs {SWITCH_ENV}=on)"}
    armed = armed_at(store)
    if armed is None:
        return {"outcome": "refused", "reason":
                "GitHub comments not armed (no arming watermark); "
                "historical backlog never auto-publishes"}
    try:
        model, ident, run, shadow = _load_model(run_dir)
    except RuntimeError as exc:
        return {"outcome": "refused", "reason": str(exc)[:300]}
    repo = str(ident["repo_slug"])
    try:
        pr = int(ident["pr_number"])
    except (TypeError, ValueError):
        return {"outcome": "refused", "reason": "bound PR not numeric"}
    head = str(ident["head_sha"])
    run_id = str(ident["run_id"])
    status = str(shadow.get("status") or "?")
    if status not in COMPLETE_STATUSES:
        return _refuse(store, repo, pr, head, run_id, now,
                       f"review status {status!r} is not complete; "
                       f"incomplete/budget-refused reviews never publish")
    if int(model.get("n_findings") or 0) + int(
            model.get("n_reuse") or 0) <= 0:
        return _refuse(store, repo, pr, head, run_id, now,
                       "review has no material content (no correctness "
                       "findings, no reuse suggestions); nothing to publish")
    finished = _review_finished_ts(store, str(run_dir))
    if finished is None:
        return _refuse(store, repo, pr, head, run_id, now,
                       "no reviews-table row corroborates this run dir; "
                       "unattested runs never publish")
    if finished < armed:
        return _refuse(store, repo, pr, head, run_id, now,
                       "review predates the arming watermark "
                       f"({finished} < {armed}); historical backlog "
                       "never auto-publishes")
    try:
        live = get_pr_live(repo, pr, gh)
    except RuntimeError as exc:
        return _refuse(store, repo, pr, head, run_id, now,
                       f"live PR state unknown ({exc}); ambiguous, "
                       "email-only")
    if not live.get("head_sha"):
        return _refuse(store, repo, pr, head, run_id, now,
                       "live PR head unknown; ambiguous, email-only")
    if live.get("state") != "open" or live.get("merged"):
        return _refuse(store, repo, pr, head, run_id, now,
                       f"PR lifecycle is {live.get('state')!r} "
                       f"(merged={live.get('merged')}); close/merge/reopen "
                       "never replays a publication")
    if live["head_sha"] != head:
        return _refuse(store, repo, pr, head, run_id, now,
                       f"PR head moved since review ({head[:12]} -> "
                       f"{live['head_sha'][:12]}); claim lines may be "
                       "stale, email-only")
    try:
        changed = get_changed_paths(repo, pr, gh)
    except RuntimeError as exc:
        return _refuse(store, repo, pr, head, run_id, now,
                       f"PR changed set unavailable ({exc}); ambiguous, "
                       "email-only")
    if changed is None:
        return _refuse(store, repo, pr, head, run_id, now,
                       "PR changed set unknown or exceeds the "
                       f"{FILES_CAP}-file cap; ambiguous, email-only")
    claims = _eligibility_claims(shadow, model)
    verdict = publish_gate_mod.publication_eligibility(
        claims=claims, bound_repo=repo, bound_pr=pr,
        reviewed_head_sha=head, current_head_sha=live["head_sha"],
        changed_paths=changed, existing_threads=[],
        superseded_by=superseded_by)
    email_only = verdict.get("email_only") or []
    audit = "; ".join(str(e.get("reason") or "") for e in email_only[:5])
    if verdict.get("reason", "").startswith(
            ("superseded", "head moved", "ambiguous")):
        return _refuse(store, repo, pr, head, run_id, now,
                       f"publication eligibility refused: "
                       f"{verdict.get('reason')}")
    # Idempotent retry: an existing row reconciles against the live
    # comment list before any post. A failed lookup is ambiguous — it
    # must never fall through to a duplicate post.
    existing = store.publication_get(repo, pr, head, run_id)
    if existing is not None and existing.get("outcome") == "posted":
        found, ok = _reconcile(repo, pr, run_id, gh)
        if not ok:
            return _refuse(store, repo, pr, head, run_id, now,
                           "posted row exists but the live comment list "
                           "is unreachable; refusing to risk a duplicate")
        if found is not None:
            return {**existing, "outcome": "already-posted",
                    "comment_id": str(found.get("id") or
                                      existing.get("comment_id"))}
        # Row says posted but the comment is gone (deleted): repost
        # once and overwrite the row.
    elif existing is not None and existing.get("outcome") != "posted":
        # A prior refusal is not sticky: conditions may have changed
        # (armed later, PR reopened). Re-evaluation above already ran.
        pass
    else:
        # Crash-between-post-and-persist: no row, but the comment may
        # already exist. Reconcile before posting.
        found, ok = _reconcile(repo, pr, run_id, gh)
        if not ok:
            return _refuse(store, repo, pr, head, run_id, now,
                           "live comment list unreachable; refusing to "
                           "risk a duplicate post")
        if found is not None:
            cid = str(found.get("id") or "")
            return store.publication_record(
                repo=repo, pr=pr, head_sha=head, run_id=run_id,
                comment_id=cid, outcome="posted",
                reason=("reconciled existing comment after a post/persist "
                        "gap; no duplicate posted"),
                now=now)
    try:
        body = build_comment_body(model, ident)
    except Exception as exc:
        return _refuse(store, repo, pr, head, run_id, now,
                       f"comment render failed: {type(exc).__name__}: {exc}")
    try:
        posted = gh_post_comment(repo, pr, body,
                                 runner=_post_runner(gh))
    except RuntimeError as exc:
        return _refuse(store, repo, pr, head, run_id, now,
                       f"comment post failed ({exc}); email unaffected")
    comment_id = str(posted.get("id") or "")
    reason = (f"posted consolidated comment for {repo}#{pr} "
              f"{head[:12]} run {run_id}")
    if audit:
        reason += f" [claim-level email-only: {audit[:200]}]"
    if existing is not None:
        store.publication_update(repo, pr, head, run_id, comment_id,
                                 "posted", reason, now=now)
        return {"outcome": "posted", **store.publication_get(
            repo, pr, head, run_id)}
    return store.publication_record(
        repo=repo, pr=pr, head_sha=head, run_id=run_id,
        comment_id=comment_id, outcome="posted", reason=reason, now=now)


def _review_finished_ts(store: Any, run_dir: str) -> float | None:
    try:
        rows = store.reviews_list()
    except Exception:
        return None
    for row in rows:
        if str(row.get("run_dir") or "") == run_dir:
            try:
                return float(row.get("finished_ts") or 0) or None
            except (TypeError, ValueError):
                return None
    return None


def _reconcile(repo: str, pr: int, run_id: str,
                gh: Callable | None) -> tuple[dict | None, bool]:
    """Return (our comment or None, lookup-ok).

    ok=False means the live list is unreachable — ambiguous, never a
    license to post.
    """
    try:
        comments = list_issue_comments(repo, pr, gh)
    except RuntimeError:
        return None, False
    return find_our_comment(comments, run_id), True


def _post_runner(gh: Callable | None) -> Callable | None:
    """Adapt the dual-purpose gh fake to the poster. None = real CLI."""
    if gh is None:
        return None
    def _runner(method: str, repo: str, pr: int, body: str) -> dict:
        assert method == "POST"
        return gh("POST", repo, pr, body)
    return _runner


def _refuse(store: Any, repo: str, pr: int, head: str, run_id: str,
             now: float | None, reason: str) -> dict:
    try:
        row = store.publication_get(repo, pr, head, run_id)
        if row is not None and row.get("outcome") == "posted":
            return {"outcome": "refused", "reason": reason,
                    **{k: row.get(k) for k in
                       ("repo", "pr", "head_sha", "run_id", "comment_id")}}
        if row is None:
            row = store.publication_record(
                repo=repo, pr=pr, head_sha=head, run_id=run_id,
                comment_id="", outcome="refused", reason=reason, now=now)
        else:
            store.publication_update(repo, pr, head, run_id, "",
                                     "refused", reason, now=now)
            row = store.publication_get(repo, pr, head, run_id)
        return {"outcome": "refused", "reason": reason, **row}
    except Exception:
        return {"outcome": "refused", "reason": reason,
                "repo": repo, "pr": pr, "head_sha": head, "run_id": run_id}
