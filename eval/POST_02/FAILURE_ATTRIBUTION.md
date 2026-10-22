# POST-02 Failure Attribution — M8 Traces

**Scope:** 5 mandatory failures (01/06/12 Haiku empties, 09/10 L01 exhaustions) + 3 controls (18/24 Haiku, 04 Luna). All reads from `eval/M8_REAL_REVIEWS/traces/` and `/tmp/m8/runs/`.

**Method:** SDK run events, raw provider blocks, tool-use/tool-result sequence, thinking blocks, stop reasons, token usage, SDK final-output conversion, JSON parsing, disposition, terminal state. No hidden reasoning inferred from token counts.

---

## 1. 01_FW3_haiku — Empty final output — Model unfinished / empty provider final turn

**Run:** `01_FW3_haiku_complete` — `haiku:claude-haiku-5-5`, `max_turns 10`, `tool_calls 26`, `requests 7`, `input 699584 / output 31955`.

- **SDK events:** `transcript.json` 60 items: 27 `tool_call_item`, 27 `tool_call_output_item`, 6 `message_output_item`. No final `message_output_item` after last tool results — transcript ends with 4 `tool_call_output_item` (lines 56–59).
- **Raw provider blocks:** 6 `ResponseOutputMessage` texts, all intermediate reasoning (e.g., `transcript.json:38` "I'll start with the diffs...", `55` "Two things need checking before I decide..."), none contain `{"status":` JSON.
- **Tool sequence:** 26 dispatched (12 `diff_window`, 3 `search_code`, 2 `read_file`, etc.), ends with 4 tool results, no following text. One invalid `read_file {}` → error payload, not counted, preserved.
- **Thinking:** `model_calls.jsonl` `thinking_blocks_seen:7`, `reasoning_summary:null` (provider-private, never claimed).
- **Stop reason / tokens:** No `stop_reason` in transcript; `status: completed` per turn. Last 2 turns `output 8192, 8192` (capped), but `max_tokens` is per-turn (Haiku adapter `output_config.effort=medium`), not context. No context overflow.
- **SDK final-output conversion:** `""` (no text to convert), `model_findings_raw.jsonl` 0 bytes, `shadow_result.json: investigation_notes ["empty final output"]`.
- **JSON parsing:** `metrics.json:5-6` `parse_errors: ["empty final output"]` — no JSON to parse.
- **Terminal state:** `metrics.json:80` `status: complete` but `parse_errors` non-empty; `spend: CONFIRMED_USAGE, ceiling not exceeded`. `findings.jsonl` 0 model candidates (55 `verified_proof` are deterministic, not model).

**Earliest failure:** **Empty provider final turn / model unfinished** — after the last tool-result batch, the provider emitted no final `output_text` containing review JSON within `max_turns:10` / 7 requests. **Not** parser rejection (would be `unparseable`), **not** adapter loss (adapter preserved all 6 texts + 27/27 tool pairs verbatim).

---

## 2. 06_L10_haiku — Empty final output — Identical pattern

**Run:** `06_L10_haiku_complete` — `haiku:claude-haiku-5-5`, `max_turns 10`, `tool_calls 28`, `requests 7`, `input 461535 / output 28686`.

- **SDK events:** `transcript.json` 62 items: 28 tool_call, 28 result, 6 message. Same tail: 6 tool outputs, no final message.
- **Raw blocks:** 6 reasoning texts (e.g., `55` "The STORE-06 catalogue entry and the receipt are consistent... Still unverified: whether the five required..."), none JSON.
- **Tokens:** Last turn `output 8192` capped, `thinking_blocks_seen:7`.
- **Final-output:** `""`, `model_findings_raw` 0 bytes, `parse_errors: ["empty final output"]`.
- **Terminal:** `complete` but unpublishable.

**Earliest failure:** **Empty provider final turn / unfinished** — same mechanism as 01, not parser, not adapter loss.

---

## 3. 12_SW7_haiku — Unparseable — Parser rejection (leading prose)

**Run:** `12_SW7_haiku_complete` — `haiku:claude-haiku-5-5`, `max_turns 10`, `tool_calls 6`, `requests 3`, `input 34902 / output 2928`.

