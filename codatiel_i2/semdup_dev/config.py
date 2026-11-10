"""DEV semdup configuration from the environment (fail-closed).

Model identity: the qualified default investigator is Gemma 4 26B A4B
(QAT W4A16) served at the configured OpenAI-compatible endpoint. Both
the requested model id and the served-model expectation are explicit;
`inference.chat(..., expected_model_id=...)` invalidates any response
whose served id differs (fail-closed, never silently accepted).

No cloud-authenticated model budget is used here: the endpoint must be
a locally operated OpenAI-compatible server (the qualified vLLM host),
never the Codatiel TEST API credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

QUALIFIED_MODEL_ID = "reinforce20001/gemma4-26b-a4b-it-qat-w4a16-ct"
QUALIFIED_DEFAULT_URL = "http://192.168.1.14:8000/v1"

MAX_FAMILIES_PER_DELTA = 8
MAX_TARGETS_PER_DELTA = 8
NEIGHBORS_K = 30


@dataclass
class SemdupDevConfig:
    env: str  # must be "dev"
    state_dir: Path
    repos: list[str] = field(default_factory=list)
    investigator_url: str = QUALIFIED_DEFAULT_URL
    investigator_model: str = QUALIFIED_MODEL_ID
    investigator_expected: str = QUALIFIED_MODEL_ID
    challenger_url: str = QUALIFIED_DEFAULT_URL
    challenger_model: str = QUALIFIED_MODEL_ID
    challenger_expected: str = QUALIFIED_MODEL_ID
    api_key: str | None = None
    max_targets_per_delta: int = MAX_TARGETS_PER_DELTA
    neighbors_k: int = NEIGHBORS_K
    allow_embeddings: bool = True
    advisory_enabled: bool = True
    editorial_shadow: bool = True
    editorial_max_pairs: int = 4
    editorial_ceiling_usd: float = 0.20
    notify_to: list[str] = field(default_factory=list)
    notify_sender: str = ""


def _env_list(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",")
            if p.strip()]


class SemdupConfigError(RuntimeError):
    """Refuse to run rather than substitute an unverified configuration."""


def config_from_env() -> SemdupDevConfig:
    env = (os.environ.get("CODATIEL_ENV", "") or "").strip().lower()
    if env != "dev":
        raise SemdupConfigError(
            "CODATIEL_ENV must be 'dev' for the semdup DEV worker; "
            "refusing (TEST/Production reviewers are untouched).")
    state_dir = Path(os.environ.get("CODATIEL_DEV_STATE", "./dev_events_state"))
    repos = _env_list("CODATIEL_DEV_REPOS")
    if not repos:
        raise SemdupConfigError("CODATIEL_DEV_REPOS is empty; refusing.")
    inv_url = os.environ.get("SEMDUP_INVESTIGATOR_URL", QUALIFIED_DEFAULT_URL)
    inv_model = os.environ.get("SEMDUP_INVESTIGATOR_MODEL", QUALIFIED_MODEL_ID)
    inv_expected = os.environ.get("SEMDUP_INVESTIGATOR_EXPECTED", inv_model)
    ch_url = os.environ.get("SEMDUP_CHALLENGER_URL", inv_url)
    ch_model = os.environ.get("SEMDUP_CHALLENGER_MODEL", inv_model)
    ch_expected = os.environ.get("SEMDUP_CHALLENGER_EXPECTED", ch_model)
    # No silent substitution: any non-qualified checkpoint must be an
    # explicit operator choice, recorded in findings provenance. The
    # default path requires the qualified identity exactly.
    api_key = os.environ.get("SEMDUP_API_KEY")
    try:
        max_targets = int(os.environ.get("SEMDUP_MAX_TARGETS_PER_DELTA",
                                         str(MAX_TARGETS_PER_DELTA)))
        neighbors_k = int(os.environ.get("SEMDUP_NEIGHBORS_K",
                                         str(NEIGHBORS_K)))
    except ValueError:
        raise SemdupConfigError("SEMDUP_MAX_TARGETS_PER_DELTA / "
                                "SEMDUP_NEIGHBORS_K must be integers")
    if max_targets <= 0 or neighbors_k <= 0:
        raise SemdupConfigError("target/neighbor budgets must be positive")
    allow_embeddings = os.environ.get("SEMDUP_ALLOW_EMBEDDINGS",
                                       "1").strip() not in ("0", "no", "false")
    # Independently switchable advisory layer: 0 degrades to no
    # semantic-duplication advisory without affecting ordinary reviewing.
    advisory_enabled = os.environ.get("SEMDUP_ADVISORY",
                                      "1").strip() not in ("0", "no", "false")
    # Parallel editorial shadow (SEMDUP-07 Track A): Terra-propose /
    # Luna-edit runs alongside the existing findings+advisories path on
    # the same delta, persisted separately. 0 disables the editorial
    # pass only; engine and advisory behaviour is unchanged.
    editorial_shadow = os.environ.get("SEMDUP_EDITORIAL_SHADOW",
                                      "1").strip() not in ("0", "no", "false")
    try:
        editorial_max_pairs = int(os.environ.get(
            "SEMDUP_EDITORIAL_MAX_PAIRS", "4"))
        editorial_ceiling_usd = float(os.environ.get(
            "SEMDUP_EDITORIAL_CEILING_USD", "0.20"))
    except ValueError:
        raise SemdupConfigError("SEMDUP_EDITORIAL_MAX_PAIRS / "
                                "SEMDUP_EDITORIAL_CEILING_USD must be numeric")
    if editorial_max_pairs <= 0 or editorial_ceiling_usd <= 0:
        raise SemdupConfigError("editorial pair/ceiling budgets must be "
                                "positive")
    return SemdupDevConfig(
        env=env, state_dir=state_dir, repos=repos,
        investigator_url=inv_url, investigator_model=inv_model,
        investigator_expected=inv_expected,
        challenger_url=ch_url, challenger_model=ch_model,
        challenger_expected=ch_expected, api_key=api_key,
        max_targets_per_delta=max_targets, neighbors_k=neighbors_k,
        allow_embeddings=allow_embeddings,
        advisory_enabled=advisory_enabled,
        editorial_shadow=editorial_shadow,
        editorial_max_pairs=editorial_max_pairs,
        editorial_ceiling_usd=editorial_ceiling_usd,
        notify_to=_env_list("CODATIEL_DEV_TO"),
        notify_sender=os.environ.get("CODATIEL_DEV_SENDER", ""))
