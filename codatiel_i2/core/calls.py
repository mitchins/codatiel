"""Call ledgers: authoritative tool/model invocation records (§A5.6).

Counters in metrics.json are not enough for ablation or debugging. Every
tool call and every model call is appended here mechanically by the tool /
model layer itself (via the `ledger` kwarg or `traced` decorator), with
argument digests, status, latency and produced evidence IDs. Detectors never
hand-write these files.
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any

from codatiel_i2.common.hashing import sha256_text
from codatiel_i2.common.jsonio import canonical_json
from codatiel_i2.common.time import utc_now_iso

TOOL_LEDGER_SCHEMA = "codatiel_i2.tool_calls.v1"
MODEL_LEDGER_SCHEMA = "codatiel_i2.model_calls.v1"


def new_ledger() -> list[dict]:
    return []


def record_tool_call(ledger: list[dict] | None, *, tool: str, arguments: dict,
                     status: str, latency_ms: float,
                     evidence_ids: list[str] | None = None,
                     error: str | None = None) -> dict:
    row = {
        "tool": tool,
        "arguments_digest": sha256_text(canonical_json(arguments)),
        "status": status,
        "latency_ms": round(latency_ms, 2),
        "evidence_ids": list(evidence_ids or []),
        "error": error,
        "at": utc_now_iso(),
    }
    if ledger is not None:
        ledger.append(row)
    return row


def record_model_call(ledger: list[dict] | None, *, profile: str, model: str,
                      role: str, candidate_id: str, input_tokens: int,
                      output_tokens: int, latency_ms: float, finish_reason: str,
                      infra_status: str, error: str | None = None) -> dict:
    row = {
        "profile": profile,
        "model": model,
        "role": role,
        "candidate_id": candidate_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": round(latency_ms, 2),
        "finish_reason": finish_reason,
        "infra_status": infra_status,
        "error": error,
        "at": utc_now_iso(),
    }
    if ledger is not None:
        ledger.append(row)
    return row


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def traced(tool_name: str):
    """Decorator: record every invocation into the `ledger` kwarg (if given)."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any):
            ledger = kwargs.pop("ledger", None)
            safe_args = {"args": [_safe(a) for a in args],
                         "kwargs": {k: _safe(v) for k, v in kwargs.items()}}
            started = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                try:
                    record_tool_call(ledger, tool=tool_name, arguments=safe_args,
                                     status="error", latency_ms=(time.monotonic() - started) * 1000,
                                     error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass
                raise
            evidence = result.get("evidence") if isinstance(result, dict) else None
            if isinstance(evidence, dict):
                evidence_ids = [str(evidence.get("evidence_id", ""))]
            elif isinstance(evidence, list):
                evidence_ids = [str(e.get("evidence_id", "")) for e in evidence if isinstance(e, dict)]
            else:
                evidence_ids = []
            record_tool_call(ledger, tool=tool_name, arguments=safe_args,
                             status="ok", latency_ms=(time.monotonic() - started) * 1000,
                             evidence_ids=[e for e in evidence_ids if e])
            return result

        return wrapper

    return decorator
