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

# COST-01B: safe run_dir handling for Sonar path-traversal guard
def _safe_run_dir(run_dir: str | Path | None, state_dir: Path | None) -> Path | None:
    """Validate run_dir is within state_dir (prevents path traversal).

    Returns resolved Path if safe, else None. Sonar flags `Path(run_dir)/"semdup.json"`
    when run_dir is from `result` (tainted), but `result.run_dir` is always
    `state_dir/runs/<id>` created by the harness. This check makes the
    guarantee explicit and quiets the security hotspot without changing
    behavior. Strict `is_relative_to` ensures sibling dirs like
    `/srv/state-backup` are rejected while legitimate nested
    `state/runs/<id>` is accepted (requires Python 3.10+).
    """
    if not run_dir or state_dir is None:
        return None
    try:
        p = Path(run_dir).resolve()
        s = Path(state_dir).resolve()
        if p.is_relative_to(s):
            return p
    except Exception:
        return None
    return None

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
    # Integrated semantic-duplication stage (SEMDUP-08, DEV-only).
    # TEST keeps the pipeline exactly as before (forced off below).
    # COMPLETENESS-01: one aggregate spending guard per PR covers the
    # normal review and the semdup stage together. No independently
    # restrictive semantic pair-count ceiling exists: max_pairs 0
    # disables the experimental pair cap entirely (normal operation);
    # a positive max_pairs is an explicit controlled-experiment limit.
    semdup_enabled: bool = False
    semdup_max_pairs: int = 0
    semdup_allow_embeddings: bool = True
    whole_pr_ceiling_usd: float = 2.0
    # PROD-01 Track B (additive operator layer): effective controls
    # resolved from [state_dir]/operator.json when present, else env
    # behaviour unchanged. daily_cap_usd None = no daily budget;
    # email_enabled False = digests suppressed (records the refusal);
    # operator_source records which layer produced this config.
    daily_cap_usd: float | None = None
    email_enabled: bool = True
    operator_source: str = "env"


# BUDGET-RETRY-01: bounded re-drive of pure budget refusals. A
# ceiling-skipped (repo, PR, head) that is re-ingested under the same
# deterministic delivery ID may be reviewed once spend budget is
# available again — same head only, live-head confirmed, no prior
# review of the head, and at most BUDGET_RETRY_MAX_ATTEMPTS re-drives.
# Every attempt re-checks all spend caps before any inference; attempts
# that are refused again keep their counter so the bound holds.
BUDGET_RETRY_MAX_ATTEMPTS = 5


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
    # Integrated semdup stage: DEV-only, TEST forced off (unchanged).
    # COMPLETENESS-01: the experimental pair cap defaults to DISABLED
    # (0 = no count limit); the spending guard is the whole-PR ceiling.
    semdup_enabled, semdup_max_pairs, semdup_allow_emb = False, 0, True
    whole_pr_ceiling = 2.0
    if env == "dev":
        if (os.environ.get("CODATIEL_DEV_SEMDUP", "1") or "").strip() == "1":
            try:
                semdup_max_pairs = int(os.environ.get(
                    "CODATIEL_DEV_SEMDUP_MAX_PAIRS", "0"))
                whole_pr_ceiling = float(os.environ.get(
                    "CODATIEL_DEV_WHOLE_PR_CEILING_USD", "2.0"))
            except ValueError:
                raise store_mod.StoreError(
                    "CODATIEL_DEV_SEMDUP_MAX_PAIRS / "
                    "CODATIEL_DEV_WHOLE_PR_CEILING_USD must be numeric")
            if semdup_max_pairs < 0 or whole_pr_ceiling <= 0:
                raise store_mod.StoreError(
                    "semdup max pairs must be >= 0 and the whole-PR "
                    "ceiling must be positive")
            semdup_enabled = True
            semdup_allow_emb = (
                os.environ.get("CODATIEL_DEV_SEMDUP_ALLOW_EMBEDDINGS",
                               "1") or "").strip() != "0"
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
        repo_paths=repo_paths,
        semdup_enabled=semdup_enabled,
        semdup_max_pairs=semdup_max_pairs,
        semdup_allow_embeddings=semdup_allow_emb,
        whole_pr_ceiling_usd=whole_pr_ceiling)


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
        # Git SHAs are case-insensitive; canonicalize to lowercase so
        # an uppercase sender never forks identity (re-review loops,
        # missed replay detection, checkpoint mismatch).
        ev["base_sha"] = (str((pr.get("base") or {}).get("sha") or "")
                          or None)
        if ev["base_sha"] is not None:
            ev["base_sha"] = ev["base_sha"].lower()
        ev["head_sha"] = (str((pr.get("head") or {}).get("sha") or "")
                          or None)
        if ev["head_sha"] is not None:
            ev["head_sha"] = ev["head_sha"].lower()
        ev["draft"] = pr.get("draft")
        ev["merged"] = bool(pr.get("merged"))
        ev["closed"] = bool(ev["action"] == "closed" or pr.get("state")
                            == "closed")
        ev["created_at"] = str(pr.get("created_at") or "") or None
    elif "commits" in raw or str(raw.get("ref", "")).startswith("refs/heads/"):
        ev["event"] = "push"
        ref = str(raw.get("ref") or "")
        ev["branch"] = ref.split("refs/heads/", 1)[1] if "refs/heads/" in ref else None
        ev["before_sha"] = (str(raw.get("before") or "") or None)
        if ev["before_sha"] is not None:
            ev["before_sha"] = ev["before_sha"].lower()
        after = str(raw.get("after") or "") or None
        head_commit = raw.get("head_commit") or {}
        ev["head_sha"] = after or (str(head_commit.get("id") or "") or None)
        if ev["head_sha"] is not None:
            ev["head_sha"] = ev["head_sha"].lower()
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
    if (ev["event"] == "pull_request" and status == "draft"
            and not reviewed_head
            and (ev.get("draft") is False
                 or ev.get("action") == "ready_for_review")):
        # Readiness transition with an UNCHANGED head: the deferred draft
        # baseline is the review range. This must review even though the
        # head matches the deferred pointer — an unchanged-head skip
        # here would deadlock draft->ready forever (the poller re-ingests
        # the same head with draft=false under the same delivery ID,
        # which process_event re-roots via the skipped:draft row).
        ctx["range"] = "full"
        return "review_full", \
            "draft marked ready with no prior review; review full diff once", ctx
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


def _bind_review_identity(repo: str, pr: int, base: str, head: str,
                          run_dir: str,
                          delivery_id: str | None = None) -> dict:
    """Bind the (repo, PR, SHAs, run) identity at review creation.

    Called with the same event arguments that drive the review, so the
    binding is consistent by construction; every later consumer
    (semdup stage, digest model, delivery) re-verifies it instead of
    trusting a caller label. The run ID comes from the stored run
    artifact (not the directory name). Refuses loudly — an unbound
    review can never be delivered under a PR identity.
    """
    from codatiel_i2 import review_identity as identity_mod
    try:
        stored = json.loads((Path(run_dir) / "run.json").read_text(
            encoding="utf-8"))
        artifact_run_id = str(stored.get("run_id") or "")
    except Exception:
        artifact_run_id = ""
    return identity_mod.bind_run(
        run_dir, repo_slug=repo, pr_number=pr, base_sha=base,
        head_sha=head, run_id=artifact_run_id or Path(run_dir).name,
        binder="pipeline:run_review_for_range",
        delivery_id=delivery_id)


