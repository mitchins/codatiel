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
    """Pre-dispatch budget gate: refuse the call before doing any work.

    The ledger check in `_record` stays as the post-dispatch backstop, but
    over-budget calls must not trigger repository scans, diffs, or searches
    whose results are then thrown away.
    """
    if len(ctx.tool_log) >= ctx.tool_budget:
        return json.dumps({"status": "error",
                           "error": f"tool budget exhausted ({ctx.tool_budget} calls); "
                                    "ending investigation within bounded resources"})
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
        blocked = _over_budget(ctx)
        if blocked is not None:
            return blocked
        checkout = ctx.base_dir if revision == "base" else ctx.head_dir
        args = {"path": path, "revision": revision,
                "start_line": start_line, "end_line": end_line or None}
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
        blocked = _over_budget(ctx)
        if blocked is not None:
            return blocked
        args = {"path": path, "context_lines": context_lines}
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
        blocked = _over_budget(ctx)
        if blocked is not None:
            return blocked
        checkout = ctx.base_dir if revision == "base" else ctx.head_dir
        args = {"query": query, "revision": revision, "max_results": max_results}
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
        blocked = _over_budget(ctx)
        if blocked is not None:
            return blocked
        checkout = ctx.base_dir if revision == "base" else ctx.head_dir
        args = {"symbol": symbol, "revision": revision}
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
        blocked = _over_budget(ctx)
        if blocked is not None:
            return blocked
        checkout = ctx.base_dir if revision == "base" else ctx.head_dir
        args = {"symbol": symbol, "revision": revision}
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
        blocked = _over_budget(ctx)
        if blocked is not None:
            return blocked
        args = {"path": path}
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
        blocked = _over_budget(ctx)
        if blocked is not None:
            return blocked
        import os
        args = {"query": query, "package": package, "version": version}
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
            added = self.ledger.record_response(
                getattr(response, "response_id", None),
                getattr(response, "usage", None))
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
        # Provider responses that arrived with no usable usage payload. The
        # spend ledger must treat these as unknown, never as confirmed zero.
        self._unknown_usage_response_ids: set[str] = set()

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
            "system": system, "messages": messages,
        }
        anthropic_tools = self._sdk_tools_to_anthropic(tools)
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
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
            cache_in = (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
                + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            sdk_usage = Usage(
                requests=1,
                input_tokens=(getattr(usage, "input_tokens", 0) or 0) + cache_in,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                total_tokens=0)
            sdk_usage.total_tokens = sdk_usage.input_tokens + sdk_usage.output_tokens
        return ModelResponse(output=output, usage=sdk_usage, response_id=resp.id)

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
    if isinstance(content, str):
        return content
    parts = [p.get("text", "") for p in content
             if isinstance(p, dict) and p.get("type") in
             ("input_text", "output_text", "text")]
    return "\n".join(p for p in parts if p)


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
    return asyncio.run(
        agents.Runner.run(agent, user_input, max_turns=max_turns))


def luna_model_id() -> str:
    """Luna backend: native SDK OpenAI provider; model id only, no adapter.

    Requires ``OPENAI_API_KEY`` from the Code Review project
    (STANDARD_API_NO_TRAINING). Returned as a plain model id for
    ``Agent(model=...)`` with the SDK default provider. Always pair with
    ``luna_model_settings()`` so ``store=false`` is enforced, not asserted.
    """
    return "gpt-6-luna"


def luna_model_settings() -> Any:
    """ModelSettings enforcing the Luna privacy posture (store=false)."""
    agents = require_sdk()
    from agents.model_settings import ModelSettings
    return ModelSettings(store=False)
