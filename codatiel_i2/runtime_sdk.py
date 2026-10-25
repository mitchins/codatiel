"""Canonical Codatiel I2 agent runtime on the OpenAI Agents SDK.

One product, one agent runtime: an SDK ``Agent`` + ``Runner.run`` execution in
which the model directs investigation through SDK-exposed I2 source tools
against pinned BASE/HEAD checkouts. Domain behavior (revision-bound evidence,
replay validation, finding disposition) is inherited from
``codatiel_i2.core`` / ``codatiel_i2.tools`` — this module only adapts them to
the SDK's tool protocol and provides the three model backends plus a scripted
model for mock-inference smoke tests.

Status notes (see AGENTS.md):
- SDK cloud tracing is disabled on every entry point; traces stay local.
- This module performs no paid inference by itself; backends need keys/endpoints.
- ``ScriptedModel`` drives the real tool functions against real checkouts but
  its outputs are runtime smoke signals, never review results.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codatiel_i2.core.spend import SpendCeilingExceeded, SpendLedger


def require_sdk() -> Any:
    """Import the Agents SDK lazily so SDK-free environments still import I2."""
    try:
        import agents  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "openai-agents is not installed; SDK-runtime tests require "
            ".venv-codatiel-agents"
        ) from exc
    return agents


try:
    from agents.models.interface import Model as _SDKModel
except Exception:  # SDK not installed; require_sdk() guards actual use
    _SDKModel = object  # type: ignore[assignment,misc]


def ensure_local_tracing() -> None:
    """Disable SDK cloud tracing. Forensic traces stay local, always."""
    agents = require_sdk()
    agents.set_tracing_disabled(True)


@dataclass
class ReviewContext:
    """Pinned revisions + materialised checkouts one review runs against."""

    repo: str
    base_sha: str
    head_sha: str
    base_dir: Path
    head_dir: Path
    diff_repo: Path  # git repo holding both SHAs (for diff_window)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    tool_budget: int = 40  # max dispatched tool calls per run; over-budget
    # calls fail loudly with an error payload, never silently dropped
    budget_used: int = 0  # slots reserved by pre-dispatch checks
    budget_lock: Any = field(default_factory=threading.Lock, repr=False)


def _record(ctx: ReviewContext, name: str, args: dict[str, Any], result: dict) -> str:
    if len(ctx.tool_log) >= ctx.tool_budget:
        return json.dumps({"status": "error",
                           "error": f"tool budget exhausted ({ctx.tool_budget} calls); "
                                    "ending investigation within bounded resources"})
    summary = {k: result.get(k) for k in ("truncated", "total_lines", "binary", "status")
               if k in result}
    ev = result.get("evidence")
    items = ev if isinstance(ev, list) else [ev]
    ids = [e.get("evidence_id") for e in items
           if isinstance(e, dict) and e.get("evidence_id")]
    if ids:
        summary["evidence_ids"] = ids
    revs = {e.get("revision_sha") for e in items
            if isinstance(e, dict) and e.get("revision_sha")}
    if revs:
        summary["revision_shas"] = sorted(revs)
    ctx.tool_log.append({"tool": name, "args": args, "summary": summary})
    return json.dumps(result, default=str)


def _over_budget(ctx: ReviewContext) -> str | None:
    """Pre-dispatch budget gate: reserve one slot atomically, or refuse.

    The SDK may dispatch emitted tool calls concurrently on worker threads,
    so the check-and-reserve must be atomic: otherwise two calls near the
    limit could both pass and both run. `_record` keeps its post-dispatch
    check as the backstop.
    """
    with ctx.budget_lock:
        if ctx.budget_used >= ctx.tool_budget or len(ctx.tool_log) >= ctx.tool_budget:
            return json.dumps({"status": "error",
                               "error": f"tool budget exhausted ({ctx.tool_budget} calls); "
                                        "ending investigation within bounded resources"})
        ctx.budget_used += 1
    return None



def build_i2_tools(ctx: ReviewContext) -> list[Any]:
    """Expose I2 revision-bound source tools as SDK function tools."""
    agents = require_sdk()
    from codatiel_i2.tools.diff_window import diff_window as i2_diff_window
    from codatiel_i2.tools.lookup_docs import lookup_docs as i2_lookup_docs
    from codatiel_i2.tools.read_file import read_file as i2_read_file
    from codatiel_i2.tools.related_files import related_files as i2_related_files
    from codatiel_i2.tools.search import search as i2_search
    from codatiel_i2.tools.symbols import find_callers as i2_find_callers
    from codatiel_i2.tools.symbols import find_definition as i2_find_definition

    @agents.function_tool
    def read_file(path: str, revision: str = "head",
                  start_line: int = 1, end_line: int = 0) -> str:
        """Read exact source lines from the pinned BASE or HEAD checkout."""
        checkout = ctx.base_dir if revision == "base" else ctx.head_dir
        args = {"path": path, "revision": revision,
                "start_line": start_line, "end_line": end_line or None}
        blocked = _over_budget(ctx)
        if blocked is not None:
            return _record(ctx, "read_file", args, json.loads(blocked))
        try:
            result = i2_read_file(checkout, path, base_sha=ctx.base_sha,
                                  head_sha=ctx.head_sha, revision=revision,
                                  start_line=start_line,
                                  end_line=end_line or None)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return _record(ctx, "read_file", args, result)

    @agents.function_tool
    def diff_window(path: str, context_lines: int = 40) -> str:
        """Return the unified BASE..HEAD diff for one path with evidence anchor."""
        args = {"path": path, "context_lines": context_lines}
        blocked = _over_budget(ctx)
        if blocked is not None:
            return _record(ctx, "diff_window", args, json.loads(blocked))
        try:
            result = i2_diff_window(ctx.diff_repo, ctx.base_sha, ctx.head_sha,
                                    path, context_lines=context_lines)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return _record(ctx, "diff_window", args, result)

    @agents.function_tool
    def search_code(query: str, revision: str = "head",
                    max_results: int = 50) -> str:
        """Path-confined code search over the pinned BASE or HEAD checkout."""
        checkout = ctx.base_dir if revision == "base" else ctx.head_dir
        args = {"query": query, "revision": revision, "max_results": max_results}
        blocked = _over_budget(ctx)
        if blocked is not None:
            return _record(ctx, "search_code", args, json.loads(blocked))
        try:
            result = i2_search(checkout, query, base_sha=ctx.base_sha,
                               head_sha=ctx.head_sha, revision=revision,
                               max_results=max_results)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return _record(ctx, "search_code", args, result)

    @agents.function_tool
    def find_definition(symbol: str, revision: str = "head") -> str:
        """Find the definition of a Python symbol (classes/functions) in the
        pinned checkout. Python AST only; other languages unsupported."""
        checkout = ctx.base_dir if revision == "base" else ctx.head_dir
        args = {"symbol": symbol, "revision": revision}
        blocked = _over_budget(ctx)
        if blocked is not None:
            return _record(ctx, "find_definition", args, json.loads(blocked))
        try:
            result = i2_find_definition(
                checkout, symbol, base_sha=ctx.base_sha,
                head_sha=ctx.head_sha, revision=revision)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return _record(ctx, "find_definition", args, result)

    @agents.function_tool
    def find_callers(symbol: str, revision: str = "head") -> str:
        """Find call sites matching `symbol(` in the pinned checkout.
        Call syntax only: constant reads, imports, attribute uses without a
        call, and membership tests are NOT returned (an empty result never
        means 'no references'). Use search_code for those. Any hit must be
        confirmed with read_file before being relied on."""
        checkout = ctx.base_dir if revision == "base" else ctx.head_dir
        args = {"symbol": symbol, "revision": revision}
        blocked = _over_budget(ctx)
        if blocked is not None:
            return _record(ctx, "find_callers", args, json.loads(blocked))
        try:
            result = i2_find_callers(
                checkout, symbol, base_sha=ctx.base_sha,
                head_sha=ctx.head_sha, revision=revision)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return _record(ctx, "find_callers", args, result)

    @agents.function_tool
    def related_files(path: str) -> str:
        """Heuristic related-file suggestions (same dir, name overlap, same
        extension) for the HEAD checkout. No evidence attached; confirm with
        read_file. Navigation aid only, never a finding source."""
        args = {"path": path}
        blocked = _over_budget(ctx)
        if blocked is not None:
            return _record(ctx, "related_files", args, json.loads(blocked))
        try:
            result = i2_related_files(ctx.head_dir, path)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return _record(ctx, "related_files", args, result)

    @agents.function_tool
    def lookup_docs(query: str, package: str = "",
                    version: str = "") -> str:
        """External vendor documentation from the local cache only. Web
        fallback is disabled; project docs must be read with read_file/
        search_code at an explicit revision. Returns not_found without a
        configured vendor cache."""
        import os
        args = {"query": query, "package": package, "version": version}
        blocked = _over_budget(ctx)
        if blocked is not None:
            return _record(ctx, "lookup_docs", args, json.loads(blocked))
        try:
            result = i2_lookup_docs(
                query, package=package or None, version=version or None,
                vendor_cache_dir=os.environ.get("CODATIEL_VENDOR_CACHE_DIR"))
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return _record(ctx, "lookup_docs", args, result)

    return [read_file, diff_window, search_code, find_definition,
            find_callers, related_files, lookup_docs]


class ScriptedModel(_SDKModel):
    """Scripted SDK Model for mock-inference runtime smoke tests.

    Returns canned responses in order; every tool call is still dispatched by
    ``Runner`` to the real tool functions. Not a reviewer; proves the runtime
    loop (offer -> request -> execute -> continue) works end to end.
    """

    is_scripted = True  # mechanical non-evidence marker for downstream consumers

    def __init__(self, script: list[Any]):
        require_sdk()
        self._script = list(script)
        self.calls = 0

    async def get_response(self, system_instructions, input, model_settings,
                           tools, output_schema, handoffs, tracing, *,
                           previous_response_id, conversation_id,
                           prompt) -> Any:
        agents = require_sdk()
        _ = (system_instructions, input, model_settings, tools, output_schema,
             handoffs, tracing, previous_response_id, conversation_id, prompt)
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        from agents.items import ModelResponse
        from agents.usage import Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText
        return ModelResponse(
            output=[ResponseOutputMessage(
                id=f"msg_fallback_{self.calls}", status="completed", role="assistant",
                content=[ResponseOutputText(
                    text="SCRIPT_EXHAUSTED: silence", type="output_text",
                    annotations=[])],
                type="message")],
            usage=Usage(), response_id=f"scripted-fallback-{self.calls}")

    async def close(self) -> None:
        return None

    async def stream_response(self, system_instructions, input, model_settings,
                              tools, output_schema, handoffs, tracing, *,
                              previous_response_id, conversation_id, prompt):
        from agents.stream_events import RawResponsesStreamEvent
        response = await self.get_response(
            system_instructions, input, model_settings, tools, output_schema,
            handoffs, tracing, previous_response_id=previous_response_id,
            conversation_id=conversation_id, prompt=prompt)
        yield RawResponsesStreamEvent(data={"type": "response.completed",
                                            "response": {"id": response.response_id}})


class UsageRecordingModel(_SDKModel):
    """Record each completed provider response in a spend ledger.

    The wrapper forwards every SDK-model method unchanged. If a later tool,
    turn, or budget failure aborts the run, responses recorded before that
    failure remain in the ledger. Reconciliation stays idempotent because the
    ledger deduplicates by provider response ID.
    """

    def __init__(self, model: Any, ledger: SpendLedger | None = None):
        if isinstance(model, UsageRecordingModel):
            raise ValueError("model is already usage-recorded")
        if isinstance(model, str):
            # Native SDK string model ids (e.g. the Luna backend) must be
            # resolved to Model objects: the recording wrapper can only
            # forward to a real Model.
            require_sdk()
            from agents.models.openai_provider import OpenAIProvider

            model = OpenAIProvider().get_model(model)
        self._model = model
        self.ledger = ledger if ledger is not None else SpendLedger()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._model.get_response(*args, **kwargs)
        try:
            usage = getattr(response, "usage", None)
            cached = write_tokens = reasoning = None
            details = getattr(usage, "input_tokens_details", None) \
                if usage is not None else None
            if ((getattr(self._model, "provider_usage_details", False)
                    or details is not None)
                    and usage is not None):
                # Provider-reported token details only: either the adapter
                # marks them as sourced from the provider payload, or the
                # usage object structurally carries provider-mapped details
                # (native SDK models). Never inferred, never defaulted:
                # absent details stay None, not zero.
                try:
                    cached = details.cached_tokens
                except (AttributeError, TypeError):
                    cached = None
                try:
                    write_tokens = details.cache_write_tokens
                except (AttributeError, TypeError):
                    write_tokens = None
                try:
                    reasoning = usage.output_tokens_details.reasoning_tokens
                except (AttributeError, TypeError):
                    reasoning = None
            # Adapter-published provider split (e.g. Haiku, whose cache
            # fields cannot ride on SDK usage types). The adapter saw the
            # provider payload, so its values are authoritative and fill
            # whatever the generic details read left empty.
            cache_in_input = None
            try:
                details_fn = getattr(self._model, "usage_details_for",
                                     None)
                adapter = details_fn(
                    getattr(response, "response_id", None)) \
                    if callable(details_fn) else None
            except Exception:
                adapter = None
            if isinstance(adapter, dict):
                if adapter.get("cached_input_tokens") is not None:
                    cached = adapter["cached_input_tokens"]
                if adapter.get("cache_write_input_tokens") is not None:
                    write_tokens = adapter["cache_write_input_tokens"]
                if adapter.get("cache_in_input") is not None:
                    cache_in_input = adapter["cache_in_input"]
            added = self.ledger.record_response(
                getattr(response, "response_id", None), usage,
                cached_input_tokens=cached,
                cache_write_input_tokens=write_tokens,
                cache_in_input=cache_in_input,
                reasoning_output_tokens=reasoning)
            if added:
                unknown_ids = getattr(self._model, "_unknown_usage_response_ids", ())
                if getattr(response, "response_id", None) in (unknown_ids or ()):
                    self.ledger.mark_unknown(getattr(response, "response_id"))
            confirmed = self.ledger.confirmed_usd()
            if confirmed is not None and confirmed > self.ledger.ceiling_usd:
                raise SpendCeilingExceeded(
                    f"confirmed spend ${confirmed:.4f} exceeds ceiling "
                    f"${self.ledger.ceiling_usd:.2f}; aborting run with partial ledger")
        except SpendCeilingExceeded:
            raise
        except Exception:
            # Accounting must never alter review behaviour. The response is
            # still returned; its absence from the ledger is fail-closed and
            # will surface as unknown usage at reconciliation.
            self.ledger.note_recording_error()
        return response

    async def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        from agents.stream_events import RawResponsesStreamEvent

        # Route streaming through the recording response path so streamed
        # executions retain the same per-response ledger as direct calls.
        response = await self.get_response(*args, **kwargs)
        yield RawResponsesStreamEvent(data={"type": "response.completed",
                                            "response": {"id": response.response_id}})

    async def close(self) -> Any:
        closer = getattr(self._model, "close", None)
        if closer is None:
            return None
        return await closer()


def scripted_tool_call(*, call_id: str, name: str, arguments: dict[str, Any],
                       response_id: str) -> Any:
    """Build one scripted tool-call turn for ``ScriptedModel``."""
    from agents.items import ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall
    return ModelResponse(
        output=[ResponseFunctionToolCall(
            id=call_id, call_id=call_id, name=name,
            arguments=json.dumps(arguments), type="function_call")],
        usage=Usage(), response_id=response_id)


def scripted_final(text: str, *, response_id: str) -> Any:
    """Build one scripted final-message turn for ``ScriptedModel``."""
    from agents.items import ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText
    return ModelResponse(
        output=[ResponseOutputMessage(
            id=f"msg_{response_id}", status="completed", role="assistant",
            content=[ResponseOutputText(
                text=text, type="output_text", annotations=[])],
            type="message")],
        usage=Usage(), response_id=response_id)


def gemma_model(*, model: str, base_url: str | None = None,
                api_key: str | None = None) -> Any:
    """Local Gemma backend: OpenAI-compatible endpoint via the SDK's chat model."""
    import os
    ensure_local_tracing()
    agents = require_sdk()
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        raise RuntimeError("openai package required for the Gemma backend") from exc
    base = (base_url or os.environ.get("GEMMA_BASE_URL")
            or "http://192.168.1.14:8000/v1").rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    key = api_key or os.environ.get("GEMMA_API_KEY") or "local"
    client = AsyncOpenAI(base_url=base, api_key=key)
    return OpenAIChatCompletionsModel(model=model, openai_client=client)


