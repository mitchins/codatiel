"""Local chat-completion client: read-only inference, no fallback.

- Configurable base URL + expected model ID (unset = record served ID only).
- Per-response model-ID verification when expected is set; mismatch
  invalidates the call (fail-closed, never silently accepted).
- Bounded retry on 429/5xx + transport errors (failed tail only); timeouts.
- Token usage: measured values when the server reports them, else None
  (never 0-as-unknown); prompt-size estimate kept separate.
- No cloud fallback, no checkpoint switching. Stdlib only (urllib).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class ModelIdentityError(ValueError):
    pass


class InferenceError(RuntimeError):
    def __init__(self, message: str, *, retriable: bool):
        super().__init__(message)
        self.retriable = retriable


@dataclass
class CallResult:
    content: str
    served_model_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    request_id: str | None
    latency_s: float
    attempts: int
    json_mode: str  # "json_object" or "plain-with-local-parse"
    estimated_prompt_tokens: int


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _post(url: str, payload: dict, api_key: str | None, timeout_s: float) -> tuple[dict, str | None]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.headers.get("x-request-id")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise InferenceError(f"HTTP {exc.code}: {body}", retriable=exc.code in (429, 500, 502, 503, 504))
    except (OSError, TimeoutError, ValueError) as exc:
        raise InferenceError(f"transport: {exc}", retriable=True)


def chat(
    base_url: str,
    model: str,
    messages: list[dict],
    *,
    api_key: str | None = None,
    expected_model_id: str | None = None,
    max_tokens: int = 2400,
    timeout_s: float = 180.0,
    attempts: int = 2,
    use_json_mode: bool = True,
) -> CallResult:
    """POST one chat completion with bounded retry. Raises on exhaustion."""
    system_hint = {"role": "system", "content": "Return exactly one JSON object, no prose."}
    full_messages = [system_hint, *messages]
    estimated = estimate_tokens(json.dumps(full_messages))
    last_error: Exception | None = None
    mode = "json_object" if use_json_mode else "plain-with-local-parse"
    for attempt in range(1, attempts + 1):
        payload = {"model": model, "messages": full_messages,
                   "max_tokens": max_tokens, "temperature": 0}
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        start = time.monotonic()
        try:
            body, req_id = _post(base_url.rstrip("/") + "/chat/completions", payload, api_key, timeout_s)
        except InferenceError as exc:
            last_error = exc
            if not exc.retriable or attempt == attempts:
                raise
            time.sleep(min(2 ** attempt, 8))
            continue
        latency = time.monotonic() - start
        try:
            choice = body["choices"][0]["message"]
            content = choice.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise InferenceError(f"malformed response envelope: {exc}", retriable=False)
        served = body.get("model")
        if expected_model_id is not None and served != expected_model_id:
            raise ModelIdentityError(f"served={served!r} expected={expected_model_id!r}")
        usage = body.get("usage") or {}
        prompt_t = usage.get("prompt_tokens")
        compl_t = usage.get("completion_tokens")
        return CallResult(
            content=content, served_model_id=served,
            prompt_tokens=prompt_t if isinstance(prompt_t, int) else None,
            completion_tokens=compl_t if isinstance(compl_t, int) else None,
            request_id=req_id, latency_s=latency, attempts=attempt,
            json_mode=mode, estimated_prompt_tokens=estimated,
        )
    assert last_error is not None
    raise last_error
