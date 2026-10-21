"""Model profiles + run controller (lifted from Iteration 1, rewritten).

Lifted concepts: model profiles, endpoint abstraction, preflight, timeout
handling, concurrency/rate limits, run manifests, retries.

Critical rewrite: infrastructure status is out-of-band. This module never
fabricates semantic responses for endpoint failure / timeout / parse failure
/ quota failure. Callers receive ModelInfraError and must record infra status
separately from semantic results.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path


class ModelInfraError(RuntimeError):
    """Endpoint/timeout/quota/parse-infrastructure failure. Never a semantic answer."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind


@dataclass
class ModelProfile:
    name: str
    provider: str
    model: str
    base_url: str
    api_key_env: str = ""
    role: str = ""
    expected_context: int = 32000
    cost_tier: str = "local"
    enabled_by_default: bool = False
    timeout_seconds: int = 120
    max_attempts: int = 3

    def api_key(self) -> str:
        # Only the profile's named credential env var is ever used. There is
        # deliberately no fallback to OPENAI_API_KEY here: silently sending
        # the operator's OpenAI secret as a Bearer token to a third-party
        # local endpoint would be a credential leak. (This module is
        # superseded by providers.py + runtime_sdk.py and has no live
        # importers; the rule is enforced structurally, not by review.)
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""


DEFAULT_PROFILES = [
    ModelProfile(
        name="gemma_26b_main",
        provider="openai_compatible",
        model="reinforce20001/gemma4-26b-a4b-it-qat-w4a16-ct",
        base_url="http://192.168.1.14:8000/v1",
        role="main_verifier",
        expected_context=110000,
        cost_tier="local",
        enabled_by_default=True,
    ),
    ModelProfile(
        name="fastcontext_8b_breadth",
        provider="openai_compatible",
        model="fastcontext-8b",
        base_url="http://192.168.1.14:8001/v1",
        role="breadth_explorer",
        expected_context=32000,
        cost_tier="local",
        enabled_by_default=False,
    ),
    ModelProfile(
        name="frognano_4b_breadth",
        provider="openai_compatible",
        model="frognano-4b",
        base_url="http://192.168.1.14:8002/v1",
        role="breadth_explorer",
        expected_context=16000,
        cost_tier="local",
        enabled_by_default=False,
    ),
]


def load_profiles(path: Path | None = None) -> list[ModelProfile]:
    if path is None:
        return list(DEFAULT_PROFILES)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    profiles: list[ModelProfile] = []
    for row in payload.get("profiles", []):
        profiles.append(ModelProfile(
            name=str(row.get("profile") or row.get("name")),
            provider=str(row.get("provider", "openai_compatible")),
            model=str(row.get("model", "")),
            base_url=str(row.get("base_url") or row.get("default_base_url", "")),
            api_key_env=str(row.get("api_key_env", "")),
            role=str(row.get("role", "")),
            expected_context=int(row.get("expected_context", 32000)),
            cost_tier=str(row.get("cost_tier", "local")),
            enabled_by_default=bool(row.get("enabled_by_default", False)),
        ))
    return profiles


