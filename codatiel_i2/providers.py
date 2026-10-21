"""Unified model backends for the canonical Codatiel I2 review service.

All three backends drive the SAME SDK-managed service with the SAME tool
surface. Provider-specific protocol adaptation lives here; the investigation
mission, evidence rules, and scoring criteria are identical across backends.

Privacy posture (operator-approved, recorded):
- Luna: OpenAI Code Review project, STANDARD_API_NO_TRAINING, store=false.
- Haiku: Anthropic commercial terms, no training on API inputs/outputs by
  default, ~30-day standard retention. ZDR not applicable here.
- Gemma: local inference, no third-party transmission.

No backend may silently degrade to static review: missing credentials or
incompatible configuration raises an explicit error before any run starts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BACKENDS = ("luna", "haiku", "gemma")

_PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "model_profiles.json"


def load_profiles() -> dict[str, dict]:
    raw = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
    return {p.get("backend", p.get("profile", "")): p
            for p in raw.get("profiles", [])}


def resolve_backend(name: str) -> dict[str, Any]:
    """Resolve a backend name to its profile + credential presence.

    Returns profile metadata and key presence only — never secret values.
    Raises on unknown backends or missing credentials.
    """
    if name not in BACKENDS:
        raise ValueError(f"unknown backend {name!r}; choose from {BACKENDS}")
    profiles = load_profiles()
    profile = profiles.get(name)
    if profile is None:
        raise ValueError(f"no profile configured for backend {name!r}")
    cred_env = str(profile.get("credential_env") or "")
    key_present = bool(cred_env and os.environ.get(cred_env))
    if cred_env and not key_present:
        raise RuntimeError(
            f"backend {name!r} needs {cred_env} in the environment; "
            "refusing to run without credentials (no static fallback)")
    return {"backend": name, "profile": profile.get("profile", name),
            "model": profile.get("model", ""), "provider": profile.get("provider", ""),
            "privacy": profile.get("privacy", ""),
            "rate_usd_per_mtok": profile.get("rate_usd_per_mtok") or {},
            "rate_status": profile.get("rate_status", "unconfirmed"),
            "max_usd_per_review": float(profile.get("max_usd_per_review", 5.0)),
            "credential_env": cred_env, "key_present": key_present}


def build_model(name: str) -> tuple[Any, Any]:
    """Build (SDK model, model settings) for a backend. No inference here."""
    from codatiel_i2 import runtime_sdk

    resolved = resolve_backend(name)
    if name == "luna":
        return runtime_sdk.luna_model_id(), runtime_sdk.luna_model_settings()
    if name == "gemma":
        profiles = load_profiles()
        profile = profiles[name]
        return (runtime_sdk.gemma_model(
            model=str(profile.get("model", "")),
            base_url=str(profile.get("base_url", "")),
            api_key=None), _default_settings())
    if name == "haiku":
        return runtime_sdk.HaikuModel(), _default_settings()
    raise ValueError(f"unknown backend {name!r}")


def _default_settings() -> Any:
    agents = runtime_sdk_require()
    from agents.model_settings import ModelSettings
    return ModelSettings()


def runtime_sdk_require() -> Any:
    from codatiel_i2 import runtime_sdk
    return runtime_sdk.require_sdk()


def estimate_cost_usd(prompt_chars: int, max_turns: int,
                      rate: dict[str, Any]) -> float | None:
    """Conservative preflight estimate. None when rates are unconfirmed."""
    try:
        rate_in = rate.get("input")
        rate_out = rate.get("output")
        if rate_in is None or rate_out is None:
            return None
        in_tok = prompt_chars / 4.0
        out_tok = max_turns * 4000.0
        return in_tok / 1e6 * float(rate_in) + out_tok / 1e6 * float(rate_out)
    except (ValueError, TypeError):
        return None


def actual_cost_usd(usage: Any, rate: dict[str, Any]) -> float | None:
    """Post-hoc cost from recorded usage. None when rates unconfirmed."""
    try:
        rate_in = rate.get("input")
        rate_out = rate.get("output")
        if rate_in is None or rate_out is None:
            return None
        return (int(getattr(usage, "input_tokens", 0) or 0) / 1e6 * float(rate_in)
                + int(getattr(usage, "output_tokens", 0) or 0) / 1e6 * float(rate_out))
    except (ValueError, TypeError):
        return None