def _maybe_semdup_stage(*, cfg: EventConfig, repo: str, pr: int,
                         base: str, head: str, repo_path: Path,
                         runs_root: Path, run_dir: str | None,
                         result: dict,
                         is_stale: Callable[[], bool] | None = None) -> dict:
    """Integrated duplication stage (COMPLETENESS-01): direct retrieval
    into Terra/Luna after the standard review completes. DEV-only,
    bounded by the whole-PR remainder, never raises, never changes the
    standard review's status: standard findings stand even when the
    stage is unavailable.

    The stage spends at most (whole-PR ceiling minus the normal review's
    recorded actual). A zero/negative remainder, or an unknown normal
    cost, is an explicit unavailable — never silent, never
    over-budget.

    COST-01B: when `is_stale` reports the target head is no longer
    current, the stage is skipped before any paid inference.
    """
    result["semdup_status"] = "off"
    result["semdup_published"] = 0
    result["semdup_spend_usd"] = 0.0
    result["semdup_spend_unknown"] = False
    result["semdup_terra_spend_usd"] = 0.0
    result["semdup_luna_spend_usd"] = 0.0
    result["semdup_terra_spend_unknown"] = False
    result["semdup_luna_spend_unknown"] = False
    result["semdup_pairs_examined"] = 0
    result["semdup_pairs_reused"] = 0
    result["semdup_pairs_cache_hits"] = 0
    result["semdup_budget_usd"] = 0.0
    result["semdup_decisions_mode"] = "off"
    result["semdup_decisions_screened"] = 0
    result["semdup_decisions_spend_usd"] = 0.0
    result["semdup_decisions_spend_unknown"] = False
    if cfg.env != "dev" or not cfg.semdup_enabled or not run_dir:
        return result
    # COST-01B: live-head check before starting semantic duplication
    if is_stale is not None:
        try:
            if is_stale():
                result["semdup_status"] = "stale_skipped"
                result["semdup_reason"] = "target head no longer current before duplication; no inference issued"
                result["semdup_spend_usd"] = 0.0
                # Record provenance for stale skip
                result["semdup_pairs_examined"] = 0
                result["semdup_pairs_reused"] = 0
                result["semdup_pairs_skipped"] = result.get("semdup_pairs_skipped", 0)
                return result
        except Exception:
            pass
    normal_cost = result.get("cost_usd")
    if not isinstance(normal_cost, (int, float)) or result.get(
            "cost_unknown"):
        result["semdup_status"] = "unavailable"
        result["semdup_reason"] = (
            "normal review cost unknown; refusing semdup spend without "
            "an accountable whole-PR remainder")
        return result
    remaining = round(float(cfg.whole_pr_ceiling_usd) - float(normal_cost),
                      6)
    result["semdup_budget_usd"] = max(remaining, 0.0)
    if remaining <= 0:
        result["semdup_status"] = "unavailable"
        result["semdup_reason"] = (
            f"whole-PR ceiling ${cfg.whole_pr_ceiling_usd:.2f} already "
            f"reached by the normal review (${float(normal_cost):.4f}); "
            "no semdup inference attempted")
        return result
    try:
        from codatiel_i2.semdup_dev import stage as stage_mod
        sem = stage_mod.run_stage(
            repo_path=repo_path, repo=repo, pr=pr, base_rev=base,
            head_rev=head, run_dir=run_dir,
            state_dir=cfg.state_dir,
            max_pairs=(cfg.semdup_max_pairs or None),
            ceiling_usd=remaining,
            allow_embeddings=cfg.semdup_allow_embeddings,
            is_stale=is_stale)
    except Exception as exc:  # noqa: BLE001 - belt and braces; run_stage never raises
        sem = {"status": "unavailable", "published": [],
               "spend_usd": 0.0, "spend_unknown": True,
               "reason": f"stage harness error: {type(exc).__name__}"}
    spend = sem.get("spend_usd")
    try:
        spend = float(spend) if spend is not None else None
    except (TypeError, ValueError):
        spend, sem_unknown = None, True
        sem["spend_unknown"] = True
    else:
        sem_unknown = bool(sem.get("spend_unknown"))
    base_cost = result.get("cost_usd")
    if isinstance(base_cost, (int, float)):
        # Unknown semdup spend never collapses to confirmed $0: keep the
        # base cost and mark the total unknown.
        result["cost_usd"] = round(float(base_cost) + (spend or 0.0), 6)
    elif spend is not None:
        result["cost_usd"] = round(spend, 6)
    if sem_unknown:
        result["cost_unknown"] = True
    result["semdup_status"] = sem.get("status", "unavailable")
    result["semdup_published"] = len(sem.get("published") or [])
    # Per-model spend from the priced ledger results (persisted by the
    # stage at completion). None = unknown, never $0 confirmed.
    result["semdup_spend_usd"] = spend
    result["semdup_spend_unknown"] = sem_unknown
    result["semdup_terra_spend_usd"] = sem.get("spend_terra_usd")
    result["semdup_luna_spend_usd"] = sem.get("spend_luna_usd")
    result["semdup_terra_spend_unknown"] = bool(sem.get("spend_terra_unknown", sem_unknown))
    result["semdup_luna_spend_unknown"] = bool(sem.get("spend_luna_unknown", sem_unknown))
    # Reuse counts stay visible even on cache-only reviews (examined may
    # be 0 while reused carries the work).
    for _k in ("pairs_examined", "pairs_reused", "pairs_cache_hits",
               "pairs_total", "continuation_reused", "spend_unknown"):
        if sem.get(_k) is not None:
            result["semdup_" + _k] = sem.get(_k)
    # Decisions routing visibility (H16): mode and screened count ride
    # with the run result so operators can see screening and its spend.
    _ds = sem.get("decisions_screen") or {}
    result["semdup_decisions_mode"] = _ds.get("mode", "off")
    result["semdup_decisions_screened"] = _ds.get("screened", 0)
    result["semdup_decisions_spend_usd"] = sem.get("spend_decisions_usd")
    result["semdup_decisions_spend_unknown"] = bool(
        sem.get("spend_decisions_unknown", False))
    result["semdup_reason"] = str(sem.get("reason") or "")[:300]
    return result


def run_review_for_range(*, cfg: EventConfig, repo: str, pr: int,
                         base: str, head: str, kind: str,
                         repo_path: Path,
                         runs_root: Path, work_root: Path,
                         model_factory: Any = None,
                         is_stale: Callable[[], bool] | None = None,
                         delivery_id: str | None = None) -> dict:
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
        # Whole-PR breaker covers the normal review too: DEV caps the
        # primary review at the whole-PR ceiling (ordinary PRs never
        # approach it); TEST keeps the long-standing default.
        primary_cap = (cfg.whole_pr_ceiling_usd if cfg.env == "dev"
                       else None)
        out = service_mod.review_local(
            repo_path, base, head, model=model, model_name=model_name,
            runs_root=runs_root, work_root=work_root,
            rate=rate, rate_status=rate_status,
            max_usd_per_review=primary_cap,
            inventory_strategy="bundle-0.1", enable_final=True,
            delivery_id=delivery_id)
        if out.get("status") not in ("complete", "inconclusive") or \
                not out.get("run_dir"):
            return {"status": out.get("status", "failed"),
                    "stage": out.get("stage"), "error": out.get("error"),
                    "mode": "terra-only-primary",
                    "run_dir": out.get("run_dir"),
                    "terra_findings": 0, "added": 0, "cost_usd": None,
                    "cost_unknown": True}
        cost = _read_spend_usd(out.get("run_dir"))
        result = {"status": out["status"], "mode": "terra-only",
                "run_dir": out["run_dir"],
                "terra_findings": _count_terra(out["run_dir"]),
                "added": 0, "cost_usd": cost,
                "cost_unknown": cost is None,
                "terra_spend_usd": cost,
                "luna_spend_usd": 0.0}
        try:
            _bind_review_identity(repo, pr, base, head, out["run_dir"],
                                    delivery_id=delivery_id)
        except Exception as exc:
            return {"status": "failed", "stage": "identity",
                    "error": f"identity binding refused: {exc}",
                    "mode": "terra-only-primary",
                    "run_dir": out.get("run_dir"),
                    "terra_findings": 0, "added": 0, "cost_usd": cost,
                    "cost_unknown": cost is None}
        return _maybe_semdup_stage(
            cfg=cfg, repo=repo, pr=pr, base=base, head=head,
            repo_path=repo_path, runs_root=runs_root,
            run_dir=out.get("run_dir"), result=result, is_stale=is_stale)
    from codatiel_i2 import additive as additive_mod
    # Whole-PR breaker: the Terra primary is capped at the whole-PR
    # ceiling in DEV (ordinary PRs never approach it); the Luna subagent
    # keeps its own smaller ceiling, bounded above by the whole-PR
    # ceiling so the two together cannot exceed it. TEST untouched.
    primary_cap = (cfg.whole_pr_ceiling_usd if cfg.env == "dev" else None)
    sub_cap = 0.25 if cfg.env != "dev" else min(
        0.25, cfg.whole_pr_ceiling_usd)
    out = additive_mod.run_additive(
        mode="additive",
        target={"mode": "local", "case": f"{repo}#{pr}",
                "root": str(repo_path), "base": base, "head": head},
        runs_root=runs_root, work_root=work_root,
        primary_ceiling_usd=primary_cap, sub_ceiling_usd=sub_cap,
        delivery_id=delivery_id)
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
    result = {"status": out["status"], "mode": out.get("mode"),
            "run_dir": out["run_dir"],
            "terra_findings": _count_terra(out["run_dir"]),
            "added": int(out.get("added") or 0),
            "cost_usd": round(parent + sub, 4), "cost_unknown": False,
            "terra_spend_usd": round(parent, 4),
            "luna_spend_usd": round(sub, 4)}
    try:
        _bind_review_identity(repo, pr, base, head, out["run_dir"],
                              delivery_id=delivery_id)
    except Exception as exc:
        return {"status": "failed", "stage": "identity",
                "error": f"identity binding refused: {exc}",
                "mode": out.get("mode", "terra-only-primary"),
                "run_dir": out.get("run_dir"),
                "terra_findings": 0, "added": 0,
                "cost_usd": result.get("cost_usd"),
                "cost_unknown": result.get("cost_unknown", True)}
    return _maybe_semdup_stage(
        cfg=cfg, repo=repo, pr=pr, base=base, head=head,
        repo_path=repo_path, runs_root=runs_root,
        run_dir=out.get("run_dir"), result=result, is_stale=is_stale)


def _count_terra(run_dir: str) -> int:
    try:
        shadow = json.loads((Path(run_dir) / "shadow_result.json")
                            .read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(shadow.get("advisory", []) or [])


# -- orchestration ---------------------------------------------------------------

def get_live_head(repo: str, pr_number: int) -> str | None:
    """Current GitHub PR head SHA, or None when unknown.

    PROD-02 freshness oracle: a short gh call, never raising. None
    means "unverifiable" (network/gh failure) — callers proceed with
    the existing behavior and the post-inference check still guards.
    Tests monkeypatch this module attribute with a scripted mapping.
    """
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}",
             "--jq", ".head.sha"],
            capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _is_readiness_rerun(ev: dict, store: store_mod.Store,
                        prior: dict) -> bool:
    """True when a duplicate delivery carries a draft->ready transition.

    The poller keys delivery IDs by (repo, PR, head), so the
    mark-ready tick for an unchanged head repeats the deferred
    skipped:draft delivery ID. Replay-blocking it would deadlock
    draft->ready forever. Only this narrow shape re-roots: a
    pull_request event that is no longer a draft, for a checkpoint
    still draft-deferred with no reviewed head, superseding a
    skipped:draft row (directly, or via a skipped:replay wrapper that
    preserved the original decision in its detail).
    """
    dec = str(prior.get("decision") or "")
    if dec == "skipped:replay":
        try:
            detail = json.loads(prior.get("detail") or "{}")
        except Exception:
            detail = {}
        dec = str(detail.get("original_decision") or "")
    if dec != "skipped:draft":
        return False
    if ev.get("event") != "pull_request" or not ev.get("pr_number"):
        return False
    if ev.get("draft") is not False and ev.get("action") != \
            "ready_for_review":
        return False
    try:
        cp = store.checkpoint_get(ev["repo"], int(ev["pr_number"]))
    except Exception:
        return False
    return (cp is not None and cp.get("status") == "draft"
            and not cp.get("last_reviewed_head"))