def deepseek_model(*, model: str = "deepseek-flash",
                   base_url: str | None = None,
                   api_key: str | None = None) -> Any:
    """Build the DeepSeek Flash backend (M10 primary: thinking enabled).

    M10 primary configuration (recorded 2026-10-23, before any evaluation
    run): thinking ENABLED (explicit ``extra_body`` toggle, provider
    default), default effort. Rationale: provider default (no tuning);
    the installed SDK replays ``reasoning_content`` for deepseek models,
    which thinking mode with tools requires (else HTTP 400); the M9.2
    escalation hypothesis is reasoning-side, making the thinking variant
    the theoretically interesting challenger; parity with Haiku's adaptive
    thinking. If thinking mode proves incompatible, fall back to
    non-thinking and record the switch as a compatibility finding.

    ``provider_usage_details`` marks SDK Usage token details as
    provider-reported: DeepSeek returns ``prompt_tokens_details``
    (cached tokens) and ``completion_tokens_details`` (reasoning tokens)
    in chat-completion usage (verified by raw probe in M10 smoke test), so
    the spend ledger may record them as reported — never as inferred.
    ``reasoning_content`` text is counted via reasoning tokens, never
    stored: it is deliberation, not review evidence.
    """
    import os
    ensure_local_tracing()
    require_sdk()
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        raise RuntimeError("openai package required for the DeepSeek backend") from exc
    base = (base_url or os.environ.get("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com").rstrip("/")
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    client = AsyncOpenAI(base_url=base, api_key=key)

    class DeepSeekModel(OpenAIChatCompletionsModel):
        provider_usage_details = True

    return DeepSeekModel(model=model, openai_client=client)


def deepseek_model_settings() -> Any:
    """ModelSettings for DeepSeek: explicit thinking toggle + output bound.

    Thinking enabled explicitly (not relied on as a silent default);
    ``max_tokens=8192`` bounds output tokens per request (parity with the
    Haiku adapter). No temperature/top_p: thinking mode ignores them.
    """
    agents = require_sdk()
    from agents.model_settings import ModelSettings
    return ModelSettings(max_tokens=8192,
                         extra_body={"thinking": {"type": "enabled"}})


def glm_model(*, model: str = "glm-5.3-flash",
              base_url: str | None = None,
              api_key: str | None = None) -> Any:
    """Build the GLM flash backend (M10 primary: forced thinking, preserved).

    M10-GLM primary configuration (recorded 2026-10-23, before any GLM
    evaluation run): thinking ENABLED + ``clear_thinking: false``
    (Preserved Thinking, the provider's recommendation for coding/agent
    scenarios), reasoning_effort unset (provider default). Rationale:
    thinking is forced-on for glm-5.3-flash (cannot be disabled per
    provider docs); preserved thinking requires the complete unmodified
    reasoning_content back with tool results, so the adapter enables the
    SDK's reasoning replay for glm models (same mechanism the SDK uses
    for deepseek). ``reasoning_content`` text is counted via reasoning
    tokens, never stored.
    """
    import os
    ensure_local_tracing()
    require_sdk()
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        raise RuntimeError("openai package required for the GLM backend") from exc
    # Default: Z.AI Coding Plan subscription endpoint (OpenAI Chat
    # Completions format). The general pay-as-you-go endpoint
    # (https://api.z.ai/api/paas/v4) does NOT consume plan quota.
    base = (base_url or os.environ.get("ZAI_BASE_URL")
            or "https://api.z.ai/api/coding/paas/v4").rstrip("/")
    key = api_key or os.environ.get("ZAI_API_KEY")
    if not key:
        raise RuntimeError("ZAI_API_KEY is not set")
    client = AsyncOpenAI(base_url=base, api_key=key)

    def _replay_glm(context: Any) -> bool:
        return "glm" in str(getattr(context, "model", "") or "").lower()

    class GLMModel(OpenAIChatCompletionsModel):
        # Raw probe 2026-10-23 (coding endpoint, fresh key): usage
        # carries prompt_tokens_details.cached_tokens and
        # completion_tokens_details.reasoning_tokens — recorded as
        # provider-reported, never inferred.
        provider_usage_details = True

    return GLMModel(model=model, openai_client=client,
                    should_replay_reasoning_content=_replay_glm)


def glm_model_settings() -> Any:
    """ModelSettings for GLM: preserved thinking + output bound.

    ``max_tokens=8192`` bounds output tokens per request (parity with the
    Haiku/DeepSeek adapters). No temperature/top_p; reasoning_effort
    unset (provider default).
    """
    agents = require_sdk()
    from agents.model_settings import ModelSettings
    return ModelSettings(
        max_tokens=8192,
        extra_body={"thinking": {"type": "enabled",
                                 "clear_thinking": False}})


class HaikuModel(_SDKModel):
    """Claude Haiku 5.5 backend: native Anthropic Messages API as an SDK Model.

    Translates the SDK tool list to Anthropic tool schemas and maps text /
    tool_use blocks back to SDK response items. Provider-private thinking and
    redacted blocks are counted, never claimed as visible reasoning: only
    provider-exposed text is recorded.
    """

    def __init__(self, *, model: str = "claude-haiku-5-5",
                 max_tokens: int = 8192,
                 thinking: dict[str, Any] | None = None,
                 output_config: dict[str, Any] | None = None) -> None:
        ensure_local_tracing()
        require_sdk()
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except Exception as exc:
            raise RuntimeError("anthropic package required for the Haiku backend") from exc
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = AsyncAnthropic()
        self._model = model
        self._max_tokens = max_tokens
        self._thinking = thinking if thinking is not None else {"type": "adaptive"}
        self._output_config = output_config or {"effort": "medium"}
        self.thinking_blocks_seen = 0
        # Thinking blocks Anthropic returned but the SDK item protocol cannot
        # carry. Retained here grouped by the producing response, and
        # re-emitted once before the first historical fragment of that
        # response's tool_use so multi-turn continuation stays
        # protocol-valid. Never recorded as visible reasoning: only
        # provider-exposed text leaves this adapter. Instances are
        # single-run: build one HaikuModel per review.
        self._thinking_groups: list[dict[str, Any]] = []
        # Provider-reported usage splits by response ID (cache reads and
        # cache writes). Plain dicts carried for the recording wrapper;
        # never inferred, never forced into SDK usage types.
        self._usage_details: dict[str, dict[str, Any]] = {}
        # Provider responses that arrived with no usable usage payload. The
        # spend ledger must treat these as unknown, never as confirmed zero.
        self._unknown_usage_response_ids: set[str] = set()
        # Provider termination reasons per response_id (end_turn, tool_use,
        # max_tokens, etc.) — recorded as local provider metadata, not SDK
        # state. Distinguishes: completed with final, issued tool calls,
        # reached output-token limit, returned empty.
        self._termination_by_response: dict[str, str] = {}

    def _sdk_tools_to_anthropic(self, tools: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tool in tools or []:
            params = getattr(tool, "params_json_schema", None) or {"type": "object"}
            out.append({"name": tool.name,
                        "description": tool.description or tool.name,
                        "input_schema": params})
        return out

    async def get_response(self, system_instructions, input, model_settings,
                           tools, output_schema, handoffs, tracing, *,
                           previous_response_id, conversation_id,
                           prompt) -> Any:
        from agents.items import ModelResponse
        from agents.usage import Usage
        from openai.types.responses import (
            ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText,
        )
        system_text, messages = self._input_to_anthropic(input)
        system = "\n\n".join([s for s in [system_instructions or "", system_text]
                               if s])
        kwargs: dict[str, Any] = {
            "model": self._model, "max_tokens": self._max_tokens,
            "messages": messages,
        }
        # Prompt-caching breakpoints (CACHE-01): Anthropic caches the full
        # prefix (tools, system, messages) up to and including the
        # designated block. Markers are metadata only — message content,
        # order and tool definitions are byte-identical either way.
        # Default 5-minute TTL (no extended retention); refreshed on hit.
        if system:
            kwargs["system"] = [{"type": "text", "text": system,
                                 "cache_control": {"type": "ephemeral"}}]
        else:
            kwargs["system"] = system
        anthropic_tools = self._sdk_tools_to_anthropic(tools)
        if anthropic_tools:
            anthropic_tools[-1] = {**anthropic_tools[-1],
                                   "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = anthropic_tools
        if messages:
            _mark_last_message_cacheable(messages)
        if self._thinking:
            kwargs["thinking"] = self._thinking
        kwargs["output_config"] = self._output_config
        try:
            resp = await self._client.messages.create(**kwargs)
        except Exception as exc:
            # Narrow single fallback: only an explicit output_config rejection
            # retries without it. Every other failure surfaces, never retries.
            if "output_config" not in str(exc).lower():
                raise
            kwargs.pop("output_config", None)
            resp = await self._client.messages.create(**kwargs)
        # Preserve provider termination reason (end_turn, tool_use, max_tokens,
        # etc.) as local metadata. Do not infer max_tokens from usage.
        stop_reason = getattr(resp, "stop_reason", None)
        if isinstance(stop_reason, str) and stop_reason:
            self._termination_by_response[str(resp.id)] = stop_reason
        thinking_here: list[dict[str, Any]] = []
        texts: list[str] = []
        output: list[Any] = []
        tool_ids_here: list[str] = []
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                texts.append(block.text)
            elif btype == "thinking":
                self.thinking_blocks_seen += 1
                retained = _retain_thinking_block(
                    "thinking", getattr(block, "thinking", ""),
                    getattr(block, "signature", ""))
                if retained is not None:
                    thinking_here.append(retained)
            elif btype == "redacted_thinking":
                self.thinking_blocks_seen += 1
                thinking_here.append({"type": "redacted_thinking",
                                      "data": block.data})
            elif btype == "tool_use":
                tool_ids_here.append(block.id)
                output.append(ResponseFunctionToolCall(
                    id=block.id, call_id=block.id, name=block.name,
                    arguments=json.dumps(block.input), type="function_call"))
        if thinking_here and tool_ids_here and self._thinking:
            # One group per response: fragments of a parallel tool call share
            # it, and re-emission happens once before the first fragment.
            self._thinking_groups.append({"ids": list(tool_ids_here),
                                          "thinking": thinking_here})
        if texts:
            output.append(ResponseOutputMessage(
                id=f"msg_{resp.id}", status="completed", role="assistant",
                content=[ResponseOutputText(
                    text="\n\n".join(texts), type="output_text", annotations=[])],
                type="message"))
        usage = getattr(resp, "usage", None)
        sdk_usage = Usage()
        if usage is None:
            # The provider returned a response with no usable usage payload.
            # Record the response ID as unknown rather than minting zeros that
            # downstream accounting could mistake for confirmed zero usage.
            self._unknown_usage_response_ids.add(resp.id)
            sdk_usage = Usage(requests=1, input_tokens=0, output_tokens=0, total_tokens=0)
        else:
            # Anthropic's input_tokens already includes cache creation and
            # cache read tokens (they are sub-counts, not extras): report it
            # unchanged and carry the split separately. Never default a
            # missing split to zero — absent stays None.
            cache_write = getattr(usage, "cache_creation_input_tokens", None)
            cache_read = getattr(usage, "cache_read_input_tokens", None)
            sdk_usage = Usage(
                requests=1,
                input_tokens=(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                total_tokens=0)
            sdk_usage.total_tokens = sdk_usage.input_tokens + sdk_usage.output_tokens
            # Provider-sourced cache split, keyed by response ID for the
            # recording wrapper (see usage_details_for). Kept OFF the SDK
            # Usage object: its input_tokens_details type cannot express a
            # write split and must stay SDK-compatible.
            self._usage_details[resp.id] = {
                # Anthropic input_tokens counts only uncached tokens; cache
                # reads and writes are separate billed lines (verified by
                # live probe: input stayed ~4 while 18K tokens were
                # cache-written and cache-read).
                "cached_input_tokens": cache_read,
                "cache_write_input_tokens": cache_write,
                "cache_in_input": False}
        return ModelResponse(output=output, usage=sdk_usage, response_id=resp.id)

    def usage_details_for(self, response_id: str | None) -> dict[str, Any] | None:
        """Provider-reported cache split for one response, if recorded."""
        if response_id is None:
            return None
        return self._usage_details.get(str(response_id))

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass

    async def stream_response(self, system_instructions, input, model_settings,
                              tools, output_schema, handoffs, tracing, *,
                              previous_response_id, conversation_id, prompt):
        from agents.stream_events import RawResponsesStreamEvent
        response = await self.get_response(
            system_instructions, input, model_settings, tools, output_schema,
            handoffs, tracing, previous_response_id=previous_response_id,
            conversation_id=conversation_id, prompt=prompt)
        yield RawResponsesStreamEvent(data={"type": "response.completed",
                                            "response": {"id": response.response_id}})


    def _input_to_anthropic(
            self, input: Any) -> tuple[str, list[dict[str, Any]]]:
        """Map SDK input to (system_extra, messages), re-emitting each
        response's retained thinking once before the first historical
        fragment of that response's tool_use. Parallel tool fragments share
        one emission, so no signed block is duplicated or interleaved."""
        system_text, messages = _responses_input_to_anthropic(input)
        if not self._thinking_groups or not self._thinking:
            return system_text, messages
        id_to_group: dict[str, int] = {}
        for gi, group in enumerate(self._thinking_groups):
            for tool_id in group["ids"]:
                id_to_group.setdefault(tool_id, gi)
        emitted: set[int] = set()
        for message in messages:
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            ids = [c.get("id") for c in content
                   if isinstance(c, dict) and c.get("type") == "tool_use"]
            prepend: list[dict[str, Any]] = []
            for tool_id in ids:
                gi = id_to_group.get(tool_id)
                if gi is not None and gi not in emitted:
                    emitted.add(gi)
                    prepend.extend(self._thinking_groups[gi]["thinking"])
            if prepend:
                message["content"] = prepend + [c for c in content
                                               if not (isinstance(c, dict) and c.get(
                                                   "type") in ("thinking",
                                                               "redacted_thinking"))]
        return system_text, messages


def _responses_input_to_anthropic(input: Any) -> tuple[str, list[dict[str, Any]]]:
    """Map SDK Responses-format input items to (system_extra, messages).

    Item ``type`` is matched before any ``role`` default: ``function_call``
    becomes an assistant ``tool_use`` block and ``function_call_output`` a
    ``tool_result`` user block, so tool results always reach the model.
    """
    if isinstance(input, str):
        return "", [{"role": "user", "content": input}]
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for item in (list(input) if input else []):
        if not isinstance(item, dict):
            content = getattr(item, "content", None)
            messages.append({"role": "user", "content": str(content or item)})
            continue
        itype = item.get("type", "")
        if itype == "function_call":
            try:
                tool_input = json.loads(item.get("arguments") or "{}")
            except ValueError:
                tool_input = {"_raw_arguments": item.get("arguments") or ""}
            messages.append({"role": "assistant", "content": [{
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id") or "",
                "name": item.get("name") or "",
                "input": tool_input,
            }]})
        elif itype == "function_call_output":
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("call_id") or "",
                "content": str(item.get("output", "")),
            }]})
        elif itype == "message":
            role = item.get("role", "user")
            text = _message_text(item.get("content", ""))
            if role == "assistant":
                messages.append({"role": "assistant", "content": text})
            elif role == "system":
                system_parts.append(text)
            else:
                messages.append({"role": "user", "content": text})
        elif "role" in item:
            # Older/plain shapes: honour the explicit role, never default
            # tool-protocol items to user.
            role = item.get("role", "user")
            content = item.get("content", "")
            text = _message_text(content) if isinstance(content, list) else str(content)
            if role == "assistant":
                messages.append({"role": "assistant", "content": text})
            elif role == "system":
                system_parts.append(text)
            else:
                messages.append({"role": "user", "content": text})
        # Anything else (reasoning items, handoffs, approvals) is dropped:
        # it carries no content the Messages API can consume.
    if not messages:
        messages.append({"role": "user", "content": ""})
    for message in messages:
        if message.get("role") == "assistant" and isinstance(
                message.get("content"), list):
            message["content"] = _order_assistant_blocks(
                message["content"], "assistant")
    return "\n\n".join(p for p in system_parts if p), _merge_same_role(messages)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = [p.get("text", "") for p in content
             if isinstance(p, dict) and p.get("type") in
             ("input_text", "output_text", "text")]
    return "\n".join(p for p in parts if p)


