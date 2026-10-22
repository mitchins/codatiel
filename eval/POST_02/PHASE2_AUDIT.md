# POST-02 Phase 2 — Provider/SDK Boundary Audit

**Scope:** Haiku adapter (`codatiel_i2/runtime_sdk.py:412-562`) and its interaction with Agents SDK `Runner.run`, `ModelResponse`, `Usage`, and `service.py` final-output handling.

**Method:** Offline, against actual captured M8 provider response shapes (transcript.json verbatim) and current SDK contracts (agents 0.17.6). No new inference.

---

## 1. Thinking / text / tool-use block ordering

**Code:** `HaikuModel.get_response:496-526` collects `thinking_here`, `texts`, `output` (tool_use) per response, then orders via `_order_assistant_blocks: thinking, text, tool_use` and `_merge_same_role`.

**Observed:** M8 01/06/12 all show provider returns `text` before `tool_use` in same response (e.g., `transcript.json:108` text + 5 tool_use). The adapter's ordering matches Anthropic's required `thinking → text → tool_use` and SDK's `assistant` role alternation. No evidence of provider misreading trailing text as prefill in M8 traces; the `output_config.effort=medium` with `thinking: adaptive` is correctly set.

**Verdict:** **Correct.** No defect. The ordering logic is preserved verbatim in `transcript.json` raw `ResponseFunctionToolCall` + `ResponseOutputMessage`.

## 2. Preservation of thinking state across tool turns

**Code:** `_thinking_groups` retains per-response thinking + tool_ids, re-emitted via `_input_to_anthropic` before the first historical fragment of that response's `tool_use`.

**Observed:** M8 01/06 each had 7 thinking blocks across 7 requests, each preceding its tool_use batch. The re-emission was verified by checking that `transcript.json` raw items contain `thinking` with signature, and that `model_calls.jsonl` counts `thinking_blocks_seen:7` but `reasoning_summary: null` (provider-private, never claimed). No `redacted_thinking` appeared in M8.

**Verdict:** **Correct.** No loss of thinking state observed; the 2 incomplete L01 traces have no `transcript.json` to verify, but the mechanism is identical.

## 3. Multiple tool calls in one response

**Code:** `tool_ids_here: list[str]` collects all `tool_use.id` per response, groups thinking once per response.

**Observed:** M8 01 had 5 tool calls in one response (turn 1), 06 had 5, 12 had 3. All tool_use blocks were preserved as separate `ResponseFunctionToolCall` items in `ModelResponse.output`, each with `call_id` matching the subsequent `tool_call_output_item` in `transcript.json`. No truncation or merging.

**Verdict:** **Correct.** Parallel tool calls share one thinking emission, not duplicated, not interleaved — as implemented.

## 4. Empty thinking blocks

**Code:** `_retain_thinking_block` returns `None` if `thinking.strip() == ""`, dropping empty blocks.

**Observed:** M8 shows `thinking_blocks_seen:7` but only 6 `message_output_item` surfaced for 01/06 (one final thinking produced no text). The drop is intentional: provider validation rejects empty thinking with tool_use (400 on multi-turn tool flows, per `HaikuModel` comment). No M8 trace shows a valid thinking block being incorrectly dropped.

**Verdict:** **Correct.** Prevents provider 400, as documented.

## 5. Text appearing before or after tool-use blocks

**Code:** Orders `thinking` → `text` → `tool_use` per assistant message; `_merge_same_role` merges consecutive same-role fragments with `_order_assistant_blocks`.

**Observed:** All M8 Haiku responses have text *before* tool_use (e.g., 01: `108` text then 5 tool_use). No M8 trace shows text *after* tool_use in the same response (which would be a provider edge case). The adapter would order trailing text before tool_use, which is wrong for that shape, but it did not occur in M8.

**Verdict:** **Correct for observed shapes; not demonstrated for trailing-text shape.** No M8 failure is attributed here. A trailing-text response would be a provider edge case to test in regression (Phase 4).

## 6. Stop reasons and max-token exhaustion

**Code:** No explicit `stop_reason` handling in `HaikuModel.get_response`; relies on SDK `Runner` to check `response.usage` and `max_turns`.

**Observed:** M8 01/06 last 2 turns capped at `output 8192` (Haiku `max_tokens=8192`), but `metrics.json` shows `status: complete` not `max_tokens` error. The SDK did not surface `stop_reason: max_tokens` as a distinct terminal state; it simply returned the capped output (8192 tokens) and then on the next turn the provider returned empty final. No `max_tokens` exhaustion was recorded as a terminal state.

