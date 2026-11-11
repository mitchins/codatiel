"""Minimal bounded tool loop (product path): stdlib inference only.

Text protocol (no function-calling dependency): the model emits one
fenced ```tool {"tool": name, "arguments": {...}}``` block per turn,
or the final JSON object. Strikes on misunderstanding (3 = fail
closed). Per-turn input/output accounting: measured usage when the
server reports it, char-estimate otherwise (labeled).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import inference as I

TOOL_RE = re.compile(r"```tool\s*(\{.*?\})\s*```", re.DOTALL)
TAG_RE = re.compile(r"<tool_call>\s*<function\s*=\s*([A-Za-z_0-9]+)\s*>"
                    r"\s*(\{.*?\})\s*</function>\s*</tool_call>", re.DOTALL)


@dataclass
class Tool:
    name: str
    description: str
    legible_schema: str
    fn: Callable[[dict], str]


@dataclass
class LoopResult:
    ok: bool
    error: str | None
    final_text: str
    turns: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    wall_s: float = 0.0


def _parse_call(text: str) -> dict | None:
    m = TOOL_RE.search(text)
    if m is None:
        m = TAG_RE.search(text)
        if m is not None:
            try:
                return {"tool": m.group(1), "arguments": json.loads(m.group(2))}
            except (ValueError, TypeError):
                return None
        s = text.strip()
        if s.startswith('{"tool"'):
            try:
                return json.loads(s[s.find("{"):s.rfind("}") + 1])
            except (ValueError, TypeError):
                return None
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def run(url: str, model: str, instructions: str, user_msg: str,
        tools: list[Tool], expect_json: bool = True,
        max_turns: int = 14, max_tokens: int = 4000,
        timeout_s: float = 300.0, api_key: str | None = None,
        expected_model_id: str | None = None) -> LoopResult:
    """Bounded agentic loop. Returns LoopResult (never raises on model error)."""
    t0 = time.monotonic()
    schema_lines = [f"- {t.name}: {t.description}\n  args: {t.legible_schema}"
                    for t in tools]
    base = (instructions
            + "\n\nTool protocol (strict — the ONLY way to call tools): reply with "
              "EXACTLY one fenced block and nothing else:\n```tool\n"
              '{"tool": "<name>", "arguments": {<args>}}\n```\n'
              "One call per message; wait for RESULT. When done, reply with "
              "your final JSON object instead (no tool block). "
              "A message that is neither is a misunderstanding.\n\nAvailable tools:\n"
            + "\n".join(schema_lines))
    messages = [{"role": "user", "content": base + "\n\n" + user_msg}]
    impls = {t.name: t.fn for t in tools}
    res = LoopResult(ok=False, error="not-run", final_text="")
    strikes = 0
    in_tokens = out_tokens = 0
    for turn in range(1, max_turns + 1):
        try:
            raw = I.chat(url, model, messages, max_tokens=max_tokens,
                         timeout_s=timeout_s, attempts=1, use_json_mode=False,
                         api_key=api_key, expected_model_id=expected_model_id)
        except (I.InferenceError, I.ModelIdentityError) as exc:
            res.error = f"transport: {type(exc).__name__}: {str(exc)[:150]}"
            break
        in_tokens += raw.prompt_tokens or I.estimate_tokens(messages[-1]["content"])
        out_tokens += raw.completion_tokens or I.estimate_tokens(raw.content)
        text = (raw.content or "").strip()
        res.turns.append({"turn": turn,
                          "live_input_chars": sum(len(json.dumps(m, default=str))
                                                  for m in messages),
                          "output_chars": len(text)})
        call = _parse_call(text)
        if call is None:
            if expect_json:
                try:
                    final = text[text.find("{"):text.rfind("}") + 1]
                    json.loads(final)
                    res.ok, res.error, res.final_text = True, None, final
                    break
                except (ValueError, TypeError):
                    pass
            else:
                res.ok, res.error, res.final_text = True, None, text
                break
            strikes += 1
            messages += [{"role": "assistant", "content": text[:2000]},
                         {"role": "user", "content": "That message is neither a valid "
                          "```tool block nor final JSON. "
                          f"Strike {strikes}/3."}]
            if strikes >= 3:
                res.error = "no valid final (strikes)"
                break
            continue
        try:
            name, args = call["tool"], dict(call.get("arguments", {}))
            fn = impls[name]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            strikes += 1
            messages += [{"role": "assistant", "content": text[:2000]},
                         {"role": "user", "content": f"INVALID tool call ({exc}). "
                          f"Valid: {sorted(impls)}. Strike {strikes}/3."}]
            if strikes >= 3:
                res.error = "invalid tool calls (strikes)"
                break
            continue
        t1 = time.monotonic()
        try:
            result = fn(args)
        except (LookupError, ValueError, OSError) as exc:
            result = f"ERROR: {type(exc).__name__}: {exc}"
        res.tool_calls.append({"tool": name, "args": args,
                               "result_chars": len(result),
                               "wall_s": round(time.monotonic() - t1, 2)})
        messages += [{"role": "assistant", "content": text[:2000]},
                     {"role": "user", "content": f"RESULT of {name}:\n{result[:6000]}"}]
    res.usage = {"requests": len(res.turns), "input_tokens_est": in_tokens,
                 "output_tokens_est": out_tokens,
                 "peak_live_chars": max([t["live_input_chars"] for t in res.turns] or [0])}
    res.wall_s = round(time.monotonic() - t0, 1)
    return res
