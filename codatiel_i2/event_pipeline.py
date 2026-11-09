"""Event-driven incremental review pipeline (TEST/DEV operational verification).

Contract (per-PR checkpoint, never updated_at):
  - Existing PR at enablement -> grandfathered, no historical review.
  - New PR opened -> one full-PR-diff review.
  - Draft -> deferred until ready_for_review; reviewed once if never reviewed.
  - New commits -> one incremental review over last_reviewed_head..new_head
    (three pushed commits = one combined review of the range).
  - Same SHA/event replayed, labels/comments/title edits, reopen unchanged
    -> no review, no inference charge.
  - Rebase / force-push / divergence -> compare ACTUAL patch changes
    (interdiff); never silently rescan the whole PR; unsafe ranges ->
    needs-attention with zero spend.
  - Diff cannot be safely determined (compare failure, >300-file
    truncation mirroring the GitHub compare cap) -> needs-attention, no
    spend. A failed comparison is never mistaken for an empty diff.
  - Newer commit during a review -> finish the pinned revision, queue the
    newer delta as a separate run. Evidence from different commits is
    never mixed.
  - Delivery-ID replay (webhook retry, worker restart) -> stored decision
    returned, no inference. A stale in_progress after restart becomes
    interrupted/needs-attention; it is never silently re-spent.

Money:
  - Separate TEST / DEV state dirs and spend ledgers (CODATIEL_ENV).
  - Configurable per-env ceiling; TEST+DEV must fit under the $30 project
    cap or enablement/ingest refuses. On ceiling breach: stop inference,
    record skipped events; reset never auto-processes backlog.
  - Results are private: run dirs under the env state dir, digest to the
    configured TEST/DEV destination only. No GitHub review comments ever
    (this module never imports github.publish).

Review execution reuses the qualified paths unchanged:
  - REVIEW_PIPELINE=terra_only -> canonical service review (Terra).
  - REVIEW_PIPELINE=terra_luna_additive -> additive.run_additive
    (Terra sealed first, Luna blind sweep, deterministic merge;
    Luna failure keeps Terra's complete review).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codatiel_i2 import event_store as store_mod

PIPELINE_MODES = ("terra_luna_additive", "terra_only")
DEFAULT_PIPELINE = "terra_luna_additive"
PROJECT_CAP_USD = 30.0
COMPARE_FILE_LIMIT = 300  # mirrors the GitHub compare API changed-file cap
REVIEW_RESERVE_USD = 1.0

METADATA_ACTIONS = {"labeled", "unlabeled", "assigned", "unassigned",
                    "edited", "review_requested", "comment_created",
                    "labeled_comment"}
CLOSED_ACTIONS = {"closed"}


@dataclass
class EventConfig:
    env: str
    state_dir: Path
    repos: list[str] = field(default_factory=list)
    pipeline: str = DEFAULT_PIPELINE
    spend_cap_usd: float = 20.0
    project_cap_usd: float = PROJECT_CAP_USD
    notify_to: list[str] = field(default_factory=list)
    notify_sender: str = ""
    compare_file_limit: int = COMPARE_FILE_LIMIT
    reserve_usd: float = REVIEW_RESERVE_USD
    stale_after_s: float = 3600.0
    repo_paths: dict[str, str] = field(default_factory=dict)
    clock: Callable[[], float] = time.time


def _env_list(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",")
            if p.strip()]


def config_from_env() -> EventConfig:
    """Build config from the environment. Fails closed on any violation."""
    env = (os.environ.get("CODATIEL_ENV", "") or "").strip().lower()
    if env not in ("test", "dev"):
        raise store_mod.StoreError(
            "CODATIEL_ENV must be 'test' or 'dev' (separate TEST/DEV "
            "state and ledgers); refusing to run unset")
    prefix = "CODATIEL_TEST_" if env == "test" else "CODATIEL_DEV_"
    state_dir = Path(os.environ.get(prefix + "STATE",
                                    f"./{env}_events_state"))
    repos = _env_list(prefix + "REPOS")
    default_cap = 20.0 if env == "test" else 5.0
    try:
        cap = float(os.environ.get(prefix + "SPEND_CAP_USD",
                                   str(default_cap)))
        project_cap = float(os.environ.get("CODATIEL_PROJECT_SPEND_CAP_USD",
                                           str(PROJECT_CAP_USD)))
    except ValueError:
        raise store_mod.StoreError("spend caps must be numeric")
    if cap <= 0 or project_cap <= 0:
        raise store_mod.StoreError("spend caps must be positive")
    other_prefix = "CODATIEL_DEV_" if env == "test" else "CODATIEL_TEST_"
    other_default = 5.0 if env == "test" else 20.0
    try:
        other_cap = float(os.environ.get(other_prefix + "SPEND_CAP_USD",
                                        str(other_default)))
    except ValueError:
        raise store_mod.StoreError("spend caps must be numeric")
    if cap + other_cap > project_cap:
        raise store_mod.StoreError(
            f"TEST cap ${cap:.2f} + DEV cap ${other_cap:.2f} exceeds the "
            f"${project_cap:.2f} project ceiling; refusing")
    pipeline = (os.environ.get("REVIEW_PIPELINE", DEFAULT_PIPELINE)
                or "").strip()
    if pipeline not in PIPELINE_MODES:
        raise store_mod.StoreError(
            f"REVIEW_PIPELINE must be one of {PIPELINE_MODES}; "
            f"got {pipeline!r}")
    repo_paths: dict[str, str] = {}
    for pair in _env_list(prefix + "REPO_PATHS"):
        if "=" not in pair:
            raise store_mod.StoreError(
                f"bad {prefix}REPO_PATHS entry {pair!r}; want repo=/path")
        repo, _, path = pair.partition("=")
        repo_paths[repo.strip()] = path.strip()
    return EventConfig(
        env=env, state_dir=state_dir, repos=repos, pipeline=pipeline,
        spend_cap_usd=cap, project_cap_usd=project_cap,
        notify_to=_env_list(prefix + "TO"),
        notify_sender=(os.environ.get(prefix + "SENDER", "") or "").strip(),
        repo_paths=repo_paths)


def verify_signature(payload_bytes: bytes,
                     signature: str | None) -> tuple[bool, str]:
    """HMAC-SHA256 webhook verification (GitHub X-Hub-Signature-256).

    When no secret is configured (local/test operation) any payload is
    accepted. When a secret IS configured, a missing or mismatched
    signature refuses the delivery before any state change.
    """
    secret = os.environ.get("CODATIEL_WEBHOOK_SECRET", "") or ""
    if not secret:
        return True, "no-secret-configured"
    if not signature:
        return False, "missing signature with secret configured"
    want = "sha256=" + hmac.new(secret.encode("utf-8"), payload_bytes,
                                hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, signature.strip()):
        return False, "signature mismatch"
    return True, "verified"


# -- event normalization -------------------------------------------------
def normalize_event(raw: dict, delivery_id: str,
                    now: float | None = None) -> tuple[dict | None, str]:
    """Normalize a pull_request / push webhook body to the pipeline shape.

    Never invents SHAs or PR numbers: unresolvable identity is a caller
    error, not a default.
    """
    ts = time.time() if now is None else now
    if not isinstance(raw, dict):
        return None, "payload is not an object"
    repo = (((raw.get("repository") or {}).get("full_name")) or "").strip()
    ev: dict[str, Any] = {"delivery_id": delivery_id, "repo": repo,
                          "received_ts": ts, "event": "",
                          "action": str(raw.get("action") or ""),
                          "pr_number": None, "branch": None,
                          "base_sha": None, "head_sha": None,
                          "before_sha": None, "forced": False,
                          "draft": None, "merged": False,
                          "closed": False, "created_at": None}
    if "pull_request" in raw and isinstance(raw["pull_request"], dict):
        pr = raw["pull_request"]
        ev["event"] = "pull_request"
        try:
            ev["pr_number"] = int(pr.get("number") or 0) or None
        except (TypeError, ValueError):
            return None, "pull_request number invalid"
        ev["branch"] = str((pr.get("head") or {}).get("ref") or "") or None
        ev["base_sha"] = str((pr.get("base") or {}).get("sha") or "") or None
        ev["head_sha"] = str((pr.get("head") or {}).get("sha") or "") or None
        ev["draft"] = pr.get("draft")
        ev["merged"] = bool(pr.get("merged"))
        ev["closed"] = bool(ev["action"] == "closed" or pr.get("state")
                            == "closed")
        ev["created_at"] = str(pr.get("created_at") or "") or None
    elif "commits" in raw or str(raw.get("ref", "")).startswith("refs/heads/"):
        ev["event"] = "push"
        ref = str(raw.get("ref") or "")
        ev["branch"] = ref.split("refs/heads/", 1)[1] if "refs/heads/" in ref else None
        ev["before_sha"] = str(raw.get("before") or "") or None
        after = str(raw.get("after") or "") or None
        head_commit = raw.get("head_commit") or {}
        ev["head_sha"] = after or (str(head_commit.get("id") or "") or None)
        ev["forced"] = bool(raw.get("forced"))
        if isinstance(raw.get("pr_number"), int):
            ev["pr_number"] = raw["pr_number"]
    else:
        kind = str(raw.get("hook") or raw.get("zen") or "unknown")
        ev["event"] = f"unsupported:{kind}"
    if not ev["repo"]:
        return None, "missing repository.full_name"
    return ev, ""


# -- eligibility -----------------------------------------------------------
METADATA_ONLY = "metadata"  # labels/comments/title: never a review


def classify_event(ev: dict, cp: dict | None,
                   enabled_at: str) -> tuple[str, str, dict]:
    """Pure eligibility decision. Returns (decision, reason, ctx).

    Decisions: review_full | review_incremental | skipped:<reason> |
    queued:<...> (refined at claim time). No inference, no repo access.
    Identity compares use SHAs and stored content, never updated_at.
    """
    ctx: dict[str, Any] = {}
    if ev["event"].startswith("unsupported"):
        return "skipped:unsupported", f"event {ev['event']} ignored", ctx
    if ev["event"] == "push" and ev["pr_number"] is None:
        ctx["need_branch_map"] = True
    if ev["closed"] or ev["action"] in CLOSED_ACTIONS:
        if ev.get("merged"):
            return "skipped:closed", "PR merged; nothing to review", ctx
        # Reopened with identical head is not new work; a changed head
        # falls through to the SHA comparison below.
        if ev["action"] == "reopened" and cp is not None and ev.get(
                "head_sha") and ev["head_sha"] == (
                cp.get("last_reviewed_head") or cp.get("baseline_head")):
            return "skipped:unchanged", "PR reopened with identical head", ctx
        if ev["closed"]:
            return "skipped:closed", "PR closed unmerged; nothing to review", ctx
    if ev["event"] == "pull_request" and not ev.get("head_sha"):
        return "needs_attention:identity", \
            "pull_request event lacks head SHA; refusing to guess", ctx
    if cp is None:
        created = str(ev.get("created_at") or "")
        if created and created <= enabled_at:
            return "skipped:historical", \
                "PR predates enablement; grandfathered, no review", ctx
        if ev.get("draft") is True:
            ctx["defer"] = True
            return "skipped:draft", \
                "draft PR deferred until ready_for_review", ctx
        ctx["range"] = "full"
        return "review_full", "new PR opened; review full PR diff once", ctx
    status = cp.get("status") or ""
    reviewed_head = cp.get("last_reviewed_head") or ""
    baseline = cp.get("baseline_head") or ""
    anchor = reviewed_head or baseline
    head = ev.get("head_sha") or ""
    base = ev.get("base_sha")
    if status == "in_progress":
        ctx["inflight"] = True
        return "review_incremental", \
            "PR has a pinned review in flight; claim decides queue", ctx
    if status == "interrupted":
        return "needs_attention:interrupted", \
            "prior review interrupted; operator must explicitly re-drive", ctx
    if ev.get("draft") is True and ev.get("action") != "ready_for_review":
        ctx["defer"] = True
        return "skipped:draft", "draft PR deferred until ready_for_review", ctx
    if head and head == reviewed_head and (not base or not cp.get(
            "last_reviewed_base") or base == cp["last_reviewed_base"]):
        if ev.get("action") in METADATA_ACTIONS:
            return "skipped:metadata", \
                "labels/comments/title change; SHAs identical", ctx
        return "skipped:unchanged", \
            "head identical to last successfully reviewed head", ctx
    if not head:
        return "needs_attention:identity", \
            "event carries no head SHA; refusing to guess", ctx
    if not anchor:
        ctx["range"] = "full"
        return "review_full", "no reviewed baseline; review full diff", ctx
    if ev.get("action") == "ready_for_review" and not reviewed_head:
        ctx["range"] = "full"
        return "review_full", \
            "draft ready with no prior review; review full diff once", ctx
    ctx["range"] = "incremental"
    ctx["from"] = anchor
    if base and cp.get("last_reviewed_base") and base != cp[
            "last_reviewed_base"]:
        ctx["rebase"] = True
    return "review_incremental", \
        "new commits; review only changes since last reviewed head", ctx


# -- git compare guard -------------------------------------------------------
def _git(repo_path: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_path), *args],
                          capture_output=True, text=True, timeout=timeout)


def compare_range(repo_path: Path, from_sha: str, to_sha: str, *,
                  file_limit: int = COMPARE_FILE_LIMIT
                  ) -> tuple[dict | None, str]:
    """Safely describe from..to. Never returns an empty diff on failure.

    Returns (info, "") with info {files, names, diff_sha, empty} or
    (None, reason) where reason is compare_failed | truncated |
    unknown_endpoint. Callers must treat None as needs-attention, never
    as "nothing changed".
    """
    for label, sha in (("from", from_sha), ("to", to_sha)):
        if not sha or not len(sha) >= 7:
            return None, f"unknown_endpoint: missing {label} SHA"
        proc = _git(repo_path, "cat-file", "-e", f"{sha}^{{commit}}")
        if proc.returncode != 0:
            return None, f"unknown_endpoint: {label} {sha[:12]} not present"
    names = _git(repo_path, "diff", "--name-only", "-z",
                 f"{from_sha}", f"{to_sha}", "--")
    if names.returncode != 0:
        return None, f"compare_failed: {names.stderr.strip()[:300]}"
    files = [p for p in names.stdout.split("\0") if p]
    if len(files) > file_limit:
        return None, (f"truncated: {len(files)} changed files exceed the "
                      f"{file_limit}-file comparison cap; refusing to "
                      f"review a range that cannot be enumerated")
    raw = _git(repo_path, "diff", "--raw", "--no-abbrev",
               f"{from_sha}", f"{to_sha}", "--")
    if raw.returncode != 0:
        return None, f"compare_failed: {raw.stderr.strip()[:300]}"
    digest = hashlib.sha256(raw.stdout.encode("utf-8")).hexdigest()
    return ({"files": len(files), "names": sorted(files),
             "diff_sha": digest, "empty": not files}, "")


def is_ancestor(repo_path: Path, maybe_ancestor: str,
                descendant: str) -> bool | None:
    """True/False, or None when ancestry cannot be determined."""
    try:
        proc = _git(repo_path, "merge-base", "--is-ancestor",
                    maybe_ancestor, descendant)
    except Exception:
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


# -- repo access ---------------------------------------------------------------
class RepoAccess:
    """Local checkout access for compare + review materialization."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def ensure(self, repo: str) -> tuple[bool, str]:
        if not (self.path / ".git").exists():
            try:
                probe = _git(self.path, "rev-parse", "--git-dir")
            except Exception as exc:
                return False, f"{type(exc).__name__}: {exc}"
            if probe.returncode != 0:
                return False, f"{self.path} is not a git checkout"
        return True, ""

    def compare(self, from_sha: str, to_sha: str, *,
                file_limit: int) -> tuple[dict | None, str]:
        return compare_range(self.path, from_sha, to_sha,
                             file_limit=file_limit)

    def ancestor(self, maybe_ancestor: str, descendant: str) -> bool | None:
        return is_ancestor(self.path, maybe_ancestor, descendant)