def _prior_retry_attempts(prior: dict) -> int:
    """Recorded budget-retry attempts for a delivery row (0 when none)."""
    try:
        detail = json.loads(prior.get("detail") or "{}")
    except Exception:
        return 0
    try:
        return int(detail.get("budget_retry_attempts") or 0)
    except Exception:
        return 0


def _ceiling_original_decision(prior: dict | None) -> str | None:
    """The refusal a delivery row records, unwrapping replay wrappers.

    Returns "skipped:ceiling" only for a pure ceiling refusal, either
    directly or via a skipped:replay wrapper preserving it as the
    original decision; None for every other shape.
    """
    dec, _, _ = _original_outcome(prior)
    return dec if dec == "skipped:ceiling" else None


def _prior_carries_spend(prior: dict) -> bool:
    """True when a delivery row has non-zero spend attached."""
    try:
        return abs(float(prior.get("cost_usd") or 0.0)) > 0.0
    except Exception:
        return True


def _same_generation(ev: dict, prior: dict) -> bool:
    """True when the event names the exact refused (repo, PR, head)."""
    head = ev.get("head_sha") or ""
    if not head or head != (prior.get("head_sha") or ""):
        return False
    if str(ev.get("repo") or "") != str(prior.get("repo") or ""):
        return False
    try:
        return int(ev.get("pr_number") or 0) == int(prior.get("pr") or 0)
    except Exception:
        return False


def _head_unreviewed(store: store_mod.Store, ev: dict, head: str) -> bool:
    """True when no review has since covered the head.

    A checkpoint last_reviewed_head match — or a pinned claim for the
    head, still owned by its claimant — blocks the retry so the same
    head is never reviewed twice. The checkpoint pointer alone is not
    enough: a later review of a newer head advances the pointer, so a
    successful review of THIS head is checked directly in the reviews
    history. A live reset to an already-reviewed head therefore stays
    a blocked replay.
    """
    try:
        cp = store.checkpoint_get(ev["repo"], int(ev["pr_number"])) or {}
    except Exception:
        return False
    if (cp.get("last_reviewed_head") or "") == head:
        return False
    if (cp.get("status") or "") == "in_progress" and \
            (cp.get("in_progress_head") or "") == head:
        return False
    try:
        hit = store.cx.execute(
            "SELECT 1 FROM reviews WHERE repo=? AND pr=?"
            " AND head_sha=? AND status IN ('complete','inconclusive')"
            " LIMIT 1",
            (ev["repo"], int(ev["pr_number"]), head)).fetchone()
    except Exception:
        return False
    return not bool(hit)


def _head_still_live(ev: dict, head: str) -> bool:
    """True when the live PR head positively confirms the head.

    Unverifiable (gh unreachable or lookup error) fails closed: the
    refusal is retried on a later tick, never on a guess.
    """
    try:
        live = get_live_head(ev["repo"], int(ev["pr_number"] or 0))
    except Exception:
        return False
    return bool(live) and live == head


def _is_budget_retry(ev: dict, store: store_mod.Store,
                     prior: dict) -> bool:
    """True when a duplicate delivery may re-drive a budget refusal.

    Narrow escape from replay protection, mirroring
    _is_readiness_rerun: a pure skipped:ceiling row (directly, or via
    a skipped:replay wrapper preserving it as the original decision)
    for the same (repo, PR, head) may be processed as new work when —
    and only when — every bound holds:

    - the refusal carried no spend (spend-carrying rows are never
      touched);
    - the re-ingested event names the exact refused head;
    - no review has since covered that head (checkpoint, pinned
      claim, or the reviews history itself — so a later review that
      advanced the checkpoint pointer does not re-arm an old
      refusal);
    - the live PR head positively confirms the refused head is still
      current;
    - fewer than BUDGET_RETRY_MAX_ATTEMPTS re-drives recorded.

    All spend caps are re-checked by the normal flow after re-rooting;
    this predicate authorizes no inference by itself.
    """
    head = ev.get("head_sha") or ""
    return (bool(head)
            and _ceiling_original_decision(prior) == "skipped:ceiling"
            and not _prior_carries_spend(prior)
            and _same_generation(ev, prior)
            and _prior_retry_attempts(prior) < BUDGET_RETRY_MAX_ATTEMPTS
            and _head_unreviewed(store, ev, head)
            and _head_still_live(ev, head))


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
                       reason: str, cost: float | None,
                       now: float | None = None,
                       detail: dict | None = None,
                       ack_gh: Any = None,
                       live_head: str = "") -> dict:
    store.delivery_update_decision(ev["delivery_id"], decision, reason,
                                   cost, now=now, detail=detail)
    row = store.delivery_get(ev["delivery_id"]) or {}
    # CODATIEL-ACK-01: terminal decisions update the PR ack comment
    # (best-effort, never raises, never affects the stored row).
    if ev.get("pr_number"):
        if ack_gh is None:
            ack_gh = ev.get("_ack_gh")
        _ack_for_terminal(store, ev, decision, reason, detail,
                          ack_gh=ack_gh, live_head=live_head)
    return row

def _provenance_detail(*, repo: str, pr: int, base: str, head: str,
                      result: dict | None, kind: str | None,
                      info: dict | None, superseded: bool) -> dict:
    """Minimal cost provenance for Gate D (no second ledger).

    Retains repo/pr/base/head, run identity, timestamps, stage
    costs, and pair counts. Costs that are unavailable are omitted
    (marked unavailable by the caller, never fabricated).
    """
    detail: dict[str, Any] = {
        "repo": repo, "pr": pr, "base_sha": base, "head_sha": head,
        "kind": kind or "", "superseded": bool(superseded),
        "provenance_version": "cost01b-v1",
    }
    if info is not None:
        detail["diff_sha"] = info.get("diff_sha")
        detail["files"] = info.get("files")
    if result is not None:
        run_dir = result.get("run_dir") or ""
        if run_dir:
            detail["run_dir"] = run_dir
            try:
                run_json = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
                if run_json.get("run_id"):
                    detail["run_id"] = str(run_json["run_id"])
            except Exception:
                pass
        detail["total_cost_usd"] = result.get("cost_usd")
        detail["cost_unknown"] = bool(result.get("cost_unknown"))
        detail["terra_findings"] = result.get("terra_findings")
        detail["luna_added"] = result.get("added")
        detail["semdup_status"] = result.get("semdup_status")
        # Semdup spend from the stage's persisted priced-ledger results
        # (result fields, written by the stage at completion). None =
        # unknown and is omitted, never represented as confirmed $0.
        if result.get("semdup_spend_usd") is not None:
            detail["semdup_spend_usd"] = result.get("semdup_spend_usd")
        if result.get("semdup_spend_unknown") is not None:
            detail["semdup_spend_unknown"] = result.get("semdup_spend_unknown")
        detail["semdup_published"] = result.get("semdup_published")
        # Explicit Luna/Terra stage spend (Gate D)
        if result.get("terra_spend_usd") is not None:
            detail["terra_spend_usd"] = result.get("terra_spend_usd")
        if result.get("luna_spend_usd") is not None:
            detail["luna_spend_usd"] = result.get("luna_spend_usd")
        # Pair counts: prefer semdup.json when available (authoritative)
        try:
            if run_dir:
                sd = json.loads((Path(run_dir) / "semdup.json").read_text(encoding="utf-8"))
                detail["pairs_examined"] = sd.get("pairs_examined")
                detail["pairs_reused"] = sd.get("pairs_reused")
                # cost01b new field
                if "pairs_cache_hits" in sd:
                    detail["pairs_cache_hits"] = sd.get("pairs_cache_hits")
                detail["pairs_total"] = sd.get("pairs_total")
                detail["semdup_wall_s"] = sd.get("wall_s")
                # Semdup per-model spend from the stage's persisted
                # priced-ledger results (semdup.json, written at stage
                # completion). None = unknown and is omitted, never $0.
                for side in ("terra", "luna"):
                    if sd.get(f"spend_{side}_usd") is not None:
                        detail[f"semdup_{side}_spend_usd"] = sd.get(f"spend_{side}_usd")
                    if sd.get(f"spend_{side}_unknown") is not None:
                        detail[f"semdup_{side}_spend_unknown"] = sd.get(f"spend_{side}_unknown")
        except Exception:
            # Fallback to result fields when semdup.json not yet written.
            # Reuse counts stay visible even on cache-only reviews.
            # Result keys are read directly (they already carry the
            # semdup_ prefix as written by _maybe_semdup_stage).
            for _rk, _dk in (("semdup_pairs_examined", "pairs_examined"),
                             ("semdup_pairs_reused", "pairs_reused"),
                             ("semdup_pairs_cache_hits", "pairs_cache_hits"),
                             ("semdup_pairs_total", "pairs_total"),
                             ("semdup_terra_spend_usd",
                              "semdup_terra_spend_usd"),
                             ("semdup_luna_spend_usd",
                              "semdup_luna_spend_usd"),
                             ("semdup_terra_spend_unknown",
                              "semdup_terra_spend_unknown"),
                             ("semdup_luna_spend_unknown",
                              "semdup_luna_spend_unknown"),
                             ("semdup_spend_unknown",
                              "semdup_spend_unknown")):
                if result.get(_rk) is not None:
                    detail[_dk] = result.get(_rk)
        # Fallback Terra spend from metrics.json when not in result
        if "terra_spend_usd" not in detail:
            try:
                if run_dir:
                    m = json.loads((Path(run_dir) / "metrics.json").read_text(encoding="utf-8"))
                    spend = (m.get("spend") or {})
                    if spend.get("usd_estimate") is not None:
                        detail["terra_spend_usd"] = spend.get("usd_estimate")
            except Exception:
                pass
    detail["finished_at"] = time.time()
    # Strip None values for compactness
    return {k: v for k, v in detail.items() if v is not None}