- **SDK events:** `transcript.json` 13 items: 6 tool_call, 6 result, **1** message (the final).
- **Raw provider block:** `transcript.json:12` preserved verbatim:
  ```
  ResponseOutputMessage(id='msg_msg_011CgEQ2YjK9AwtVB3xpGY5w', content=[ResponseOutputText(text='I checked the three changed files at HEAD and the diff hunks. I found no regression... The 10 listed docs_code candidates all map to whole-file line 1... {"status": "silence", "summary": "Checked the CI workflow...", "findings": []}', type='output_text')] , status='completed')
  ```
  Provider **did** produce content: ~1700 chars prose + trailing valid JSON `{"status":"silence",...}`.
- **Tool sequence:** `diff_window`×3 → `search_code`×2 → `read_file`×1 → final text — clean, no error.
- **SDK final-output conversion:** Adapter preserved prose+JSON verbatim (single `message_output_item`).
- **JSON parsing:** `metrics.json:5-6` `parse_errors: ["unparseable: Expecting value: line 1 column 1 (char 0)"]` — harness did `json.loads` on entire `output_text` starting at `I` (char 0), correct JSON starts after `\n\n`. Parser is strict whole-string, not trailing-object extraction.
- **Terminal:** `status: complete` with parse error, `model_findings_raw` 0 bytes, `publish_preview publishable_count 0`.

**Earliest failure:** **Parser rejection** — provider non-empty, adapter intact, model did emit JSON but prepended prose violating `REVIEWER_OUTPUT_CONTRACT` (requires isolated JSON). **Not** empty, **not** adapter loss, **not** unfinished.

---

## 4. Controls — Successful Haiku final-output

**18_RB7_haiku_complete** — `metrics.json:5-6` `parse_errors: []`, `tool_calls 35`, `requests 8`, last output `6398` (not capped), `thinking_blocks_seen:8`. Final `transcript.json:71`:
```
ResponseOutputMessage(text='{"status": "finding", "summary": "Reviewed the checkpoint...", "findings": [{"detector": "contracts", ...}]}')
```
Starts at `{"status"` column 1 → `json.loads` succeeds → `model_findings_raw.jsonl` populated, disposition assigned.

**24_KL1_haiku_complete** — `parse_errors []`, `tool_calls 16`, `requests 5`, last output `2188`, final `transcript.json:36` `{"status": "finding", ... "evidence_ids": [...]}` — same pure-JSON success.

Both demonstrate **correct SDK final-output conversion**: raw `output_text` exactly the JSON object, no leading prose, extracted verbatim, parsed.

---

## 5. 09_L01_haiku — Turn-budget exhaustion

**Run:** `09_L01_haiku_incomplete_budget_exhausted` — `haiku:claude-haiku-5-5`, `max_turns 10`, `status: incomplete_budget_exhausted`, `stage: budget_exhausted`, `error: "turn budget exhausted after 10 turns; investigation incomplete, not a review"`.

- **Artifacts present:** `run.json`, `metrics.json`, `spend_ledger.jsonl` (3 files). **Missing:** `transcript.json`, `tool_calls.jsonl`, `tool_outputs.jsonl`, `evidence.jsonl`, `model_calls.jsonl`, `findings.jsonl` — not written on `MaxTurnsExceeded` path (`service.py: _write_failed_run` writes only `run/metrics/spend`).
- **Token usage:** `metrics.json:90-94` `input 1049539 / output 14068 / requests 10` (10 ledger lines 1:1 with requests). Per-turn ledger `3916→144218` input growth, no truncation flag. `accounting_status: UNKNOWN_USAGE`, `ceiling_usd 4.7756` not exceeded, `preflight` not refused, `tool_budget 40` not hit.
- **Tool purpose / repeated:** **Unknown from surviving artifacts** — `ledger.json tool_calls: null`, no `tool_calls.jsonl` to count/quote. Input growth proves *some* tool outputs were returned and re-fed, but type/purpose not recorded. No `model_calls.jsonl` to quote. Must not infer from token counts.
- **Progress measurable:** **No** — zero evidence rows, zero tool outputs persisted.
- **Decisive evidence available:** **Cannot be affirmed** from this trace — 48-file diff in scope (`changes.json` 22 source /22 test /3 docs /1 config), but no excerpts quoted. Gemma on same SHAs fetched `OperationReconcile.swift:1-236` etc., yet still `thin` — scope vs budget signal.
- **Why exhausted:** SDK bounded loop hit `max_turns=10` → `MaxTurnsExceeded` → `_write_failed_run(budget_exhausted)` — byte-identical error in `run.json:10`/`metrics.json:2`. Not spend ceiling, not tool-budget, not context overflow (final input 144k < 200k window).