def _clone_dir(state_dir: Path, repo: str) -> Path:
    return state_dir / "clones" / repo.replace("/", "__")


def ensure_clone(state_dir: Path, repo: str) -> tuple[Path | None, str]:
    """Maintain a full read-only clone per repo for compare + review.

    Uses `gh repo clone` (operator's gh auth); updates via `git fetch`.
    Any failure is fail-closed: callers record needs-attention, spend
    nothing.
    """
    dest = _clone_dir(state_dir, repo)
    try:
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["gh", "repo", "clone", repo, str(dest)],
                capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                return None, f"clone failed: {proc.stderr.strip()[:300]}"
        else:
            proc = subprocess.run(
                ["git", "-C", str(dest), "fetch", "origin"],
                capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                return None, (f"fetch failed for {repo}: "
                              f"{proc.stderr.strip()[:300]}")
            head_ref = subprocess.run(
                ["gh", "api", f"repos/{repo}", "--jq", ".default_branch"],
                capture_output=True, text=True, timeout=60)
        return dest, ""
    except FileNotFoundError:
        return None, "gh CLI is not available"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def resolve_access(cfg: EventConfig, repo: str) -> tuple[Any, str]:
    """Local path override (tests / operators) else maintained clone."""
    override = (cfg.repo_paths or {}).get(repo)
    if override:
        acc = RepoAccess(Path(override))
        ok, err = acc.ensure(repo)
        return (acc, "") if ok else (None, err)
    dest, err = ensure_clone(cfg.state_dir, repo)
    if dest is None:
        return None, err
    acc = RepoAccess(dest)
    ok, err2 = acc.ensure(repo)
    return (acc, "") if ok else (None, err2)


# -- review driver ---------------------------------------------------------------
def _read_spend_usd(run_dir: str | None) -> float | None:
    if not run_dir:
        return None
    try:
        metrics = json.loads((Path(run_dir) / "metrics.json").read_text(
            encoding="utf-8"))
    except Exception:
        return None
    return (metrics.get("spend", {}) or {}).get("usd_estimate")


def run_review_for_range(*, cfg: EventConfig, repo: str, pr: int,
                         base: str, head: str, kind: str,
                         repo_path: Path,
                         runs_root: Path, work_root: Path,
                         model_factory: Any = None) -> dict:
    """Execute one review for base..head via the configured pipeline.

    Terra's canonical review runs first and unchanged; additive mode seals
    it and merges an independent Luna sweep deterministically. Any Luna
    or merge failure keeps Terra's complete result.
    """
    if cfg.pipeline == "terra_only":
        from codatiel_i2 import providers as providers_mod
        from codatiel_i2 import service as service_mod
        if model_factory is not None:
            model, model_name = model_factory("terra")
            rate, rate_status, settings = ({"input": 0.0, "output": 0.0},
                                           "test-priced", None)
        else:
            resolved = providers_mod.resolve_backend("terra")
            model, settings = providers_mod.build_model("terra")
            model_name = f"terra:{resolved['model']}"
            rate, rate_status = (resolved["rate_usd_per_mtok"],
                                resolved["rate_status"])
        out = service_mod.review_local(
            repo_path, base, head, model=model, model_name=model_name,
            runs_root=runs_root, work_root=work_root,
            rate=rate, rate_status=rate_status,
            inventory_strategy="bundle-0.1", enable_final=True)
        if out.get("status") not in ("complete", "inconclusive") or \
                not out.get("run_dir"):
            return {"status": out.get("status", "failed"),
                    "stage": out.get("stage"), "error": out.get("error"),
                    "mode": "terra-only-primary",
                    "run_dir": out.get("run_dir"),
                    "terra_findings": 0, "added": 0, "cost_usd": None,
                    "cost_unknown": True}
        cost = _read_spend_usd(out.get("run_dir"))
        return {"status": out["status"], "mode": "terra-only",
                "run_dir": out["run_dir"],
                "terra_findings": _count_terra(out["run_dir"]),
                "added": 0, "cost_usd": cost,
                "cost_unknown": cost is None}
    from codatiel_i2 import additive as additive_mod
    out = additive_mod.run_additive(
        mode="additive",
        target={"mode": "local", "case": f"{repo}#{pr}",
                "root": str(repo_path), "base": base, "head": head},
        runs_root=runs_root, work_root=work_root)
    if out.get("status") not in ("complete", "inconclusive") or \
            not out.get("run_dir"):
        return {"status": out.get("status", "failed"),
                "stage": out.get("stage"), "error": out.get("error"),
                "mode": out.get("mode", "terra-only-primary"),
                "run_dir": out.get("run_dir"),
                "terra_findings": 0, "added": 0, "cost_usd": None,
                "cost_unknown": True}
    parent = _read_spend_usd(out.get("run_dir")) or 0.0
    sub = out.get("sub_usd") or 0.0
    return {"status": out["status"], "mode": out.get("mode"),
            "run_dir": out["run_dir"],
            "terra_findings": _count_terra(out["run_dir"]),
            "added": int(out.get("added") or 0),
            "cost_usd": round(parent + sub, 4), "cost_unknown": False}


def _count_terra(run_dir: str) -> int:
    try:
        shadow = json.loads((Path(run_dir) / "shadow_result.json")
                            .read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(shadow.get("advisory", []) or [])


# -- orchestration ---------------------------------------------------------------
def _record(store: store_mod.Store, ev: dict, decision: str, reason: str,
             cost: float | None, detail: dict | None = None,
             now: float | None = None) -> dict:
    return store.delivery_record(
        delivery_id=ev["delivery_id"], event_type=ev["event"],
        action=ev.get("action") or "", repo=ev.get("repo") or "",
        pr=int(ev.get("pr_number") or 0), head_sha=ev.get("head_sha") or "",
        decision=decision, reason=reason, cost_usd=cost,
        detail=detail, now=now)


def _finalize_delivery(store: store_mod.Store, ev: dict, decision: str,
                       reason: str, cost: float | None) -> dict:
    store.delivery_update_decision(ev["delivery_id"], decision, reason,
                                   cost)
    return store.delivery_get(ev["delivery_id"]) or {}


def process_event(*, cfg: EventConfig, store: store_mod.Store,
                  ev: dict, model_factory: Any = None,
                  transport: Any = None) -> dict:
    """Process one normalized event. Returns the stored delivery row."""
    now = cfg.clock()
    store.require_enabled()
    prior = store.delivery_get(ev["delivery_id"])
    if prior is not None and prior.get("decision") not in ("in_progress",):
        # Webhook retry / worker restart: the stored decision stands and
        # the duplicate is recorded as a blocked replay. No inference.
        return store.delivery_mark_replay(ev["delivery_id"], prior)
    if prior is None:
        _record(store, ev, "in_progress", "claimed", None, now=now)
    cp = None
    if ev.get("pr_number"):
        cp = store.checkpoint_get(ev["repo"], int(ev["pr_number"]))
    elif ev.get("branch"):
        cp = _find_by_branch(store, ev["repo"], ev["branch"])
        if cp is not None:
            ev = dict(ev, pr_number=cp["pr"])
    if cp is None and ev.get("event") == "push" and not ev.get("pr_number"):
        return _finalize_delivery(
            store, ev, "needs_attention:unmapped_push",
            "push event maps to no known PR branch; refusing to guess "
            "identity (no inference, no spend)", None)
    decision, reason, ctx = classify_event(
        ev, cp, store.require_enabled())
    if decision.startswith("skipped:") or decision.startswith(
            "needs_attention:"):
        if decision == "skipped:draft" and ev.get("pr_number"):
            _remember_draft(store, ev, cp)
        return _finalize_delivery(store, ev, decision, reason, 0.0)
    # review path: resolve repo access, compute a safe range, check
    # ceiling, claim atomically, run, seal.
    assert ev.get("pr_number"), "review requires a PR number"
    pr = int(ev["pr_number"])
    access, err = resolve_access(cfg, ev["repo"])
    if access is None:
        return _finalize_delivery(
            store, ev, "needs_attention:repo_access",
            f"cannot access repo checkout: {err}", None)
    kind = "full" if decision == "review_full" else "incremental"
    from_sha = ctx.get("from") or ev.get("base_sha") or ev.get("before_sha")
    to_sha = ev.get("head_sha")
    if not from_sha or not to_sha:
        return _finalize_delivery(
            store, ev, "needs_attention:identity",
            "review range lacks base/head SHAs; refusing to guess", None)
    info, cerr = access.compare(from_sha, to_sha,
                                file_limit=cfg.compare_file_limit)
    if info is None:
        tag = cerr.split(":", 1)[0]
        if tag not in ("truncated", "compare_failed", "unknown_endpoint"):
            tag = "compare_failed"
        return _finalize_delivery(
            store, ev, f"needs_attention:{tag}", cerr, None)
    if info["empty"]:
        return _finalize_delivery(
            store, ev, "skipped:unchanged",
            "safe comparison shows an empty diff; no inference", 0.0)
    if kind == "incremental" and ctx.get("rebase"):
        anc = access.ancestor(from_sha, to_sha)
        if anc is None:
            return _finalize_delivery(
                store, ev, "needs_attention:compare_failed",
                "ancestry indeterminable after base change; refusing to "
                "review an unverifiable range", None)
        # Non-ancestor (force-push/rebase) or ancestor (fast-forward):
        # either way the from..to interdiff IS the actual patch change.
        # The failure mode that matters (from_sha gone) is already
        # excluded by compare_range's endpoint check.
    totals = store.spend_totals()
    if (totals["settled_usd"] + totals["reserved_usd"] +
            cfg.reserve_usd > cfg.spend_cap_usd):
        if ev.get("pr_number"):
            _remember_queued(store, ev, cp, from_sha, to_sha, "deferred")
        return _finalize_delivery(
            store, ev, "skipped:ceiling",
            f"spend cap ${cfg.spend_cap_usd:.2f} reached "
            f"(settled ${totals['settled_usd']:.4f}); inference stopped, "
            f"event recorded; no backlog processing on reset", 0.0)
    claim = store.claim_for_review(ev["repo"], pr, base=from_sha,
                                   head=to_sha, now=now)
    if claim == "queued":
        return _finalize_delivery(
            store, ev, "queued",
            "a review is pinned and running; newer delta queued as a "
            "separate run (evidence never mixed)", 0.0)
    if claim == "already":
        return _finalize_delivery(
            store, ev, "skipped:unchanged",
            "identical pinned review already in flight; no second spend",
            0.0)
    store.reserve(cfg.reserve_usd)
    result = run_review_for_range(
        cfg=cfg, repo=ev["repo"], pr=pr, base=from_sha, head=to_sha,
        kind=kind, repo_path=access.path,
        runs_root=cfg.state_dir / "runs", work_root=cfg.state_dir / "work",
        model_factory=model_factory)
    cost = result.get("cost_usd")
    unknown = bool(result.get("cost_unknown")) or cost is None
    store.settle(cfg.reserve_usd, None if unknown else float(cost or 0.0))
    if result["status"] not in ("complete", "inconclusive"):
        _remember_queued(store, ev, cp, from_sha, to_sha, "failed")
        store.review_record(
            repo=ev["repo"], pr=pr, kind=kind, base_sha=from_sha,
            head_sha=to_sha, diff_sha=info["diff_sha"], files=info["files"],
            pipeline=cfg.pipeline, status="failed",
            terra_findings=0, luna_added=0, cost_usd=cost,
            cost_unknown=unknown, run_dir=result.get("run_dir") or "")
        return _finalize_delivery(
            store, ev, "failed",
            f"primary review failed ({result.get('stage') or 'unknown'}: "
            f"{(result.get('error') or '')[:200]}); checkpoint not "
            f"advanced, Luna never spent", cost)
    done = store.complete_review(
        ev["repo"], pr, base=(ev.get("base_sha") or from_sha),
        head=to_sha, diff_sha=info["diff_sha"], files=info["files"],
        terra_findings=int(result.get("terra_findings") or 0),
        luna_added=int(result.get("added") or 0), cost_usd=cost,
        cost_unknown=bool(unknown), run_dir=result.get("run_dir") or "",
        result=result["status"], now=now)
    store.review_record(
        repo=ev["repo"], pr=pr, kind=kind, base_sha=from_sha,
        head_sha=to_sha, diff_sha=info["diff_sha"], files=info["files"],
        pipeline=cfg.pipeline, status=result["status"],
        terra_findings=int(result.get("terra_findings") or 0),
        luna_added=int(result.get("added") or 0), cost_usd=cost,
        cost_unknown=unknown, run_dir=result.get("run_dir") or "")
    notified = _notify(cfg, result.get("run_dir") or "",
                       result["status"], transport)
    final = _finalize_delivery(
        store, ev, "completed",
        f"{kind} review {result['status']} "
        f"(terra {result.get('terra_findings', 0)}, "
        f"luna-exclusive {result.get('added', 0)}; "
        f"notified={notified})", cost)
    # Drain a queued newer delta as its own run (separate evidence).
    if done.get("queued"):
        qb, qh = done["queued"]
        follow = dict(ev, delivery_id=ev["delivery_id"] + ":drain",
                      base_sha=to_sha, head_sha=qh, before_sha=to_sha,
                      action="synchronize")
        sub = store.delivery_get(follow["delivery_id"])
        if sub is None:
            _record(store, follow, "in_progress", "queued-drain", None,
                    now=now)
            out = process_event(cfg=cfg, store=store, ev=follow,
                                model_factory=model_factory,
                                transport=transport)
            final = {**final, "drained": out.get("decision")}
    return final


def _find_by_branch(store: store_mod.Store, repo: str,
                    branch: str) -> dict | None:
    for cp in store.list_checkpoints():
        if cp["repo"] == repo and cp.get("head_branch") == branch:
            return cp
    return None


def _remember_draft(store: store_mod.Store, ev: dict,
                    cp: dict | None) -> None:
    # Draft syncs only advance the deferred pointer; an existing
    # reviewed/grandfathered baseline is never clobbered.
    base_row = dict(cp) if cp else {}
    row = {**base_row, "repo": ev["repo"], "pr": int(ev["pr_number"]),
           "head_branch": ev.get("branch") or "",
           "pr_created_at": (ev.get("created_at") or
                             base_row.get("pr_created_at") or ""),
           "note": "draft deferred; no review run"}
    if not row.get("status"):
        row["status"] = "draft"
    reviewed = base_row.get("last_reviewed_head") or ""
    if reviewed:
        row["queued_base"] = reviewed
        row["queued_head"] = ev.get("head_sha") or ""
    elif not row.get("baseline_head"):
        row["baseline_head"] = ev.get("head_sha") or ""
    store.checkpoint_upsert(row)


def _remember_queued(store: store_mod.Store, ev: dict, cp: dict | None,
                     from_sha: str, to_sha: str,
                     fallback_status: str) -> None:
    # Never fabricate a "reviewed" checkpoint when no review ran: a new
    # PR keeps a deferred/failed status with the range preserved, so a
    # later event still drives the full range.
    base_row = dict(cp) if cp else {}
    row = {**base_row, "repo": ev["repo"], "pr": int(ev["pr_number"]),
           "head_branch": ev.get("branch") or "",
           "pr_created_at": (ev.get("created_at") or
                             base_row.get("pr_created_at") or ""),
           "queued_base": from_sha, "queued_head": to_sha}
    if not row.get("status"):
        row["status"] = fallback_status
        if fallback_status == "failed":
            row["note"] = "first review failed before completion"
        else:
            row["note"] = "deferred: spend ceiling reached before review"
    if not row.get("baseline_head") and not row.get("last_reviewed_head"):
        row["baseline_head"] = from_sha
    store.checkpoint_upsert(row)


def _notify(cfg: EventConfig, run_dir: str, status: str,
            transport: Any) -> bool:
    """Private digest to the configured env destination ONLY.

    Refuses anything but the configured TEST/DEV recipient list. No PR
    comments exist anywhere on this path.
    """
    if not run_dir or not cfg.notify_to or transport is None:
        return False
    try:
        from codatiel_i2 import service as service_mod
        receipt = service_mod.deliver_shadow(
            run_dir, to=list(cfg.notify_to), sender=cfg.notify_sender,
            transport=transport)
        return bool(receipt)
    except Exception:
        return False


# -- enable / recover / reset / status / dashboard -------------------------------
def enable(*, cfg: EventConfig, store: store_mod.Store,
           discover: Any = None) -> dict:
    """Arm collection: immutable watermark + grandfather open PRs.

    No inference, no notifications. Refsing an existing watermark
    refuses (history is never rewritten).
    """
    if store.meta_get("enabled_at"):
        raise store_mod.StoreError(
            f"already enabled at {store.meta_get('enabled_at')}; "
            f"watermark is immutable")
    if not cfg.notify_to or not cfg.notify_sender:
        raise store_mod.StoreError(
            "notification destination required before enable "
            f"({'TEST' if cfg.env == 'test' else 'DEV'}_TO/_SENDER)")
    if not cfg.repos:
        raise store_mod.StoreError(
            "no configured repositories; refusing to enable with an "
            "empty allowlist")
    from codatiel_i2 import watcher_eligibility as elig_mod
    discover = discover or elig_mod.discover_open_prs
    now = cfg.clock()
    enabled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    grandfathered: list[str] = []
    warnings: list[str] = []
    for repo in cfg.repos:
        rows, warn = discover(repo)
        if warn:
            warnings.append(f"{repo}: {warn}")
        if rows is None:
            warnings.append(f"{repo}: discovery failed; PRs there fall "
                            f"back to the created_at rule")
            continue
        for raw in rows:
            valid, err = elig_mod.validate_pr_metadata(repo, raw)
            if valid is None:
                warnings.append(f"{repo}: invalid PR row skipped ({err})")
                continue
            store.checkpoint_upsert({
                "repo": repo, "pr": valid.number,
                "head_branch": str(raw.get("headRefName") or ""),
                "pr_created_at": valid.created_at,
                "status": "grandfathered",
                "baseline_head": valid.head_sha,
                "last_reviewed_base": valid.base_sha,
                "note": "grandfathered at enablement; no historical "
                        "review"}, now=now)
            grandfathered.append(f"{repo}#{valid.number}")
    store.meta_set("enabled_at", enabled_at)
    store.meta_set("pipeline", cfg.pipeline)
    store.meta_set("repos", json.dumps(cfg.repos))
    return {"status": "ok", "env": cfg.env, "enabled_at": enabled_at,
            "pipeline": cfg.pipeline,
            "grandfathered": grandfathered, "warnings": warnings}


def recover(*, cfg: EventConfig, store: store_mod.Store) -> dict:
    """Convert stale in_progress rows to interrupted (no re-spend)."""
    now = cfg.clock()
    marked = []
    for cp in store.list_checkpoints():
        if cp.get("status") == "in_progress" and (
                now - float(cp.get("updated_ts") or 0)) >= cfg.stale_after_s:
            store.mark_interrupted(
                cp["repo"], cp["pr"],
                "worker restart: pinned review did not seal; operator "
                "must explicitly re-drive (no automatic re-spend)")
            marked.append(f"{cp['repo']}#{cp['pr']}")
    return {"status": "ok", "interrupted": marked}


def reset_spend(*, store: store_mod.Store) -> dict:
    """Zero the env spend ledger. Skipped/queued rows are untouched:
    nothing auto-processes after a reset."""
    before = store.spend_totals()
    store.spend_set("settled_usd", 0.0)
    store.spend_set("reserved_usd", 0.0)
    store.spend_set("unknown_holds", 0.0)
    return {"status": "ok", "cleared": before,
            "note": "backlog NOT processed; new events drive new work"}


def status(*, cfg: EventConfig, store: store_mod.Store) -> dict:
    totals = store.spend_totals()
    cps = store.list_checkpoints()
    return {"status": "ok", "env": cfg.env,
            "enabled_at": store.meta_get("enabled_at"),
            "pipeline": cfg.pipeline, "repos": cfg.repos,
            "spend_cap_usd": cfg.spend_cap_usd,
            "spend": totals,
            "spend_remaining_usd": round(
                max(0.0, cfg.spend_cap_usd - totals["settled_usd"] -
                    totals["reserved_usd"]), 4),
            "checkpoints": len(cps),
            "by_status": _counts(cps, "status"),
            "deliveries": store.deliveries_by_decision()}


def dashboard(*, cfg: EventConfig, store: store_mod.Store) -> dict:
    """Operating dashboard: reviewed / incremental / skipped, findings,
    completions, failures, costs. All from durable tables."""
    reviews = store.reviews_list()
    done = [r for r in reviews if r["status"] in ("complete",
                                                  "inconclusive")]
    failed = [r for r in reviews if r["status"] not in ("complete",
                                                        "inconclusive")]
    prs = {f"{r['repo']}#{r['pr']}" for r in done}
    incr_prs = {f"{r['repo']}#{r['pr']}" for r in done
                if r["kind"] == "incremental"}
    known_costs = [float(r["cost_usd"]) for r in done
                   if r["cost_usd"] is not None]
    totals = store.spend_totals()
    by_decision = store.deliveries_by_decision()
    skipped = {k: v for k, v in by_decision.items()
               if k.startswith("skipped:") or k.startswith("queued")}
    needs = {k: v for k, v in by_decision.items()
             if k.startswith("needs_attention:") or k == "failed"}
    blocked_replay = sum(v for k, v in by_decision.items()
                         if k in ("skipped:replay", "skipped:historical",
                                  "skipped:unchanged"))
    return {
        "env": cfg.env, "pipeline": cfg.pipeline,
        "enabled_at": store.meta_get("enabled_at"),
        "prs_reviewed": len(prs),
        "prs_incrementally_reviewed": len(incr_prs),
        "reviews_completed": len(done),
        "reviews_failed": len(failed),
        "skipped_events": skipped,
        "needs_attention_events": needs,
        "historical_replayed_blocked": blocked_replay,
        "terra_findings_total": sum(int(r["terra_findings"]) for r in done),
        "luna_exclusive_total": sum(int(r["luna_added"]) for r in done),
        "cumulative_spend_usd": round(totals["settled_usd"], 4),
        "reserved_usd": round(totals["reserved_usd"], 4),
        "unknown_cost_holds": int(totals["unknown_holds"]),
        "spend_cap_usd": cfg.spend_cap_usd,
        "avg_cost_per_reviewed_pr_usd": (
            round(sum(known_costs) / len(prs), 4) if prs and known_costs
            else 0.0),
    }


def _counts(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r.get(key) or "")] = out.get(str(r.get(key) or ""), 0) + 1
    return out
