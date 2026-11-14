"""Operator settings layer (PROD-01 Track B): additive file-based controls.

The event-pipeline worker is historically configured purely by env vars
(see docs/dev-events.env.example for key names such as CODATIEL_DEV_STATE
and CODATIEL_DEV_REPOS). This module adds an OPTIONAL, additive settings
file — ``[state_dir]/operator.json`` — that an operator dashboard (or an
operator editing JSON by hand) can manage. Track A (publisher +
spend-limit settings on its own branch) will be reconciled to read this
file at promotion time; the contract implemented here is:

* The worker reads ``operator.json`` when present, otherwise it behaves
  exactly as before (env configuration only).
* Reads never touch secret values: no ``*_KEY``/``*_TOKEN``/``*_SECRET``
  value is ever loaded by this module. Secrets stay in env files.
* Every write is validated (negatives, unknown repos, unknown profiles
  are rejected with explained errors), written atomically (temp file +
  ``os.replace``, never half-written), and appended to the audit log
  ``[state_dir]/operator-audit.jsonl``.
* Settings are durable across restarts: they live in the env state dir
  next to ``events.sqlite``, not in memory.

Precedence (env vs file) — documented once, enforced in one place:

* Spending limits (monthly / daily / per-PR): the EFFECTIVE limit is
  the MINIMUM of all applicable sources. A file value can only ever
  lower the env-configured (or default) ceiling; a stale file can never
  raise limits above what the env explicitly sets, and vice versa. An
  unset file field leaves the env value standing.
* Repo allowlist: the effective set is the INTERSECTION of the env
  allowlist and the file's enabled repos. The file can only narrow.
* Pauses: ``pause_reviews`` pauses when set in the file (there is no
  env equivalent). Fail-closed: either signal pauses.
* Notification destinations (``notify_to``/``notify_sender``): an
  explicitly set, non-empty file value overrides env; otherwise env
  stands. (Rationale: the dashboard is the operator control surface;
  env remains the fallback.) This is the one place where the file wins
  over an explicitly set env value, and every override is audit-logged.
* Model profile: file value when set, else ``REVIEW_PIPELINE`` env,
  else the pipeline default. File values are restricted to the fixed
  pipeline choices and can never bypass spend/review contracts (the
  profile only selects between the two qualified pipelines; ceilings,
  reserves, no-retry and no-PR-comment rules are untouched).
* Upstream OpenAI project limit: DISPLAY ONLY. Never enforced, never
  fetched with admin credentials, never conflated with the Codatiel
  ledger. It exists so the operator can eyeball headroom in one place.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

OPERATOR_FILENAME = "operator.json"
AUDIT_FILENAME = "operator-audit.jsonl"
SCHEMA_VERSION = "codatiel_i2.operator.v1"

# Fixed model-profile choices: the two qualified review pipelines.
# Imported from the pipeline module so the allowlist cannot drift.
from codatiel_i2.event_pipeline import PIPELINE_MODES as MODEL_PROFILES

SECRET_NAME_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")

DEFAULT_ALERT_THRESHOLDS = {"warn_frac": 0.8, "critical_frac": 0.95}

DEFAULT_BILLING_URL = (
    "https://platform.openai.com/settings/organization/limits")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Top-level keys the file may carry. Anything else is rejected so a
# typo'd key fails loudly instead of silently doing nothing.
KNOWN_KEYS = frozenset({
    "schema_version", "monthly_budget_usd", "daily_budget_usd",
    "per_pr_breaker_usd", "alert_thresholds", "repos",
    "pause_reviews", "pause_github_comments",
    # Platform-01 migration: unified publication pause (email under the
    # same dispatch gate as comments). New key; the legacy comment key
    # keeps working via the settings import mapping.
    "pause_publication",
    "email_enabled", "email_on_complete",
    "notify_to", "notify_sender", "model_profile",
    "openai_project_limit_usd", "openai_billing_url",
    "updated_at", "updated_by",
})


class OperatorConfigError(RuntimeError):
    """Invalid operator settings: caller must refuse, never default."""


def secret_like(name: str) -> bool:
    """True when an env/config key name looks secret-bearing."""
    upper = str(name or "").upper()
    return any(hint in upper for hint in SECRET_NAME_HINTS)


def mask_value(name: str, present: bool) -> str:
    """Presence marker for secret-sounding keys. Never the value."""
    _ = name
    return "set" if present else "unset"


@dataclass
class OperatorSettings:
    """Validated operator controls. None/empty = fall back to env."""

    monthly_budget_usd: float | None = None
    daily_budget_usd: float | None = None
    per_pr_breaker_usd: float | None = None
    alert_thresholds: dict = field(
        default_factory=lambda: dict(DEFAULT_ALERT_THRESHOLDS))
    # repo slug -> enabled flag. Empty dict = no file constraint.
    repos: dict[str, bool] = field(default_factory=dict)
    pause_reviews: bool = False
    pause_github_comments: bool = True
    # Platform-01 unified publication pause (email under the same gate
    # as comments). None = fall back to pause_github_comments.
    pause_publication: bool | None = None
    email_enabled: bool = True
    email_on_complete: bool = True
    notify_to: list[str] = field(default_factory=list)
    notify_sender: str = ""
    model_profile: str | None = None
    openai_project_limit_usd: float | None = None
    openai_billing_url: str = DEFAULT_BILLING_URL
    updated_at: str = ""
    updated_by: str = ""
    # True when loaded from operator.json; False = env-only defaults.
    present: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "monthly_budget_usd": self.monthly_budget_usd,
            "daily_budget_usd": self.daily_budget_usd,
            "per_pr_breaker_usd": self.per_pr_breaker_usd,
            "alert_thresholds": dict(self.alert_thresholds),
            "repos": dict(self.repos),
            "pause_reviews": self.pause_reviews,
            "pause_github_comments": self.pause_github_comments,
            "pause_publication": self.pause_publication,
            "email_enabled": self.email_enabled,
            "email_on_complete": self.email_on_complete,
            "notify_to": list(self.notify_to),
            "notify_sender": self.notify_sender,
            "model_profile": self.model_profile,
            "openai_project_limit_usd": self.openai_project_limit_usd,
            "openai_billing_url": self.openai_billing_url,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


# -- paths ---------------------------------------------------------------


def operator_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / OPERATOR_FILENAME


def audit_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / AUDIT_FILENAME


def state_dir_from_env(explicit: str | None = None) -> Path | None:
    """Resolve the env state dir without reading any secret values."""
    if explicit:
        return Path(explicit)
    for env_name in ("CODATIEL_DEV_STATE", "CODATIEL_TEST_STATE"):
        raw = (os.environ.get(env_name, "") or "").strip()
        if raw:
            return Path(raw)
    return None


# -- validation ------------------------------------------------------------


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_doc(doc: Any,
                 known_repos: list[str] | None = None) -> list[str]:
    """Return a list of human-readable errors; [] means valid."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["operator.json must hold a JSON object"]
    for key in doc:
        if key not in KNOWN_KEYS:
            errors.append(
                f"unknown key {key!r}; known keys: "
                f"{sorted(KNOWN_KEYS)}")
    for key in ("monthly_budget_usd", "daily_budget_usd",
                "per_pr_breaker_usd"):
        value = doc.get(key)
        if value is None:
            continue
        if not _is_num(value):
            errors.append(f"{key} must be a number of USD, got {value!r}")
        elif float(value) <= 0:
            errors.append(
                f"{key} must be positive (got {value}); unset means "
                "fall back to env")
    thresh = doc.get("alert_thresholds")
    if thresh is not None:
        if not isinstance(thresh, dict):
            errors.append("alert_thresholds must be an object with "
                          "warn_frac/critical_frac")
        else:
            for sub in ("warn_frac", "critical_frac"):
                value = thresh.get(sub)
                if value is None:
                    errors.append(f"alert_thresholds.{sub} is required")
                elif not _is_num(value) or not (
                        0 < float(value) <= 1):
                    errors.append(
                        f"alert_thresholds.{sub} must be in (0, 1], "
                        f"got {value!r}")
            extra = set(thresh) - {"warn_frac", "critical_frac"}
            if extra:
                errors.append(
                    f"alert_thresholds has unknown entries: "
                    f"{sorted(extra)}")
            if (_is_num(thresh.get("warn_frac"))
                    and _is_num(thresh.get("critical_frac"))
                    and not (float(thresh["warn_frac"])
                             < float(thresh["critical_frac"]))):
                errors.append("alert_thresholds.warn_frac must be below "
                              "critical_frac")
    repos = doc.get("repos")
    if repos is not None:
        if not isinstance(repos, dict):
            errors.append("repos must be an object mapping "
                          "'owner/repo' to true/false")
        else:
            for slug, enabled in repos.items():
                if not isinstance(slug, str) or not REPO_RE.match(slug):
                    errors.append(
                        f"repos key {slug!r} is not an 'owner/repo' slug")
                if not isinstance(enabled, bool):
                    errors.append(
                        f"repos[{slug!r}] must be true/false, "
                        f"got {enabled!r}")
                if (known_repos is not None and isinstance(slug, str)
                        and REPO_RE.match(slug)
                        and slug not in known_repos):
                    errors.append(
                        f"repos[{slug!r}] is unknown: not in the "
                        "configured env allowlist "
                        f"{known_repos}; refusing (stale config can "
                        "never widen access)")
    for key in ("pause_reviews", "pause_github_comments",
                "email_enabled", "email_on_complete"):
        if key in doc and not isinstance(doc[key], bool):
            errors.append(f"{key} must be true/false, got {doc[key]!r}")
    if ("pause_publication" in doc and doc["pause_publication"] is not None
            and not isinstance(doc["pause_publication"], bool)):
        errors.append("pause_publication must be true/false or unset "
                      f"(inherit pause_github_comments), got "
                      f"{doc['pause_publication']!r}")
    notify_to = doc.get("notify_to")
    if notify_to is not None:
        if not isinstance(notify_to, list) or not all(
                isinstance(a, str) for a in notify_to):
            errors.append("notify_to must be a list of email addresses")
        else:
            for addr in notify_to:
                if not EMAIL_RE.match(addr.strip()):
                    errors.append(
                        f"notify_to entry {addr!r} is not an email "
                        "address")
    sender = doc.get("notify_sender")
    if sender is not None:
        if not isinstance(sender, str):
            errors.append("notify_sender must be a string")
        elif sender.strip() and not EMAIL_RE.match(sender.strip()):
            errors.append(
                f"notify_sender {sender!r} is not an email address")
    profile = doc.get("model_profile")
    if profile is not None:
        if profile not in MODEL_PROFILES:
            errors.append(
                f"model_profile must be one of {list(MODEL_PROFILES)} "
                f"(fixed choices; cannot bypass spend/review "
                f"contracts); got {profile!r}")
    upstream = doc.get("openai_project_limit_usd")
    if upstream is not None:
        if not _is_num(upstream):
            errors.append("openai_project_limit_usd must be a number "
                          f"of USD, got {upstream!r}")
        elif float(upstream) <= 0:
            errors.append("openai_project_limit_usd must be positive; "
                          "it is display-only and never enforced")
    url = doc.get("openai_billing_url")
    if url is not None:
        if not isinstance(url, str) or not url.startswith(
                ("https://", "http://")):
            errors.append("openai_billing_url must be an http(s) URL")
    return errors