**Earliest failure:** **Resource-budget exhaustion (turn)** — honest `incomplete`, partial usage retained, nothing fabricated. Scope (48 files, durable store + ledger) not completable in 10 turns; gemma on same SHAs used 9 turns + 8 tools and still `thin`.

---

## 6. 10_L01_luna — Turn-budget exhaustion

**Run:** `10_L01_luna_incomplete_budget_exhausted` — `luna:gpt-6-luna`, `max_turns 10`, same `budget_exhausted` status/error.

- **Artifacts:** Same 3-file set as 09, same 7 missing files, same `tool_calls: null` in ledger.
- **Token usage:** `input 1297030 / output 4568 / requests 10` (highest input, lowest output; turns 8–9 emitted only 64/40 tokens — near-silence while input grew to 181k). `ceiling_usd 4.6636` not exceeded, `UNKNOWN_USAGE`.
- **Tool purpose / repeated:** **Unknown** — no `tool_calls.jsonl` to classify.
- **Why exhausted:** Same `max_turns=10` → `MaxTurnsExceeded` mechanism. Turns 8–9 low-output pattern suggests thrashing near limit, but without transcript no tool names to cite.

**Earliest failure:** **Resource-budget exhaustion (turn)** — same root cause as 09, but lower turn efficiency (24% more input, 68% fewer output). Two independent backends exhausting on same 48-file PR is strong scope-vs-budget signal.

---

## 7. Control — 04_FW3_luna — Completes within budget

**Run:** `04_FW3_luna_complete` — `luna:gpt-6-luna`, `max_turns 10`, `status: complete`, `tool_calls 27`, `requests 7`, `input 395482 / output 3313`, `wall 116s`, `spend CONFIRMED_USAGE $0.041`.

- **Tool purpose:** 27 dispatched tools, all returned evidence, **no duplicate `path+args`**: 12 `diff_window`, 2 `search_code`, 13 `read_file` (each distinct slice, no overlapping range). `tool_budget 40` not approached, `MaxTurnsExceeded` not raised.
- **Progress measurable:** Yes — 27 evidence IDs over 7 turns, culminating in 1 attested finding (`store.py:845` median) + 180+ `observed_not_material`. Decisive hunk fetched (`read_file store.py:836-850`).
- **Why not exhausted:** FW3 scope (24 files) fits within 7/10 turns with focused reads; 3 turns and 13 tool slots spare. Same `max_turns=10`/`tool_budget=40`/Luna backend that exhausted on L01 completes FW3 — validates harness, isolates exhaustion to scope, not bug. Same `service` version `canonical-service-0.1`.

---

## Summary — earliest failure per run

| Run | Recorded terminal | Earliest demonstrable failure | Distinction |
|---|---|---|---|
| 01_FW3_haiku | `complete` but `empty final output` | Empty provider final turn / unfinished (no final `output_text` after last tool results) | Not parser, not adapter loss |
| 06_L10_haiku | `complete` but `empty final output` | Empty provider final turn / unfinished | Not parser, not adapter loss |
| 12_SW7_haiku | `complete` but `unparseable` | Parser rejection (leading prose + trailing JSON) | Not empty, adapter intact |
| 09_L01_haiku | `incomplete_budget_exhausted` | Resource-budget exhaustion (turn, 10/10) | Not spend/tool/context |
| 10_L01_luna | `incomplete_budget_exhausted` | Resource-budget exhaustion (turn, 10/10) | Same, lower efficiency |
| 18_RB7_haiku (control) | `complete` | — (pure JSON final, parsed) | — |
| 24_KL1_haiku (control) | `complete` | — (pure JSON final, parsed) | — |
| 04_FW3_luna (control) | `complete` | — (27 tools, 7 turns, finding) | — |

**Unresolved attributions:** None for the 3 Haiku empties (distinction clear from `parse_errors` and `transcript.json` verbatim). For 09/10, per-tool purpose classification is **unresolved** due to missing `tool_calls.jsonl` on the `budget_exhausted` path — token counts alone not used to infer reasoning quality.

**Files quoted:** `run.json`, `metrics.json:5-6` parse_errors, `model_calls.jsonl:1` thinking_blocks_seen, `transcript.json` raw `ResponseOutputMessage`/`ResponseFunctionToolCall`, `tool_calls.jsonl` counts, `spend_ledger.jsonl`, `model_findings_raw.jsonl`, `shadow_result.json: investigation_notes`, `findings.jsonl`/`observations.jsonl` dispositions.