def _recommendation_fingerprint(row: dict) -> str:
    """Deterministic fingerprint of the material recommendation.

    Exact hash of (decision + revised_comment), never an LLM similarity
    check: the same code pair with a different recommended comment is a
    different finding and must not be suppressed by an earlier delivery.
    """
    decision = str(row.get("decision") or "")
    comment = str(row.get("revised_comment") or "")
    return hashlib.sha256(
        f"{decision}|{comment}".encode("utf-8")).hexdigest()[:16]


def _finding_hash_from_row(row: dict) -> str:
    """Stable hash for a finding: code anchors + recommendation.

    Line-independent (no line numbers). Two rows on the same code pair
    with different recommended text hash differently, so a revised
    recommendation is delivered rather than wrongly suppressed.
    """
    a = row.get("anchors") or {}
    t = a.get("target") or {}
    c = a.get("counterpart") or {}
    # Content hashes + paths are the code identity (line-independent);
    # the recommendation fingerprint distinguishes materially different
    # suggestions on the same pair.
    rec = _recommendation_fingerprint(row)
    raw = (f"{t.get('path')}|{t.get('sha256')}|{c.get('path')}|"
           f"{c.get('sha256')}|{rec}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _filter_already_delivered(store, repo: str, pr: int, published: list, channel: str) -> tuple[list, list]:
    """Split published into (to_deliver, already_delivered) per channel."""
    to_deliver: list = []
    already: list = []
    for row in published or []:
        try:
            fh = _finding_hash_from_row(row)
            if fh and store.is_finding_delivered(repo, pr, fh, channel):
                already.append(row)
            else:
                to_deliver.append(row)
        except Exception:
            to_deliver.append(row)
    return to_deliver, already


def _record_finding_deliveries(store, repo: str, pr: int, published: list, channel: str, head_sha: str, run_id: str, outcome: str) -> None:
    """Record each finding hash as delivered to channel."""
    for row in published or []:
        try:
            fh = _finding_hash_from_row(row)
            if fh:
                store.finding_delivery_record(repo=repo, pr=pr, finding_hash=fh, channel=channel, head_sha=head_sha, run_id=run_id, outcome=outcome)
        except Exception:
            continue


def _github_delivery_posted(store, repo: str, pr: int, head_sha: str, run_id: str) -> bool:
    """Structured publication outcome (not message substring).

    Returns True only when the publications row for (repo, PR, head,
    run) has outcome == "posted". A refused/failed row whose reason
    text mentions "posted" (e.g. "comment post failed (not posted)")
    returns False, so no finding_deliveries receipt is created.
    """
    try:
        if not run_id:
            return False
        row = store.publication_get(repo, pr, head_sha, run_id)
        return bool(row and row.get("outcome") == "posted")
    except Exception:
        return False


def process_event(*, cfg: EventConfig, store: store_mod.Store,
                  ev: dict, model_factory: Any = None,
                  transport: Any = None, ack_gh: Any = None) -> dict:
    """Process one normalized event. Returns the stored delivery row.

    ``ack_gh`` is the injectable GitHub transport for the early
    acknowledgement comment (tests pass fakes; None shells out to the
    gh CLI and stays silent unless comments are switched on+armed).
    """
    now = cfg.clock()
    if ack_gh is not None:
        # CODATIEL-ACK-01: carry the injectable ack transport on the
        # event so every terminal decision in this call tree (including
        # queued drains) acknowledges through it. Production passes
        # None and the real gh CLI applies under the HOLD gates.
        ev = dict(ev, _ack_gh=ack_gh)
    store.require_enabled()
    prior = store.delivery_get(ev["delivery_id"])
    if prior is not None and prior.get("decision") not in ("in_progress",):
        if _is_readiness_rerun(ev, store, prior):
            # Draft->ready readiness transition re-ingested under the
            # same poller delivery ID (same head, draft now false): the
            # deferred skipped:draft row is superseded and the event is
            # processed as new work. Every other repeat stays a blocked
            # replay with no inference.
            store.delivery_reopen_draft(ev["delivery_id"], now=now)
            prior = None
        else:
            # Webhook retry / worker restart: the stored decision stands
            # and the duplicate is recorded as a blocked replay.
            # No inference. Two narrow escapes re-root instead: the
            # draft readiness transition, and a bounded budget retry of
            # a pure ceiling refusal (same head, still live, budget
            # re-checked downstream).
            if _is_budget_retry(ev, store, prior):
                attempt = _prior_retry_attempts(prior) + 1
                reopened, won = store.delivery_reopen_budget(
                    ev["delivery_id"], attempt=attempt,
                    max_attempts=BUDGET_RETRY_MAX_ATTEMPTS, now=now)
                if won and (reopened or {}).get("decision") == \
                        "in_progress":
                    prior = None
                else:
                    # Lost the atomic reclaim (or the row moved under
                    # us): fall back to replay, never process as new
                    # work. The replay mark cannot overwrite the
                    # winner's active claim.
                    _reconcile_ack_on_replay(
                        store, ev, prior, ack_gh=ev.get("_ack_gh"))
                    return store.delivery_mark_replay(
                        ev["delivery_id"], prior)
            else:
                # CODATIEL-ACK-01: a replayed terminal delivery retries
                # its acknowledgement from the stored outcome (a failed
                # PATCH converges here). No inference, no state change.
                _reconcile_ack_on_replay(
                    store, ev, prior, ack_gh=ev.get("_ack_gh"))
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
            "identity (no inference, no spend)", None, now=now)
    decision, reason, ctx = classify_event(
        ev, cp, store.require_enabled())
    if decision.startswith("skipped:") or decision.startswith(
            "needs_attention:"):
        if decision == "skipped:draft" and ev.get("pr_number"):
            _remember_draft(store, ev, cp)
        return _finalize_delivery(store, ev, decision, reason, 0.0, now=now)
    # review path: resolve repo access, compute a safe range, check
    # ceiling, claim atomically, run, seal.
    assert ev.get("pr_number"), "review requires a PR number"
    pr = int(ev["pr_number"])
    access, err = resolve_access(cfg, ev["repo"])
    if access is None:
        return _finalize_delivery(
            store, ev, "needs_attention:repo_access",
            f"cannot access repo checkout: {err}", None, now=now)
    kind = "full" if decision == "review_full" else "incremental"
    from_sha = ctx.get("from") or ev.get("base_sha") or ev.get("before_sha")
    to_sha = ev.get("head_sha")
    if not from_sha or not to_sha:
        return _finalize_delivery(
            store, ev, "needs_attention:identity",
            "review range lacks base/head SHAs; refusing to guess", None, now=now)
    info, cerr = access.compare(from_sha, to_sha,
                                file_limit=cfg.compare_file_limit)
    if info is None:
        tag = cerr.split(":", 1)[0]
        if tag not in ("truncated", "compare_failed", "unknown_endpoint"):
            tag = "compare_failed"
        return _finalize_delivery(
            store, ev, f"needs_attention:{tag}", cerr, None, now=now)
    if info["empty"]:
        return _finalize_delivery(
            store, ev, "skipped:unchanged",
            "safe comparison shows an empty diff; no inference", 0.0, now=now)
    if kind == "incremental" and ctx.get("rebase"):
        anc = access.ancestor(from_sha, to_sha)
        if anc is None:
            return _finalize_delivery(
                store, ev, "needs_attention:compare_failed",
                "ancestry indeterminable after base change; refusing to "
                "review an unverifiable range", None, now=now)
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
            f"event recorded; a later tick may bounded-retry this exact "
            f"head once budget is available (same head, live-head "
            f"confirmed, max {BUDGET_RETRY_MAX_ATTEMPTS} attempts)", 0.0, now=now)
    live = get_live_head(ev["repo"], pr)
    if live is not None and live != to_sha:
        # PROD-02: the event's head is no longer the PR's head (older
        # poll/event, or a stale queued drain). Reviewing it would redo
        # a superseded range and email misleading "regressions". $0, no
        # checkpoint mutation, no email, no comment. A legitimate
        # force-push/rebase whose head IS live still reviews (equality
        # only; ancestry is irrelevant).
        return _finalize_delivery(
            store, ev, "skipped:stale_head",
            f"event head {to_sha[:12]} is not the live PR head "
            f"{live[:12]}; superseded event dropped with no "
            "inference, no email, no checkpoint change", 0.0, now=now,
            live_head=live)
    if live is None and cp is not None:
        # gh unreachable: fall back to local ancestry (PROD-02 review).
        # A head older than the reviewed head is stale even when the
        # live head cannot be confirmed; anything else still reviews
        # (a genuine backwards reset is re-driven when gh recovers, or
        # via the explicit review op).
        reviewed = cp.get("last_reviewed_head") or ""
        if reviewed and to_sha != reviewed:
            try:
                back = access.ancestor(to_sha, reviewed)
            except Exception:
                back = None
            if back is True:
                return _finalize_delivery(
                    store, ev, "skipped:stale_head",
                    f"event head {to_sha[:12]} is an ancestor of the "
                    f"reviewed head {reviewed[:12]} and the live head "
                    "is unverifiable; superseded event dropped with "
                    "no inference, no email, no checkpoint change",
                    0.0, now=now)
    claim = store.claim_for_review(ev["repo"], pr, base=from_sha,
                                   head=to_sha, now=now)
    if claim == "queued":
        return _finalize_delivery(
            store, ev, "queued",
            "a review is pinned and running; newer delta queued as a "
            "separate run (evidence never mixed)", 0.0, now=now)
    if claim == "already":
        return _finalize_delivery(
            store, ev, "skipped:unchanged",
            "identical pinned review already in flight; no second spend",
            0.0, now=now)
    # Generation token minted at claim time distinguishes a same-range
    # replacement (recovery + reclaim with identical base/head) without
    # a concurrency framework. Single-worker deployment makes this a
    # hypothetical discriminator; owned check fails closed if the token
    # no longer matches.
    _claimed_auto = (store.checkpoint_get(ev["repo"], pr)
                     if ev.get("pr_number") else None)
    _claimed_auto_run = (_claimed_auto or {}).get("in_progress_run") or ""
    _claimed_auto_base = (_claimed_auto or {}).get("in_progress_base") or ""
    # RESERVE-RECOVERY-01: the reservation is attributed to the claim's
    # generation token, so a later recovery can settle or release
    # exactly this claim's hold (never another worker's).
    store.reserve(cfg.reserve_usd, holder=_claimed_auto_run,
                  repo=ev["repo"], pr=pr, head_sha=to_sha,
                  delivery_id=ev.get("delivery_id") or "")
    # CODATIEL-ACK-01: the claim is atomic and paid inference has not
    # started — this is the early visible acknowledgement, pinned to
    # the exact head under review.
    request_ack(store=store, repo=ev["repo"], pr=pr, head=to_sha,
                state="in_progress",
                detail=(f"claim pinned {from_sha[:12]}..{to_sha[:12]}; "
                        f"{kind} review starting."),
                ack_gh=ack_gh)
    # COST-01B: persistent stale observation latch (CodeRabbit)
    _stale_observed: list[str] = []

    def _is_stale() -> bool:
        try:
            live = get_live_head(ev["repo"], pr)
            stale = live is not None and live != to_sha
            if stale:
                _stale_observed.append(live)
            return stale
        except Exception:
            return False

    result = run_review_for_range(
        cfg=cfg, repo=ev["repo"], pr=pr, base=from_sha, head=to_sha,
        kind=kind, repo_path=access.path,
        runs_root=cfg.state_dir / "runs", work_root=cfg.state_dir / "work",
        model_factory=model_factory, is_stale=_is_stale,
        delivery_id=ev.get("delivery_id"))
    cost = result.get("cost_usd")
    unknown = bool(result.get("cost_unknown")) or cost is None
    store.settle(cfg.reserve_usd, None if unknown else float(cost or 0.0),
                 holder=_claimed_auto_run)
    cp_now = (store.checkpoint_get(ev["repo"], pr)
              if ev.get("pr_number") else None)
    owned = bool(
        cp_now and cp_now.get("status") == "in_progress"
        and cp_now.get("in_progress_head") == to_sha
        and cp_now.get("in_progress_base") == _claimed_auto_base
        and (cp_now.get("in_progress_run") or "") == _claimed_auto_run)
    live_after = (get_live_head(ev["repo"], pr)
                  if ev.get("pr_number") else None)
    # Latch: earlier positive stale observation stands when final lookup is unknown
    stale_unresolved = live_after is None and bool(_stale_observed)
    if not owned or stale_unresolved or (live_after is not None and live_after != to_sha):
        # PROD-02: the head went stale while inference ran (or another
        # run owns the checkpoint). This run must not seal, must not
        # email a "current-review" digest, must not publish, and must
        # not overwrite the newer checkpoint. Spend is real: recorded
        # on the delivery. The claim is released, not sealed.
        if owned:
            prior = (cp.get("status") if isinstance(cp, dict) else None)
            store.release_claim(
                ev["repo"], pr,
                "run superseded mid-flight; claim released, checkpoint "
                "left to the newer review",
                base=from_sha, head=to_sha,
                prior_status=(prior if isinstance(prior, str)
                              else None),
                claim_run=_claimed_auto_run)
        prov = _provenance_detail(
            repo=ev["repo"], pr=pr, base=from_sha, head=to_sha,
            result=result, kind=kind, info=info, superseded=True)
        final = _finalize_delivery(
            store, ev, "skipped:stale_superseded",
            f"head {to_sha[:12]} went stale during inference "
            f"(live {(live_after or '?')[:12]}, "
            f"{'claim lost' if not owned else 'claim released'}); "
            "no checkpoint change, no email, no comment; "
            f"inference cost ${float(cost or 0.0):.4f} recorded",
            cost if not unknown else None, now=cfg.clock(), detail=prov,
            live_head=(live_after or ""))
        # A newer head queued during this run must not strand: drain
        # it through the same verified path (stale queues drop $0).
        # The follow range starts at the queued BASE (not this run's
        # unsealed head), so a first-review supersede cannot silently
        # skip the A..H1 segment.
        cp_after = (store.checkpoint_get(ev["repo"], pr)
                    if ev.get("pr_number") else None)
        qh_after = (cp_after or {}).get("queued_head") or ""
        if qh_after:
            qb_after = (cp_after or {}).get("queued_base") or ""
            final = _drain_queued(
                cfg=cfg, store=store, ev=ev, pr=pr,
                to_sha=(qb_after or to_sha),
                queued=(qb_after, qh_after), final=final, access=access,
                model_factory=model_factory, transport=transport, now=now,
                ack_gh=ack_gh)
        return final
    if result["status"] not in ("complete", "inconclusive"):
        _remember_queued(store, ev, cp, from_sha, to_sha, "failed")
        store.review_record(
            repo=ev["repo"], pr=pr, kind=kind, base_sha=from_sha,
            head_sha=to_sha, diff_sha=info["diff_sha"], files=info["files"],
            pipeline=cfg.pipeline, status="failed",
            terra_findings=0, luna_added=0, cost_usd=cost,
            cost_unknown=unknown, run_dir=result.get("run_dir") or "")
        prov = _provenance_detail(
            repo=ev["repo"], pr=pr, base=from_sha, head=to_sha,
            result=result, kind=kind, info=info, superseded=False)
        return _finalize_delivery(
            store, ev, "failed",
            f"primary review failed ({result.get('stage') or 'unknown'}: "
            f"{(result.get('error') or '')[:200]}); checkpoint not "
            f"advanced, Luna never spent", cost, now=now, detail=prov)
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
    # COST-01B 1.1: per-finding delivery deduplication (separate channels)
    run_dir = result.get("run_dir") or ""
    # Load original published list for filtering
    _orig_semdup = {}
    try:
        if run_dir:
            _orig_semdup = json.loads((Path(run_dir) / "semdup.json").read_text(encoding="utf-8"))
    except Exception:
        _orig_semdup = {}
    _orig_published = list(_orig_semdup.get("published") or [])

    # Filter for email channel before notify
    _email_to_deliver = _orig_published
    _email_already = []
    if _orig_published:
        try:
            _email_to_deliver, _email_already = _filter_already_delivered(store, ev["repo"], pr, _orig_published, "email")
            if len(_email_to_deliver) != len(_orig_published):
                # Temporarily write filtered published for email digest
                _tmp = dict(_orig_semdup)
                _tmp["published"] = _email_to_deliver
                # Keep suppressed as is, but add already-delivered to suppressed for audit
                _tmp["suppressed"] = list(_orig_semdup.get("suppressed") or [])
                for r in _email_already:
                    _tmp["suppressed"].append({**r, "reason": "already_delivered_email", "detail": "finding already delivered via email on this PR"})
                _safe = _safe_run_dir(run_dir, cfg.state_dir)
                if _safe is not None:
                    (_safe / "semdup.json").write_text(json.dumps(_tmp, indent=1), encoding="utf-8")
        except Exception:
            pass
    notified, notify_detail = _notify(cfg, run_dir or "",
                        result["status"], transport,
                        context=(f"{cfg.env}:{ev['repo']}#{pr}"
                                 if cfg.env == "dev" else None))
    # Record email deliveries for actually delivered findings
    if notified:
        try:
            _run_id = ""
            try:
                _run_id = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8")).get("run_id") or ""
            except Exception:
                pass
            _record_finding_deliveries(store, ev["repo"], pr, _email_to_deliver, "email", to_sha, _run_id, "posted")
        except Exception:
            pass
    # Restore original for GitHub filtering (email and github must not be conflated)
    try:
        if run_dir and _orig_semdup:
            _safe2 = _safe_run_dir(run_dir, cfg.state_dir)
            if _safe2 is not None:
                (_safe2 / "semdup.json").write_text(json.dumps(_orig_semdup, indent=1), encoding="utf-8")
    except Exception:
        pass
    # Filter for GitHub channel before publish
    _github_to_deliver = _orig_published
    _github_already = []
    if _orig_published:
        try:
            _github_to_deliver, _github_already = _filter_already_delivered(store, ev["repo"], pr, _orig_published, "github")
            if len(_github_to_deliver) != len(_orig_published):
                _tmp2 = dict(_orig_semdup)
                _tmp2["published"] = _github_to_deliver
                _tmp2["suppressed"] = list(_orig_semdup.get("suppressed") or [])
                for r in _github_already:
                    _tmp2["suppressed"].append({**r, "reason": "already_delivered_github", "detail": "finding already delivered via GitHub on this PR"})
                _safe3 = _safe_run_dir(run_dir, cfg.state_dir)
                if _safe3 is not None:
                    (_safe3 / "semdup.json").write_text(json.dumps(_tmp2, indent=1), encoding="utf-8")
        except Exception:
            pass
    semdup_bits = ""
    if result.get("semdup_status", "off") != "off":
        # Report actually delivered count, not just approved
        _delivered_count = len(_github_to_deliver) if _orig_published else result.get("semdup_published", 0)
        semdup_bits = (f", semdup {result.get('semdup_status')}/{_delivered_count}")
    # GitHub comment publisher (PROD-01 Track A): email already
    # attempted above and its outcome recorded; publishing runs second
    # in try/except and can never change the email outcome. Default
    # HOLD (CODATIEL_GITHUB_COMMENTS=off) makes this a no-op.
    publish_bits = _maybe_publish_comment(
        cfg=cfg, store=store, run_dir=run_dir or "",
        now=now)
    # Record GitHub deliveries only on structured posted outcome (not substring).
    # _github_delivery_posted checks publications.outcome == "posted"; a
    # failure message mentioning "posted" never counts.
    try:
        _run_id2 = ""
        try:
            _run_id2 = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8")).get("run_id") or ""
        except Exception:
            pass
        _github_posted = _github_delivery_posted(store, ev["repo"], pr, to_sha, _run_id2)
    except Exception:
        _run_id2 = ""
        _github_posted = False
    if _github_posted:
        try:
            _record_finding_deliveries(store, ev["repo"], pr, _github_to_deliver, "github", to_sha, _run_id2, "posted")
        except Exception:
            pass
    # Restore original semdup.json for provenance (keep approved list intact)
    try:
        if run_dir and _orig_semdup:
            _safe4 = _safe_run_dir(run_dir, cfg.state_dir)
            if _safe4 is not None:
                (_safe4 / "semdup.json").write_text(json.dumps(_orig_semdup, indent=1), encoding="utf-8")
    except Exception:
        pass
    prov = _provenance_detail(
        repo=ev["repo"], pr=pr, base=from_sha, head=to_sha,
        result=result, kind=kind, info=info, superseded=False)
    final = _finalize_delivery(
        store, ev, "completed",
        f"{kind} review {result['status']} "
        f"(terra {result.get('terra_findings', 0)}, "
        f"luna-exclusive {result.get('added', 0)}{semdup_bits}; "
        f"notified={notified}"
        + ("" if notified else f" ({notify_detail[:160]})")
        + publish_bits + ")", cost, now=now, detail=prov)
    # Drain a queued newer delta as its own run (separate evidence).
    if done.get("queued"):
        return _drain_queued(cfg=cfg, store=store, ev=ev, pr=pr,
                             to_sha=to_sha, queued=done["queued"],
                             final=final, access=access,
                             model_factory=model_factory,
                             transport=transport, now=now,
                             ack_gh=ack_gh)
    return final


