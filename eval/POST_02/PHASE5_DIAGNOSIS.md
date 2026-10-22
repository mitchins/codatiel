# POST-02 Phase 5 — Resource Exhaustion Diagnosis (L01)

**Scope:** L01 Haiku (`09`) and Luna (`10`) turn-budget exhaustion on the same 48-file PR (`f62ccdd9`→`935eec42`, 22 source /22 test /3 docs /1 config). Controls: `08_L01_gemma_complete` (thin, 8 tools, 9 turns, 112k input) and `04_FW3_luna_complete` (complete, 27 tools, 7 turns, 395k input, 24 files).

**Method:** `tool_calls.jsonl` purpose, `metrics.json` request ledgers, `run.json` terminal state. No `tool_calls.jsonl` exists for 09/10 (missing on `budget_exhausted` path — see Phase 1), so per-tool purpose classification is **unresolved** for those two. Diagnosis uses gemma thin as proxy and FW3 control as scope comparator.

---

## 1. Number and purpose of tool calls

- **09_haiku (exhausted):** `tool_calls: null` in `ledger.json`, no `tool_calls.jsonl` persisted. `spend_ledger.jsonl` 10 lines (10 requests) proves 10 SDK iterations, not tool count. Purpose **unresolved**.

- **10_luna (exhausted):** Same `tool_calls: null`, 10 requests, `input 1297030 / output 4568`. Turns 8–9 emitted only 64 / 40 output tokens (near-silence) while input grew to 181k — suggests thrashing, but without transcript, tool names cannot be quoted.

- **08_gemma (thin proxy):** `tool_calls.jsonl:1-8` — 1 `diff_window` on `OperationReconcile.swift:1-236` + 7 `read_file` on `SessionState.swift`/`SessionActor.swift`/`SessionStore.swift`/`OperationReconcile.swift` (distinct slices, no duplicates). All useful follow-ups to diff hits, but coverage insufficient for 48 files.

- **04_FW3_luna (control):** 27 tools (12 `diff_window`, 2 `search_code`, 13 `read_file`), all distinct, no duplicates, `tool_budget 40` not approached.

## 2. Useful versus repeated reads

- For 09/10: **Cannot be determined** — no tool log to classify.
- For 08_gemma proxy: **0 repeated identical calls** (checked `path+args` unique). All 8 were useful in isolation, but 8 is too few for 48 files.
- For 04 control: **0 duplicates** across 27 tools.

## 3. Whether the agent was making measurable progress

- **09/10:** **No measurable progress** in surviving artifacts — zero `evidence.jsonl`, zero `tool_outputs` persisted. `output_tokens` per turn (95–2670 for Haiku, 64–561 for Luna) shows model was emitting, but without tool records, breadth/depth cannot be measured. Must not infer from token counts.

- **08_gemma:** Measurable progress exists (8 evidence IDs), but adjudication found it still `thin` — “8 tools insufficient for 48-file substantive PR; cannot rule out”.

- **04 control:** Measurable progress yes — 27 evidences, culminating in 1 attested finding + 180+ observations.

## 4. Whether decisive evidence was available

- **Cannot be affirmed for 09/10** — no excerpts persisted. The 48-file diff includes `SessionStore.swift`, `SessionState.swift`, `SessionTransition.swift`, `OperationReconcile.swift`, `GRDBSessionStore.swift`, etc., all reachable via `read_file`/`diff_window`, but no record that the agent reached them before exhaustion.

- **Proxy 08** did fetch `OperationReconcile.swift:1-236` and `SessionState` slices, yet still `thin` — suggests decisive evidence was reachable but scope was too broad for 8 tools.

## 5. Why the final turn budget was exhausted

- **SDK bounded loop hit `max_turns=10`.** `service.py: MaxTurnsExceeded` → `_write_failed_run(budget_exhausted)` — byte-identical error in `run.json:10`/`metrics.json:2`. Not spend ceiling (`ceiling 4.77/4.66` not exceeded, `preflight` not refused), not tool-budget (40 never reached), not context overflow (final input 144k/182k < 200k window).

- **Turns 8–9 low-output pattern for Luna** (64/40 tokens) suggests thrashing near limit, but surviving records contain no `tool budget exhausted` error payload — exhaustion is at Runner level, not tool gate.

## 6. Whether the review scope was realistically completable under configured limits

- **No — 10 turns insufficient for this 48-file substantive PR with bounded resources.**

  - Gemma on same SHAs used 9/10 turns + 8 tools (112k input) and still `thin` — 10 turns leaves no slack for 48-file inventory + targeted reads.
  - Haiku burned 10/10 turns (1,049k input, 14k output) and still incomplete without verdict.
  - Luna burned 10/10 turns (1,297k input, 4.5k output, lower efficiency) and same result.
  - FW3 control (24 files, half the scope) completed in 7/10 turns / 27 tools with 3 turns and 13 tool slots spare — demonstrates 24 files fits, 48 files does not.
  - `m8_run.py` enforces `tool_budget 40` and `max_turns` as hard caps; raising `M8_TURNS` would be required to test completability — not done for this pair.

## 7. Minimal, testable proposal (if clear failure mechanism exists)

**Bounded investigation improvement — turn-1 triage + soft budget gate (not new heuristics in POST-02):**

- **Turn 1 triage:** `file_classification.json` + `changes.json` summary already available at `run.json` start. If `changed_files > 20`, model instruction to prioritize `diff_window` on `README`/`docs/milestones/*` + `search_code` for verification claims before deep `probes/*` reads; plus SDK soft gate: at turn 8 if no finding drafted and turns remaining ≤2, emit `thin` with preserved notes rather than exhausting to `incomplete`.

- **Reusable:** Existing `file_classification.json`, `changes.json`, `max_turns` param in `run.json`.

- **Risk:** Low — triage only reorders tool priority; budget gate only affects otherwise-incomplete runs, converting `incomplete` → `thin` (more honest, not more findings). Risk of missing deep probe defect if triage skips it — mitigate by still allowing probe reads if README check passes and turns remain.

- **Qualification test:** Replay `L01` 48-file `file_classification.json` through triage prompt (simulated); verify `diff_window README`/`m0-ledger` not in `L01` case but `SessionState` priority is correct; verify budget gate on a 10-turn mock transcript produces `thin` not `incomplete`.

- **Complexity:** S–M (1–2 days, prompt + 10-line SDK gate).

- **Expected:** Completion ↑ (incomplete → thin honest), useful may ↑ slightly (README-class misses caught earlier), false positives unchanged.

**No implementation in POST-02** — diagnosis only, per brief.

---

**Files quoted:** `09/.../run.json:5,10,16-17`, `09/.../metrics.json:6-9,90-94,14-85`, `09/.../spend_ledger.jsonl:1-10`, `10/.../run.json:5,10,16-17`, `10/.../metrics.json:6-9,90-94,14-85`, `08_L01_gemma_complete/tool_calls.jsonl:1-8`, `04_FW3_luna_complete/tool_calls.jsonl:1-27`, `eval/M8_ROSTER.json:2`, `ledger.json:137-161`, `codatiel_i2/service.py:preflight` and `runtime_sdk.py:744-768`.