def parse(doc: dict[str, Any]) -> OperatorSettings:
    """Build validated settings from an already-checked doc."""
    return OperatorSettings(
        monthly_budget_usd=(None if doc.get("monthly_budget_usd") is None
                            else float(doc["monthly_budget_usd"])),
        daily_budget_usd=(None if doc.get("daily_budget_usd") is None
                         else float(doc["daily_budget_usd"])),
        per_pr_breaker_usd=(None if doc.get("per_pr_breaker_usd") is None
                            else float(doc["per_pr_breaker_usd"])),
        alert_thresholds=dict(doc.get("alert_thresholds")
                              or DEFAULT_ALERT_THRESHOLDS),
        repos=dict(doc.get("repos") or {}),
        pause_reviews=bool(doc.get("pause_reviews", False)),
        pause_github_comments=bool(doc.get("pause_github_comments", True)),
        pause_publication=(None if doc.get("pause_publication") is None
                           else bool(doc.get("pause_publication"))),
        email_enabled=bool(doc.get("email_enabled", True)),
        email_on_complete=bool(doc.get("email_on_complete", True)),
        notify_to=list(doc.get("notify_to") or []),
        notify_sender=str(doc.get("notify_sender") or ""),
        model_profile=doc.get("model_profile"),
        openai_project_limit_usd=(
            None if doc.get("openai_project_limit_usd") is None
            else float(doc["openai_project_limit_usd"])),
        openai_billing_url=str(doc.get("openai_billing_url")
                               or DEFAULT_BILLING_URL),
        updated_at=str(doc.get("updated_at") or ""),
        updated_by=str(doc.get("updated_by") or ""),
        present=True,
    )