def _drain_queued(*, cfg, store, ev, pr, to_sha, queued, final,
                  access, model_factory=None, transport=None, now=None,
                  ack_gh=None):
    """Run a queued delta after its parent sealed (or was superseded).

    PROD-02: the queued head is verified before spending — against the
    live GitHub head when reachable, else local ancestry. Stale queues
    are dropped $0 and cleared, never drained backwards. Shared by the
    seal path and the mid-flight-superseded path so a stranded queue
    cannot wait on an unrelated future event.
    """
    qb, qh = queued
    live_q = get_live_head(ev["repo"], pr)
    stale_q = live_q is not None and live_q != qh
    if not stale_q and live_q is None and qh != to_sha:
        # gh unreachable: local ancestry fallback (PROD-02 review).
        try:
            back_q = access.ancestor(qh, to_sha)
        except Exception:
            back_q = None
        stale_q = back_q is True
    if stale_q:
        # PROD-02: the queued head is stale against live GitHub
        # (older event, or a residue that never matched a push).
        # Drop it with $0: no inference, no email, no comment.
        # The stale queue entry is cleared so it can never drain
        # backwards later. Identity-compared: a newer queued head
        # arriving during verification survives.
        store.clear_queued(ev["repo"], pr, base=qb, head=qh)
        follow = dict(ev, delivery_id=ev["delivery_id"] + ":drain",
                      base_sha=to_sha, head_sha=qh, before_sha=to_sha,
                      action="synchronize")
        _record(store, follow, "skipped:stale_head",
                f"queued head {qh[:12]} is stale "
                f"({('not the live PR head ' + live_q[:12]) if live_q is not None else 'an ancestor of the just-reviewed head, live head unverifiable'}); "
                "stale queue dropped and cleared, "
                "no inference, no email", 0.0, now=now)
        return final
    follow = dict(ev, delivery_id=ev["delivery_id"] + ":drain",
                  base_sha=to_sha, head_sha=qh, before_sha=to_sha,
                  action="synchronize")
    sub = store.delivery_get(follow["delivery_id"])
    if sub is None:
        _record(store, follow, "in_progress", "queued-drain", None,
                now=now)
        out = process_event(cfg=cfg, store=store, ev=follow,
                            model_factory=model_factory,
                            transport=transport, ack_gh=ack_gh)
        final = {**final, "drained": out.get("decision")}
    return final