def _mark_last_message_cacheable(messages: list[dict[str, Any]]) -> None:
    """Attach an ephemeral cache breakpoint to the final message.

    The marker moves forward each turn, so every request extends the
    cached prefix by one message while earlier content keeps hitting.
    Content is untouched — only the cache_control marker is added.
    At most one message carries the tail marker per request.
    """
    if not messages:
        return
    last = messages[-1]
    content = last.get("content")
    marker = {"type": "ephemeral"}
    if isinstance(content, str):
        if not content:
            return
        last["content"] = [{"type": "text", "text": content,
                            "cache_control": marker}]
        return
    if isinstance(content, list) and content:
        tail = content[-1]
        if isinstance(tail, dict):
            tail = dict(tail)
            tail["cache_control"] = marker
            last["content"] = content[:-1] + [tail]
        return
    # Non-text final content (images etc.): leave unmarked rather than
    # reshaping provider payloads.


def _retain_thinking_block(kind: str, thinking: str,
                           signature: str) -> dict[str, Any] | None:
    """Decide whether a thinking block is re-emitted for tool continuation.

    Blocks with empty reasoning content are dropped: they carry no thought
    for the model to stay coherent with, and re-emitting them trips provider
    validation on this backend (400 on multi-turn tool flows). Non-empty
    thoughts are re-emitted unmodified with their signatures.
    """
    if kind == "thinking" and not (thinking or "").strip():
        return None
    return {"type": "thinking", "thinking": thinking, "signature": signature}