def preflight(profile: ModelProfile, timeout_seconds: int = 30) -> dict:
    """Check endpoint liveness. Returns infra status dict (never a semantic answer)."""
    started = time.monotonic()
    if not profile.base_url or not profile.model:
        return {"profile": profile.name, "status": "missing_config",
                "latency_seconds": round(time.monotonic() - started, 4)}
    try:
        req = urllib.request.Request(
            profile.base_url.rstrip("/") + "/models",
            headers=_auth_headers(profile),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        ids = [r.get("id") for r in payload.get("data", []) if isinstance(r, dict)]
        status = "available" if (not ids or profile.model in ids) else "model_not_found"
        return {"profile": profile.name, "status": status,
                "models": ids[:20],
                "latency_seconds": round(time.monotonic() - started, 4)}
    except urllib.error.HTTPError as exc:
        return {"profile": profile.name, "status": "missing_api_key" if exc.code in (401, 403) else "error",
                "error": f"HTTP {exc.code}", "latency_seconds": round(time.monotonic() - started, 4)}
    except Exception as exc:
        kind = "timeout" if "timed out" in str(exc).lower() or isinstance(exc, TimeoutError) else "error"
        return {"profile": profile.name, "status": kind, "error": f"{type(exc).__name__}: {exc}",
                "latency_seconds": round(time.monotonic() - started, 4)}


def chat_completion(profile: ModelProfile, messages: list[dict],
                    max_tokens: int = 512, temperature: float = 0.0,
                    ledger: list[dict] | None = None, candidate_id: str = "",
                    role: str = "") -> dict:
    """Invoke chat completion with retries. Raises ModelInfraError on infra failure.

    Every attempt outcome (ok or infra failure) is appended to `ledger` when
    given, so model usage is measurable per candidate.
    """
    import time as _time

    from codatiel_i2.core.calls import record_model_call

    url = profile.base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": profile.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    last_error = "unknown"
    started = _time.monotonic()
    for attempt in range(1, profile.max_attempts + 1):
        try:
            req = urllib.request.Request(url, data=body,
                                         headers={**_auth_headers(profile),
                                                  "Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=profile.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            content = _extract_content(payload)
            if not content:
                raise ModelInfraError("empty_response", "model returned empty content")
            usage = payload.get("usage", {}) if isinstance(payload.get("usage"), dict) else {}
            choices = payload.get("choices")
            finish_reason = ""
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                finish_reason = str(choices[0].get("finish_reason") or "")
            record_model_call(ledger, profile=profile.name, model=profile.model,
                              role=role or profile.role, candidate_id=candidate_id,
                              input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                              output_tokens=int(usage.get("completion_tokens", 0) or 0),
                              latency_ms=(_time.monotonic() - started) * 1000,
                              finish_reason=finish_reason,
                              infra_status="ok")
            return {"content": content, "usage": usage, "attempts": attempt,
                    "model": payload.get("model", profile.model)}
        except ModelInfraError as exc:
            record_model_call(ledger, profile=profile.name, model=profile.model,
                              role=role or profile.role, candidate_id=candidate_id,
                              input_tokens=0, output_tokens=0,
                              latency_ms=(_time.monotonic() - started) * 1000,
                              finish_reason="", infra_status=exc.kind,
                              error=str(exc))
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < profile.max_attempts:
                record_model_call(ledger, profile=profile.name, model=profile.model,
                                  role=role or profile.role, candidate_id=candidate_id,
                                  input_tokens=0, output_tokens=0,
                                  latency_ms=(_time.monotonic() - started) * 1000,
                                  finish_reason="", infra_status="rate_limited",
                                  error=f"HTTP 429 attempt {attempt}")
                time.sleep(min(20.0, attempt * 5.0))
                last_error = f"HTTP 429 rate_limited"
                continue
            record_model_call(ledger, profile=profile.name, model=profile.model,
                              role=role or profile.role, candidate_id=candidate_id,
                              input_tokens=0, output_tokens=0,
                              latency_ms=(_time.monotonic() - started) * 1000,
                              finish_reason="", infra_status="model_error",
                              error=f"HTTP {exc.code}")
            raise ModelInfraError("model_error", f"HTTP {exc.code}")
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < profile.max_attempts:
                record_model_call(ledger, profile=profile.name, model=profile.model,
                                  role=role or profile.role, candidate_id=candidate_id,
                                  input_tokens=0, output_tokens=0,
                                  latency_ms=(_time.monotonic() - started) * 1000,
                                  finish_reason="", infra_status="retrying_timeout", error=last_error)
                time.sleep(min(10.0, attempt * 2.0))
                continue
            record_model_call(ledger, profile=profile.name, model=profile.model,
                              role=role or profile.role, candidate_id=candidate_id,
                              input_tokens=0, output_tokens=0,
                              latency_ms=(_time.monotonic() - started) * 1000,
                              finish_reason="", infra_status="timeout", error=last_error)
            raise ModelInfraError("timeout", last_error)
        except Exception as exc:
            record_model_call(ledger, profile=profile.name, model=profile.model,
                              role=role or profile.role, candidate_id=candidate_id,
                              input_tokens=0, output_tokens=0,
                              latency_ms=(_time.monotonic() - started) * 1000,
                              finish_reason="", infra_status="model_error",
                              error=f"{type(exc).__name__}: {exc}")
            raise ModelInfraError("model_error", f"{type(exc).__name__}: {exc}")
    record_model_call(ledger, profile=profile.name, model=profile.model,
                      role=role or profile.role, candidate_id=candidate_id,
                      input_tokens=0, output_tokens=0,
                      latency_ms=(_time.monotonic() - started) * 1000,
                      finish_reason="", infra_status="model_error", error=last_error)
    raise ModelInfraError("model_error", last_error)


def _auth_headers(profile: ModelProfile) -> dict:
    key = profile.api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _extract_content(payload: dict) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
                for key in ("content", "reasoning_content"):
                    value = choice["message"].get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    return ""