def _maybe_publish_comment(*, cfg: EventConfig, store: store_mod.Store,
                           run_dir: str, now: float | None = None) -> str:
    """Best-effort GitHub comment post. Returns reason bits, never
    raises, never affects the email outcome recorded by the caller.
    """
    from codatiel_i2.github import comments as comments_mod
    if not comments_mod.is_enabled():
        return ""
    if not run_dir:
        return "; github-comment refused (no run dir)"
    try:
        pub = comments_mod.post_review_comment(
            run_dir=run_dir, store=store, now=now)
    except Exception as exc:  # noqa: BLE001 - publish never blocks email
        return (f"; github-comment error "
                f"({type(exc).__name__}: {exc}"[:160] + ")")
    outcome = str(pub.get("outcome") or "unknown")
    if outcome == "posted":
        return (f"; github-comment posted "
                f"(comment {pub.get('comment_id') or '?'})")
    return f"; github-comment {outcome} ({str(pub.get('reason') or '')[:160]})"


ACK_DECISION_STATE = {
    "skipped:draft": "deferred",
    "skipped:paused": "deferred",
    "skipped:ceiling": "deferred",
    "queued": "queued",
    "skipped:stale_head": "superseded",
    "skipped:stale_superseded": "superseded",
    "completed": "completed",
    "failed": "failed",
}


def stable_ack_detail(decision: str, reason: str) -> str:
    """Tick-stable ack detail: never embeds volatile ledger amounts or
    timestamps, so repeated ticks reconcile to already-current."""
    low = (reason or "").lower()
    if decision == "skipped:draft":
        return "draft PR — held until marked ready for review."
    if decision == "skipped:paused":
        return "operator pause is set — automatic ingest holds."
    if decision == "skipped:ceiling":
        if "daily" in low:
            return ("daily budget exhausted — automatic bounded retry "
                    "when budget frees.")
        return ("spend cap reached — bounded retry when budget is "
                "available.")
    if decision == "queued":
        return ("waiting for the review worker; the review runs as soon "
                "as the worker is free.")
    if decision in ("skipped:stale_head", "skipped:stale_superseded"):
        return ("partial inference cost was recorded; no findings were "
                "published for this head.")
    if decision == "failed":
        return (reason or "review failed")[:160]
    return ""


def request_ack(*, store: store_mod.Store, repo: str, pr: int,
                head: str, state: str, detail: str = "",
                findings_comment_id: str = "",
                live_head: str = "",
                ack_gh: Any = None) -> dict:
    """Best-effort ack comment sync. Returns the outcome dict, never
    raises, never affects review, spend, email, or publication paths.

    A deferred request never fires for a head the checkpoint already
    shows as reviewed or in flight (same-head no-regression rule at
    the source, before any GitHub traffic).
    """
    from codatiel_i2.github import ack as ack_mod
    try:
        if state == "deferred" and _head_has_review(store, repo, pr,
                                                    head):
            return {"outcome": "already-current",
                    "reason": ("head already reviewed or under review; "
                               "deferred ack suppressed"),
                    "repo": repo, "pr": pr, "head_sha": head}
        return ack_mod.sync_ack(
            repo=repo, pr=pr, head=head, state=state, detail=detail,
            findings_comment_id=findings_comment_id,
            live_head=live_head, store=store, gh=ack_gh)
    except Exception as exc:  # noqa: BLE001 - ack never blocks review
        return {"outcome": "error",
                "reason": f"{type(exc).__name__}: {exc}"[:300],
                "repo": repo, "pr": pr, "head_sha": head}