# -- load / save -----------------------------------------------------------


def load(state_dir: Path | str,
         known_repos: list[str] | None = None
         ) -> tuple[OperatorSettings, str]:
    """Load settings; ("settings", "file"|"absent").

    Absent file = env-only behaviour (never an error). A present but
    invalid file is a loud error: the worker must refuse rather than
    run on unknown controls.
    """
    path = operator_path(state_dir)
    if not path.exists():
        return OperatorSettings(present=False), "absent"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OperatorConfigError(
            f"operator.json at {path} is unreadable "
            f"({type(exc).__name__}: {exc}); refusing to run on "
            "unparsed operator controls") from exc
    errors = validate_doc(doc, known_repos=known_repos)
    if errors:
        raise OperatorConfigError(
            "operator.json invalid: " + "; ".join(errors))
    settings = parse(doc)
    return settings, "file"


def _atomic_write(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _audit(state_dir: Path | str, entry: dict[str, Any]) -> None:
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **entry}
    path = audit_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def audit_trail(state_dir: Path | str, limit: int = 50) -> list[dict]:
    """Most recent audit entries first. Missing log = []."""
    path = audit_path(state_dir)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    rows.reverse()
    return rows[:max(0, limit)]


def save(state_dir: Path | str, patch: dict[str, Any], *,
         actor: str = "dashboard",
         known_repos: list[str] | None = None) -> OperatorSettings:
    """Validate, atomically persist, and audit-log a settings change.

    ``patch`` merges over the current file (top level; ``repos``
    merges per-repo so toggling one repo never drops the rest).
    Raises OperatorConfigError with every validation error; rejected
    attempts are audit-logged too.
    """
    if not isinstance(patch, dict):
        raise OperatorConfigError("settings patch must be a JSON object")
    current, _ = load(state_dir, known_repos=None)
    base = current.to_dict()
    merged = copy.deepcopy(base)
    repos_patch = patch.get("repos")
    for key, value in patch.items():
        if key == "repos":
            continue
        merged[key] = copy.deepcopy(value)
    if repos_patch is not None:
        if not isinstance(repos_patch, dict):
            raise OperatorConfigError(
                "repos patch must be an object mapping "
                "'owner/repo' to true/false")
        merged_repos = dict(merged.get("repos") or {})
        for slug, enabled in repos_patch.items():
            if enabled is None:
                merged_repos.pop(slug, None)
            else:
                merged_repos[slug] = enabled
        merged["repos"] = merged_repos
    merged.pop("present", None)
    errors = validate_doc(merged, known_repos=known_repos)
    if errors:
        _audit(state_dir, {"actor": actor, "action": "rejected",
                           "errors": errors})
        raise OperatorConfigError(
            "operator settings rejected: " + "; ".join(errors))
    merged["schema_version"] = SCHEMA_VERSION
    merged["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime())
    merged["updated_by"] = actor
    changes: dict[str, dict[str, Any]] = {}
    for key, value in merged.items():
        if key in ("updated_at", "updated_by"):
            continue
        if base.get(key) != value:
            changes[key] = {"old": base.get(key), "new": value}
    _atomic_write(operator_path(state_dir),
                  (json.dumps(merged, indent=2, sort_keys=True)
                   + "\n").encode("utf-8"))
    _audit(state_dir, {"actor": actor, "action": "saved",
                       "changes": changes})
    settings = parse(merged)
    return settings


# -- effective resolution (the worker contract) ------------------------------


def effective_spend_caps(settings: OperatorSettings, *,
                         env_monthly_usd: float,
                         env_per_pr_usd: float | None = None
                         ) -> dict[str, Any]:
    """Effective budgets: min() over applicable sources.

    Stale config can never raise limits: each effective value is the
    minimum of the env value and the file value (when the file sets
    one). No env daily source exists for the event pipeline, so a set
    file daily value stands on its own.
    """
    monthly_file = settings.monthly_budget_usd if settings.present else None
    per_pr_file = settings.per_pr_breaker_usd if settings.present else None
    daily_file = settings.daily_budget_usd if settings.present else None
    monthly = (min(env_monthly_usd, monthly_file)
               if monthly_file is not None else env_monthly_usd)
    out: dict[str, Any] = {
        "monthly_env_usd": env_monthly_usd,
        "monthly_file_usd": monthly_file,
        "monthly_effective_usd": monthly,
        "daily_file_usd": daily_file,
        "daily_effective_usd": daily_file,
    }
    if env_per_pr_usd is not None:
        out.update({
            "per_pr_env_usd": env_per_pr_usd,
            "per_pr_file_usd": per_pr_file,
            "per_pr_effective_usd": (
                min(env_per_pr_usd, per_pr_file)
                if per_pr_file is not None else env_per_pr_usd),
        })
    elif per_pr_file is not None:
        out.update({
            "per_pr_env_usd": None,
            "per_pr_file_usd": per_pr_file,
            "per_pr_effective_usd": per_pr_file,
        })
    return out


def effective_repos(settings: OperatorSettings,
                    env_repos: list[str]) -> list[str]:
    """Effective allowlist: env ∩ file-enabled. File only narrows."""
    if not settings.present or not settings.repos:
        return list(env_repos)
    return [r for r in env_repos if settings.repos.get(r) is True]


def effective_model_profile(settings: OperatorSettings,
                            env_pipeline: str,
                            default: str = "terra_luna_additive") -> str:
    if settings.present and settings.model_profile:
        return settings.model_profile
    if env_pipeline in MODEL_PROFILES:
        return env_pipeline
    return default


def email_sending_allowed(settings: OperatorSettings) -> bool:
    if not settings.present:
        return True
    return bool(settings.email_enabled and settings.email_on_complete)


def apply_to_event_config(cfg: Any) -> Any:
    """Return a copy of an EventConfig with operator.json applied.

    Absent file = unchanged config. Invalid file = loud
    OperatorConfigError (worker must refuse, never guess).
    """
    settings, source = load(cfg.state_dir,
                            known_repos=list(cfg.repos or []))
    if source == "absent":
        return replace(cfg, operator_source="env")
    caps = effective_spend_caps(
        settings, env_monthly_usd=cfg.spend_cap_usd,
        env_per_pr_usd=(cfg.whole_pr_ceiling_usd
                        if cfg.env == "dev" else None))
    cfg = replace(
        cfg,
        spend_cap_usd=caps["monthly_effective_usd"],
        whole_pr_ceiling_usd=(
            caps.get("per_pr_effective_usd")
            if cfg.env == "dev" else cfg.whole_pr_ceiling_usd),
        daily_cap_usd=caps["daily_effective_usd"],
        pipeline=effective_model_profile(settings, cfg.pipeline),
        notify_to=(list(settings.notify_to) if settings.notify_to
                   else list(cfg.notify_to)),
        notify_sender=(settings.notify_sender.strip()
                       if settings.notify_sender.strip()
                       else cfg.notify_sender),
        email_enabled=email_sending_allowed(settings),
        operator_source="file",
    )
    return cfg


def spend_on_day(store: Any, day: str) -> dict[str, Any]:
    """Codatiel-ledger spend for one UTC day (YYYY-MM-DD).

    Sums actual review costs (SUM, never cost x COUNT). Superseded
    mid-flight inference has no reviews row — its delivery cost is
    included exactly once here, including through a later replay mark
    (replays preserve the original cost and carry the original
    decision in their detail); completed/failed deliveries are
    excluded because their cost already lives in the reviews table.
    Reviews with unknown cost contribute $0 here — they are tracked
    separately as unknown-count (including superseded runs whose true
    cost never resolved), and the spend ledger holds reserves for
    them. Labelled Codatiel-ledger-only, never total OpenAI spend.
    """
    try:
        r = store.cx.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*), "
            "COALESCE(SUM(CASE WHEN cost_unknown THEN 1 ELSE 0 END), 0) "
            "FROM reviews WHERE date(finished_ts, 'unixepoch') = ?",
            (day,)).fetchone()
        known = store.cx.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM reviews "
            "WHERE date(finished_ts, 'unixepoch') = ? "
            "AND NOT cost_unknown AND cost_usd IS NOT NULL",
            (day,)).fetchone()[0]
        # Superseded spend is attributed to the day it was incurred
        # (finished_ts), matching the reviews table — not the day the
        # delivery arrived. Pre-finished_ts rows fall back to
        # received_ts.
        day_of = "date(COALESCE(finished_ts, received_ts), 'unixepoch')"
        s = store.cx.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM deliveries "
            "WHERE (decision = 'skipped:stale_superseded' OR "
            "(decision = 'skipped:replay' AND "
            "json_extract(detail, '$.original_decision') = "
            "'skipped:stale_superseded')) "
            "AND cost_usd IS NOT NULL "
            f"AND {day_of} = ?",
            (day,)).fetchone()
        su = store.cx.execute(
            "SELECT COUNT(*) FROM deliveries "
            "WHERE (decision = 'skipped:stale_superseded' OR "
            "(decision = 'skipped:replay' AND "
            "json_extract(detail, '$.original_decision') = "
            "'skipped:stale_superseded')) "
            "AND cost_usd IS NULL "
            f"AND {day_of} = ?",
            (day,)).fetchone()[0]
    except Exception as exc:
        raise OperatorConfigError(
            f"daily spend query failed: {exc}") from exc
    review_count = int(r[1] or 0)
    unknown = int(r[2] or 0) + int(su or 0)
    superseded = float(s[0] or 0.0)
    total = float(known or 0.0) + superseded
    return {"day": day, "codatiel_ledger_usd": round(total, 6),
            "reviews": review_count, "unknown_cost_reviews": unknown,
            "superseded_inference_usd": round(superseded, 6)}