**Verdict:** **No defect in adapter, but missing terminal-state distinction.** The SDK's `Usage` does not expose `stop_reason`, and `service.py` does not distinguish `max_tokens` exhaustion from `empty final output`. Both are currently `complete` with `parse_errors`, but should be distinct per brief.

## 7. SDK final-output extraction

**Code:** `runtime_sdk.run_review` does `agents.Runner.run(agent, user_input, max_turns)` and returns `RunResult` with `final_output` (the last `ResponseOutputMessage` text, or `""` if none).

**Observed:** For 01/06, `result.final_output` was `""` (empty string) because the 7th `ModelResponse` had `output=[]` (no `ResponseOutputMessage`). For 12, `final_output` was the full prose+JSON string (`transcript.json:12` text). For controls 18/24, `final_output` was pure JSON.

**Verdict:** **Correct.** The SDK correctly extracts final text or empty string; no adapter loss.

## 8. JSON parsing and output-schema validation

**Code:** `service._parse_model_output` does:
```python
if not final.strip(): return {"status": "incomplete", ...}, ["empty final output"]
text = final.strip()
if text.startswith("```"): ... rsplit("```")
parsed = json.loads(text)
```

**Observed:** For 01/06, `final = ""` → `empty final output` → `status: incomplete` (correct, but then service records `status: complete` based on `tool_calls`, not on parse result — see Phase 3). For 12, `final = "I checked...\n\n{"status": "silence"...}"` → `json.loads` on whole string starting at `I` → `Expecting value: line 1 column 1` → `unparseable`.

**Verdict:** **Implementation defect — strict parser.** It correctly handles empty and pure JSON, but fails on demonstrably equivalent prose+JSON where the provider returned a valid JSON object trailing prose. The brief explicitly allows deterministic repair: extracting a valid JSON object from an otherwise correctly returned final text block. This is the only confirmed implementation defect in the final-output path.

## 9. Errors during tool-result continuation

**Code:** `runtime_sdk.build_i2_tools` wraps each I2 tool in `try/except` → `{"status": "error", ...}` and then `_record`, so tool errors become tool outputs, not exceptions. `HaikuModel._input_to_anthropic` maps `function_call_output` → `tool_result` user blocks.

**Observed:** M8 01 had one invalid `read_file {}` → `tool_call_output_item` with `{"unparseable_output_head": "An error..."}` and `summary: {"evidence_ids": []}` — correctly returned as `tool_result` and re-fed to model, not as exception. No `tool_result` continuation failure observed.

**Verdict:** **Correct.** No defect; tool errors are preserved and continuation succeeds.

## 10. Cancellation and resource-budget exhaustion

**Code:** `runtime_sdk.run_review` is `asyncio.run(Runner.run(..., max_turns))`; `service._run_review` catches `MaxTurnsExceeded` by name and calls `_write_failed_run(budget_exhausted)`.

**Observed:** M8 09/10 L01 both hit `MaxTurnsExceeded` after 10 turns, `run.json:10` `error: "turn budget exhausted..."`, `metrics.json:2-3` same, `ledger.json tool_calls: null`, `spend: UNKNOWN_USAGE` with partial ledger retained. No `SpendCeilingExceeded` or `tool_budget` breach recorded.

**Verdict:** **Correct.** Turn-budget exhaustion is a controlled `incomplete_budget_exhausted` state, partial spend retained, nothing fabricated. The missing `tool_calls.jsonl` on this path is intentional per `service.py:_write_failed_run` (writes only `run/metrics/spend`), but it limits forensics (see Phase 1).

---

## Required distinction (per brief)

| Scenario | Observed in M8 | Current terminal state | Correct classification | Repair needed |
|---|---|---|---|---|
| Provider returned valid final, adapter lost it | **Not observed** — 01/06 adapter preserved all 6 texts, 12 preserved prose+JSON | N/A | N/A | No |
| Provider returned incomplete/malformed, adapter preserved correctly | **12_SW7_haiku** — prose+JSON preserved, strict parser failed | `complete` with `unparseable` | `unresolved` / `model_output_failure` with trailing JSON extracted | **Yes — deterministic repair** |
| Review exhausted budget while investigating | **09/10 L01** — `MaxTurnsExceeded` | `incomplete_budget_exhausted` | Correct, preserve | No (but proposal for scope) |
| Empty provider final (model unfinished) | **01/06** — no final `output_text` after last tool results | `complete` with `empty final output` | **Should be `unresolved` / `model_output_failure`, not `complete`** | **Yes — terminal-state propagation** |

**Summary:** One confirmed implementation defect (strict parser), one terminal-state misclassification (empty final → should be unresolved, not complete), and one missing distinction (max-tokens exhaustion not surfaced). All other adapter behaviors correct for observed shapes.