def _head_has_review(store: store_mod.Store, repo: str, pr: int,
                     head: str) -> bool:
    """True when the checkpoint shows this head reviewed or in flight."""
    try:
        cp = store.checkpoint_get(repo, int(pr))
    except Exception:
        return False
    if not isinstance(cp, dict):
        return False
    if (cp.get("last_reviewed_head") or "") == (head or "") and head:
        return True
    return bool(cp.get("status") == "in_progress"
                and (cp.get("in_progress_head") or "") == (head or "")
                and head)


def gate_ack(*, store: store_mod.Store, ev: dict, row: dict | None,
             gate: dict, ack_gh: Any = None) -> dict:
    """Ack for an operator-gate refusal. Never raises.

    The gate refused, but a completed outcome for this head may exist
    (same delivery, another delivery ID, or replay-wrapped): the
    truthful completed state always wins first, so a published review
    is never relabeled as "no review has run". Otherwise the stored
    row's own terminal outcome reconciles, and only then does the
    deferred request go through the usual checkpoint guard in
    request_ack.
    """
    try:
        if _reconcile_cross_delivery(store, ev, ack_gh=ack_gh):
            return {"outcome": "reconciled-stored",
                    "decision": "completed",
                    "repo": ev.get("repo") or "",
                    "pr": ev.get("pr_number"),
                    "head_sha": ev.get("head_sha") or ""}
        hit = _reconcile_stored_row(store, ev, row, gate, ack_gh=ack_gh)
        if hit is not None:
            return hit
        if ev.get("pr_number"):
            return request_ack(
                store=store, repo=ev.get("repo") or "",
                pr=int(ev.get("pr_number") or 0),
                head=ev.get("head_sha") or "", state="deferred",
                detail=stable_ack_detail(
                    gate.get("decision") or "",
                    gate.get("reason") or ""),
                ack_gh=ack_gh)
        return {"outcome": "refused", "reason": "no PR",
                "repo": ev.get("repo") or ""}
    except Exception as exc:  # noqa: BLE001 - ack never blocks ingest
        return {"outcome": "error",
                "reason": f"{type(exc).__name__}: {exc}"[:300],
                "repo": ev.get("repo") or ""}


def _reconcile_stored_row(store: store_mod.Store, ev: dict,
                            row: dict | None, gate: dict,
                            ack_gh: Any = None) -> dict | None:
    """Reconcile the stored row's own terminal outcome, if it differs
    from the gate refusal. Returns the outcome dict, or None when the
    deferred path should proceed. Never raises."""
    try:
        stored, _, _ = _original_outcome(row)
    except Exception:
        return None
    if stored not in ACK_DECISION_STATE:
        return None
    if stored == gate.get("decision"):
        return None
    _reconcile_ack_on_replay(store, ev, row, ack_gh=ack_gh)
    return {"outcome": "reconciled-stored", "decision": stored,
            "repo": ev.get("repo") or "", "pr": ev.get("pr_number"),
            "head_sha": ev.get("head_sha") or ""}


def _reconcile_cross_delivery(store: store_mod.Store, ev: dict,
                                ack_gh: Any = None) -> bool:
    """Reconcile a completed outcome stored under another delivery ID.

    Returns True when a completed row for this head was found and its
    acknowledgement reconciled (the caller reports reconciled-stored).
    Never raises.
    """
    try:
        if not (ev.get("pr_number") and ev.get("head_sha")):
            return False
        completed = store.delivery_get_completed(
            ev.get("repo") or "", int(ev.get("pr_number") or 0),
            ev.get("head_sha") or "")
    except Exception:
        return False
    if completed is None:
        return False
    _ack_for_terminal(store, ev, "completed",
                      str(completed.get("reason") or ""),
                      _original_outcome(completed)[2],
                      ack_gh=ack_gh, live_head="")
    return True


def _original_outcome(row: dict | None) -> tuple[str, str, dict]:
    """Original decision, reason and detail, unwrapping replay wrappers.

    A duplicate delivery stores skipped:replay with the original outcome
    preserved in detail (mirrors _ceiling_original_decision). Returns
    ("", "", {}) when nothing terminal can be recovered.
    """
    row = row or {}
    decision = str(row.get("decision") or "")
    reason = str(row.get("reason") or "")
    raw_detail = row.get("detail") or {}
    if isinstance(raw_detail, str):
        try:
            raw_detail = json.loads(raw_detail)
        except ValueError:
            raw_detail = {}
    detail = raw_detail if isinstance(raw_detail, dict) else {}
    if decision == "skipped:replay":
        decision = str(detail.get("original_decision") or "")
        reason = str(detail.get("original_reason") or "")
    return decision, reason, detail


def explicit_delivery_id(repo: str, number: int, head: str,
                          run_token: str) -> str:
    """Unique delivery ID for an explicit (operator-driven) review.

    Bound to the claim's generation token, not just (repo, PR, head):
    repeated explicit re-drives of one head must not share a delivery
    ID, or recovery could match the wrong run dir and double-count
    proven spend against a new reservation.
    """
    return f"explicit:{repo}:{int(number)}:{head}:{run_token}"


def _reconcile_ack_on_replay(store: store_mod.Store, ev: dict,
                             prior: dict | None,
                             ack_gh: Any = None) -> None:
    """Retry a terminal acknowledgement from the stored delivery row.

    Replays carry no inference and change no review state; they only
    re-render the stored terminal outcome through the ack path, so a
    PATCH that failed after the decision was stored converges on the
    next duplicate delivery. Replay wrappers are unwrapped to the
    original outcome first. Never raises.
    """
    try:
        decision, reason, detail = _original_outcome(prior)
        if decision not in ACK_DECISION_STATE:
            return
        _ack_for_terminal(store, ev, decision, reason, detail,
                          ack_gh=ack_gh, live_head="")
    except Exception:
        pass


def _ack_for_terminal(store: store_mod.Store, ev: dict, decision: str,
                      reason: str, detail: dict | None,
                      ack_gh: Any = None, live_head: str = "") -> None:
    """Map a terminal delivery decision to an ack state (silent when the
    decision carries no visibility meaning)."""
    state = ACK_DECISION_STATE.get(decision)
    if state is None:
        return
    try:
        pr = int(ev.get("pr_number") or 0)
    except (TypeError, ValueError):
        return
    if not pr:
        return
    findings = ""
    if decision == "completed":
        run_id = (detail or {}).get("run_id") or ""
        try:
            pub = store.publication_get(
                ev.get("repo") or "", pr, ev.get("head_sha") or "",
                str(run_id))
        except Exception:
            pub = None
        findings = str((pub or {}).get("comment_id") or "")
    request_ack(store=store, repo=ev.get("repo") or "", pr=pr,
                head=ev.get("head_sha") or "", state=state,
                detail=stable_ack_detail(decision, reason),
                findings_comment_id=findings, live_head=live_head,
                ack_gh=ack_gh)


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
            transport: Any, context: str | None = None
            ) -> tuple[bool, str]:
    """Private digest to the configured env destination ONLY.

    Refuses anything but the configured TEST/DEV recipient list. No PR
    comments exist anywhere on this path. `transport=None` selects the
    service default (real SMTP2GO delivery); it never means "skip".
    Returns (sent, detail): refusals (including identity refusals)
    return False with the reason, so operators can tell a refusal
    from a missing configuration.
    """
    if not run_dir or not cfg.notify_to:
        return False, "no run dir or no notify recipients configured"
    try:
        from codatiel_i2 import service as service_mod
        receipt = service_mod.deliver_shadow(
            run_dir, to=list(cfg.notify_to), sender=cfg.notify_sender,
            transport=transport, context=context)
        return bool(receipt), "delivered"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# -- operator-approved semdup continuation --------------------------------