def _order_assistant_blocks(blocks: list[dict[str, Any]],
                            role: str | None) -> list[dict[str, Any]]:
    """Order assistant content blocks thinking, text, then tool_use."""
    if role != "assistant":
        return blocks
    thinking = [b for b in blocks if isinstance(b, dict) and b.get("type")
                in ("thinking", "redacted_thinking")]
    text = [b for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    rest = [b for b in blocks if not (isinstance(b, dict) and b.get("type")
                                      in ("thinking", "redacted_thinking",
                                          "text"))]
    return thinking + text + rest


def _merge_same_role(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role messages.

    The SDK emits an assistant text message and assistant tool_use as
    separate turns; the Messages API requires strict user/assistant
    alternation, so consecutive same-role fragments must be one message.
    Thinking blocks stay before the tool_use blocks they belong to.
    Within an assistant message, blocks are ordered thinking, then text,
    then tool_use: with reasoning enabled the provider misreads trailing
    text after tool_use as a prefill attempt (400).
    """
    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1].get("role") == message.get("role"):
            prev = merged[-1]
            prev_content = prev.get("content")
            cur_content = message.get("content")
            if isinstance(prev_content, str) and isinstance(cur_content, str):
                prev["content"] = (prev_content + "\n\n" + cur_content).strip()
            elif isinstance(prev_content, list) and isinstance(cur_content, list):
                prev["content"] = _order_assistant_blocks(
                    prev_content + cur_content, role=merged[-1].get("role"))
            elif isinstance(prev_content, str) and isinstance(cur_content, list):
                prev["content"] = ([{"type": "text", "text": prev_content}]
                                   if prev_content else []) + cur_content
            elif isinstance(prev_content, list) and isinstance(cur_content, str):
                prev["content"] = prev_content + ([{"type": "text", "text": cur_content}]
                                                  if cur_content else [])
            else:
                merged.append(message)
        else:
            merged.append(message)
    for message in merged:
        if message.get("role") == "assistant" and isinstance(
                message.get("content"), list):
            message["content"] = _order_assistant_blocks(
                message["content"], "assistant")
    return merged


def run_review(*, instructions: str, user_input: str, tools: list[Any],
               model: Any, max_turns: int = 12,
               model_settings: Any = None,
               usage_ledger: SpendLedger | None = None) -> Any:
    """Run one bounded agentic review through the canonical SDK runtime."""
    ensure_local_tracing()
    agents = require_sdk()
    if model_settings is None:
        from agents.model_settings import ModelSettings
        model_settings = ModelSettings()
    if isinstance(model, UsageRecordingModel):
        active_model = model
    else:
        if isinstance(model, str):
            # Native SDK string model ids (e.g. the Luna backend) must be
            # resolved to Model objects before wrapping: the spend ledger's
            # recording wrapper can only forward to a real Model.
            from agents.models.openai_provider import OpenAIProvider

            model = OpenAIProvider().get_model(model)
        active_model = UsageRecordingModel(model, usage_ledger)
    agent = agents.Agent(name="codatiel-i2", instructions=instructions,
                         tools=tools, model=active_model, model_settings=model_settings)
    result = asyncio.run(
        agents.Runner.run(agent, user_input, max_turns=max_turns))
    # M9.2 P1: capture the exact instructions object the SDK received, at
    # the SDK boundary, so the run record can prove its stored hash matches
    # what was actually passed to Agent — not just what the service claims.
    try:
        import hashlib as _hashlib
        result.sdk_boundary_instructions_sha256 = _hashlib.sha256(
            str(agent.instructions).encode("utf-8")).hexdigest()
    except Exception:
        pass
    return result


def luna_model_id() -> str:
    """Luna backend: native SDK OpenAI provider; model id only, no adapter.

    Requires ``OPENAI_API_KEY`` from the Code Review project
    (STANDARD_API_NO_TRAINING). Returned as a plain model id for
    ``Agent(model=...)`` with the SDK default provider. Always pair with
    ``luna_model_settings()`` so ``store=false`` is enforced, not asserted.
    """
    return "gpt-6-luna"


def prompt_cache_key_for(*, repo: str, base_sha: str, head_sha: str,
                           instructions_id: str, model_name: str) -> str:
    """Stable per-review OpenAI prompt-cache partition.

    The agents SDK otherwise mints a fresh ``prompt_cache_key`` per
    Runner invocation, which partitions every turn of a review into its
    own cache namespace and guarantees a zero hit rate. A key derived
    from (repo, base, head, instructions, model) is stable across one
    review's turns and distinct across reviews — identical evidence
    reuses computation, different evidence never collides. Opaque digest
    only; no paths or SHAs leak into provider metadata.
    """
    import hashlib as _hashlib
    digest = _hashlib.sha256(
        "\n".join([repo or "", base_sha or "", head_sha or "",
                   instructions_id or "", model_name or ""]).encode("utf-8")
    ).hexdigest()[:32]
    return "codatiel-i2-" + digest


def with_prompt_cache_key(model_settings: Any, key: str | None) -> Any:
    """Return model settings carrying a user-supplied prompt_cache_key.

    The runner forwards ``extra_args["prompt_cache_key"]`` to the model
    adapter instead of generating a per-run key. Metadata only: request
    content, tools, instructions and review semantics are untouched.
    """
    if not key or model_settings is None:
        return model_settings
    import dataclasses as _dataclasses
    try:
        extra = dict(getattr(model_settings, "extra_args", None) or {})
    except Exception:
        return model_settings
    if extra.get("prompt_cache_key"):
        return model_settings
    extra["prompt_cache_key"] = key
    try:
        return _dataclasses.replace(model_settings, extra_args=extra)
    except Exception:
        return model_settings


def luna_model_settings() -> Any:
    """ModelSettings enforcing the Luna privacy posture (store=false)."""
    agents = require_sdk()
    from agents.model_settings import ModelSettings
    return ModelSettings(store=False)