def ingest_gate(cfg: Any, store: Any, ev: dict[str, Any],
                settings: OperatorSettings | None = None
                ) -> dict[str, Any] | None:
    """Pre-inference gate for the automatic ingest path.

    Returns None when the event may proceed, else a
    {decision, reason} dict the caller records with $0 cost and no
    inference. Covers: global pause, operator repo narrowing, and the
    effective daily budget. (Monthly/per-PR caps stay enforced by the
    existing pipeline checks on the narrowed cfg.)
    """
    if settings is None:
        settings, _ = load(cfg.state_dir,
                            known_repos=list(cfg.repos or []))
    if settings.present and settings.pause_reviews:
        return {"decision": "skipped:paused",
                "reason": "operator pause_reviews is set; automatic "
                          "ingest takes no new reviews (explicit "
                          "'events review' still available)"}
    repo = str(ev.get("repo") or "")
    if repo and repo not in effective_repos(
            settings, list(cfg.repos or [])):
        return {"decision": "skipped:not-allowlisted",
                "reason": f"repo {repo!r} is not in the effective "
                          "operator allowlist; refusing (no inference, "
                          "no spend)"}
    daily_cap = getattr(cfg, "daily_cap_usd", None)
    if daily_cap is not None:
        import datetime as _dt
        today = _dt.datetime.now(
            _dt.timezone.utc).strftime("%Y-%m-%d")
        spent = spend_on_day(store, today)
        reserve = float(getattr(cfg, "reserve_usd", 1.0) or 0.0)
        if (spent["codatiel_ledger_usd"] + reserve > float(daily_cap)):
            return {
                "decision": "skipped:ceiling",
                "reason": (
                    f"daily Codatiel budget ${float(daily_cap):.2f} "
                    f"reached (ledger ${spent['codatiel_ledger_usd']:.4f} "
                    f"on {today}); inference stopped, event recorded; "
                    "no backlog processing on reset"),
            }
    return None