def resume_semdup(*, cfg: EventConfig, repo: str, pr: int,
                  base: str, head: str, run_dir: str,
                  ceiling_usd: float, assess_fn=None,
                  edit_fn=None) -> dict:
    """Resume semdup examination for one pinned delta at a new ceiling.

    Reads the completed run's artifacts for identity and prior spend,
    then runs ONLY the semdup stage into a fresh run dir. Completed
    verdicts are reused from the persisted continuation at $0 — no
    inference is ever repeated. Nothing is emailed, no checkpoint or
    ledger changes: the operator delivers and settles explicitly.
    Returns prior/new spend, the preflight upper bound for the work
    still unjudged, and whether any figure is unconfirmed.
    """
    from pathlib import Path as _P
    if cfg.env != "dev":
        return {"status": "failed", "stage": "config",
                "error": "resume-semdup is DEV-only"}
    try:
        ceiling = float(ceiling_usd)
    except (TypeError, ValueError):
        return {"status": "failed", "stage": "config",
                "error": "ceiling must be numeric"}
    if ceiling <= 0:
        return {"status": "failed", "stage": "config",
                "error": "ceiling must be positive"}
    orig = _P(run_dir)
    try:
        prior_sem = json.loads((orig / "semdup.json").read_text(
            encoding="utf-8"))
    except Exception as exc:
        return {"status": "failed", "stage": "identity",
                "error": f"prior semdup.json unreadable: {exc}"}
    if (prior_sem.get("base_rev") != base
            or prior_sem.get("head_rev") != head):
        return {"status": "failed", "stage": "identity",
                "error": "run dir delta does not match --base/--head; "
                         "refusing to mix deltas"}
    repo_path = (cfg.repo_paths or {}).get(repo)
    if not repo_path or not _P(repo_path).is_dir():
        return {"status": "failed", "stage": "repo_access",
                "error": f"no configured checkout for {repo}; set "
                         f"{'CODATIEL_DEV_REPO_PATHS' if cfg.env == 'dev' else 'CODATIEL_TEST_REPO_PATHS'}"}
    slug = repo.replace("/", "__")
    tag = f"ceiling-{ceiling:.4f}".rstrip("0").rstrip(".")
    dest = (cfg.state_dir / "semdup" / "resumed" / slug / str(pr) /
            f"{base}__{head}" / tag)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        from codatiel_i2.semdup_dev import stage as stage_mod
        sem = stage_mod.run_stage(
            repo_path=_P(repo_path), repo=repo, pr=pr, base_rev=base,
            head_rev=head, run_dir=dest, state_dir=cfg.state_dir,
            max_pairs=(cfg.semdup_max_pairs or None),
            ceiling_usd=ceiling,
            allow_embeddings=cfg.semdup_allow_embeddings,
            assess_fn=assess_fn, edit_fn=edit_fn)
    except Exception as exc:  # noqa: BLE001 - run_stage never raises
        return {"status": "failed", "stage": "semdup",
                "error": f"{type(exc).__name__}: {exc}"}
    prior_spend = prior_sem.get("spend_usd", 0.0)
    new_spend = sem.get("spend_usd", 0.0)
    unconfirmed = bool(prior_sem.get("spend_unknown")
                       or sem.get("spend_unknown"))
    return {"status": "ok", "run_dir": str(dest),
            "prior_spend_usd": prior_spend,
            "new_spend_usd": new_spend,
            "preflight_remaining_usd": sem.get("preflight_usd"),
            "spend_unknown": unconfirmed,
            "semdup_status": sem.get("status"),
            "coverage": sem.get("coverage"),
            "pairs_reused": sem.get("pairs_reused", 0),
            "published": len(sem.get("published", [])),
            "reason": str(sem.get("reason", ""))[:300]}


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
    recovered = recover_reservations(cfg, store, now=now)
    return {"status": "ok", "interrupted": marked,
            "reservations_recovered": recovered}


def _proven_spend(run_dir: Path | str | None) -> tuple[float, bool]:
    """Priced spend proven by a run dir, and whether it ever started.

    Proven = priced Terra ledger lines + priced semdup stage total +
    priced Luna sweep lines. Luna sink rows are raw usage records, so
    they restore through a ledger (corrections applied in order, last
    row per response wins) and price at the current verified Luna
    rates — the same semantics as a live ledger; if rates are
    unavailable the rows stay unproven (conservative). Started = the
    run dir exists (created before paid inference). Anything unpriced
    stays uncertain and is handled by the caller as an unknown hold,
    never as $0.
    """
    if not run_dir:
        return 0.0, False
    root = Path(run_dir)
    try:
        if not root.is_dir():
            return 0.0, False
    except Exception:
        return 0.0, False
    proven = 0.0
    try:
        metrics = json.loads(
            (root / "metrics.json").read_text(encoding="utf-8"))
        for row in metrics.get("spend", {}).get("request_ledger", []):
            if isinstance(row, dict):
                proven += float(row.get("priced_cost_usd") or 0.0)
    except Exception:
        pass
    try:
        semdup = json.loads(
            (root / "semdup.json").read_text(encoding="utf-8"))
        proven += float(semdup.get("spend_usd") or 0.0)
    except Exception:
        pass
    proven += _proven_luna_sink(root)
    return round(proven, 6), True


def _proven_luna_sink(run_dir: Path) -> float:
    """Price restored Luna sink rows at current verified Luna rates."""
    try:
        from codatiel_i2 import providers as providers_mod
        from codatiel_i2.core import spend as spend_mod
        resolved = providers_mod.resolve_backend("luna")
        ledger = spend_mod.SpendLedger(
            rate=resolved.get("rate_usd_per_mtok") or {},
            rate_status=resolved.get("rate_status") or "",
            ceiling_usd=float("inf"))
    except Exception:
        return 0.0
    try:
        ledger.restore_sink(run_dir / "spend_luna.jsonl")
    except Exception:
        return 0.0
    try:
        record = ledger.to_record()
    except Exception:
        return 0.0
    total = 0.0
    for row in record.get("request_ledger", []):
        if isinstance(row, dict):
            try:
                total += float(row.get("priced_cost_usd") or 0.0)
            except (TypeError, ValueError):
                pass
    return total


def _run_created_ts(run_dir: Path) -> float:
    """Run start time from run.json, or -1 when unreadable."""
    try:
        data = json.loads((run_dir / "run.json").read_text(
            encoding="utf-8"))
        created = str(data.get("created_at") or "")
        if created.endswith("Z"):
            created = created[:-1] + "+00:00"
        import datetime as _dt
        return _dt.datetime.fromisoformat(created).timestamp()
    except Exception:
        return -1.0


def _find_run_dir(state_dir: Path, delivery_id: str, *,
                  after_ts: float = 0.0,
                  exclude: set[str] | None = None) -> Path | None:
    """Locate the run dir bound to a delivery ID.

    run.json is written when the run starts (before paid inference),
    so mid-inference kills are locatable. When one delivery drove
    several runs (kills and re-drives), the earliest run created after
    the holder's claim wins; run dirs already consumed by a closed
    recovery never match again.
    """
    if not delivery_id:
        return None
    excluded = exclude or set()
    try:
        runs_root = Path(state_dir) / "runs"
        if not runs_root.is_dir():
            return None
        best: Path | None = None
        best_ts = float("inf")
        for ident in runs_root.rglob("run.json"):
            try:
                data = json.loads(ident.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("delivery_id") != delivery_id:
                continue
            if str(ident.parent) in excluded:
                continue
            ts = _run_created_ts(ident.parent)
            if ts < 0 or ts < after_ts - 60.0:
                continue
            if ts < best_ts:
                best, best_ts = ident.parent, ts
        return best
    except Exception:
        return None
    return None


def recover_reservations(cfg: EventConfig, store: store_mod.Store,
                         now: float | None = None) -> list[str]:
    """Settle or release orphaned reservations, idempotently.

    For each open reservation whose claim can no longer settle (no live
    worker owns its token): settle priced usage proven by the run dir;
    when the run started, preserve the unproven remainder as an
    explicit unknown hold; when it never started, release the hold.
    Each holder closes in the same transaction as its spend update, so
    a repeated recovery is a no-op and settled spend is never
    rewritten or double-counted. A live worker's hold is never
    touched. Per-holder failures are recorded, never raised.

    Concurrency precondition (single worker): the deployed cron
    serializes ticks under flock (inference holds the lock), so no
    second tick can start while a worker runs — a stale holder cannot
    belong to a live worker here. The holder token doubles as an
    atomic fence: a settle arriving after its holder closed fails
    loudly instead of double-spending, and a live checkpoint match
    skips the holder first on every pass. Manual concurrent runs
    outside the flock are operator territory.
    """
    ts = time.time() if now is None else now
    done: list[str] = []
    try:
        holders = store.reservations_open()
    except Exception:
        return done
    try:
        consumed = store.reservations_consumed_run_dirs()
    except Exception:
        consumed = set()
    for h in holders:
        holder = str(h.get("holder") or "")
        if not holder:
            continue
        try:
            if _holder_owned_by_live_worker(store, h, ts,
                                            cfg.stale_after_s):
                continue
            try:
                created = float(h.get("created_ts") or 0.0)
            except (TypeError, ValueError):
                created = 0.0
            run_dir = _find_run_dir(
                cfg.state_dir, str(h.get("delivery_id") or ""),
                after_ts=created, exclude=consumed)
            proven, started = _proven_spend(run_dir)
            out = store.reservation_recover(
                holder, proven_usd=proven, started=started,
                run_dir="" if run_dir is None else str(run_dir))
            if out.get("recovered"):
                if run_dir is not None:
                    consumed.add(str(run_dir))
                done.append(
                    f"{h.get('repo')}#{h.get('pr')} {holder[:8]}"
                    f" proven=${proven:.4f}"
                    f"{' started' if started else ' never-started'}")
        except Exception as exc:  # noqa: BLE001 - one bad holder
            done.append(f"{holder[:8]} error: {type(exc).__name__}")
    return done


def _holder_owned_by_live_worker(store: store_mod.Store, h: dict,
                                 now: float, stale_after_s: float) -> bool:
    """True when a live worker may still own this reservation."""
    try:
        cp = store.checkpoint_get(str(h.get("repo") or ""),
                                  int(h.get("pr") or 0))
    except Exception:
        return False
    if not isinstance(cp, dict):
        return False
    if cp.get("status") != "in_progress":
        return False
    if (cp.get("in_progress_run") or "") != str(h.get("holder") or ""):
        return False
    try:
        age = now - float(cp.get("updated_ts") or 0)
    except (TypeError, ValueError):
        return False
    return age < float(stale_after_s)


def reset_spend(*, store: store_mod.Store) -> dict:
    """Zero the env spend ledger. Skipped/queued rows are untouched:
    nothing auto-processes after a reset.

    Refused while any reservation is open: zeroing reserved_usd under
    an open holder would orphan its accounting (a later recovery would
    see reserved < held and fail every tick). Settle or recover first.
    """
    open_holders = store.reservations_open()
    if open_holders:
        raise store_mod.StoreError(
            f"reset refused: {len(open_holders)} reservation(s) open "
            f"(e.g. {open_holders[0].get('holder', '?')[:8]}); "
            "settle or recover them first")
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
