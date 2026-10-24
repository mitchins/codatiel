# M9 — Failure Mechanism Diagnosis (short, evidence-backed)

**Source:** M8 traces `eval/M8_REAL_REVIEWS/traces/*`, `failure_taxonomy.json`, `adjudications.json`, `REPORT.md`. No new retrospective beyond the minimum to choose an intervention.

## 5-way classification per failure

1. **Never considered** — KL1 Luna 38-tool run missed README staleness (`README 48/32 vs ledger 67/73`) while Gemma only `diff_window` on spec file. Evidence: `23_KL1_gemma_complete` tool_log 1 call, `25_KL1_luna_complete` 38 calls but never `read_file README.md`. Both M7 FW3 defects missed by all 8 FW3 runs (no trace cites `store.py:845` median or policy).
2. **Considered wrong code** — L01 (48 files, durable store) 8 tools (Gemma) + 2 turn-exhaustions (Haiku/Luna at 10 turns). Tool_log shows `read_file a.py`-style file-coverage without reaching `CaseLedger.swift` consumers. Mechanism `investigation_strategy / resource_limits`.
3. **Inspected but missed implication** — L10 Luna `evidenceTier` non-optional Codable break: cited `CaseLedger.swift:227` correctly, cited `base` without tier, but no `search_code` for JSON decode sites to ground breakage. Label downgraded `CONFIRMED_USEFUL → PARTIALLY_CORRECT`.
4. **Recognized but insufficient evidence** — RB7 Gemma-1 preview-policy summary (`headline_eligible`): claim true (policy gap confirmed by RB7 Luna), but cited hunk is unrelated `urllib` removal. `advisory_grounded` + correct evidence required, so comment unusable (`PARTIALLY_CORRECT`).
5. **Correct but immaterial** — FW3 Gemma `semantic_twins` phased recovery, L10 Gemma `evidenceTier` summary, SW7 Gemma `resolve_entities=False`, RB7 Gemma-0 checkpoint strings: all `REAL_LOW_VALUE` — accurate restatements of intended behavior/test additions, no defect or action.

## Why this is a discovery failure, not a filter failure

- Thoroughness without prioritization: KL1 Luna 38 calls, L01 10-turn exhaustion, FW3 24-file control — all investigated genuinely (tool evidence replayed, `M7.1 ATTESTED`) but budget spent mechanically across files, not on high-impact paths.
- Summarization trap: Gemma 5 findings, 4 low-value — model chooses the easiest observable change (diff hunk) and narrates it, because no hypothesis ranks materiality.
- Cross-file consequences missed: RB7 Luna preview-flag enforcement gap required reading `build_docs_data.py:335-340` **and** `405-422` (`_is_current_full_suite_run`); only the challenger that links changed behavior → consumer → condition will find it.

## Intervention chosen

**Impact-path investigation challenger** (smallest coherent change):
- Instructions force explicit hypothesis formation: `Changed behavior → affected consumer/contract → triggering condition → observable failure → developer impact`, ranked 2-3 by materiality/plausibility.
- Inventory adds behavioral hints (high-churn files, “focus on contracts/callers/state transitions”) so hypotheses start from impact, not file list.
- Existing 7 I2 tools reused; no second agent loop, no mandatory traversal; budget spent on promising paths, weak hypotheses abandoned.
- Mechanism visible in `CHALLENGER_INSTRUCTIONS` + `_render_inventory(variant=challenger)` and observable as hypothesis-driven tool sequences in traces (vs file-coverage baseline).

This directly addresses 1–5: it forces consideration of the right risk (hypotheses), directs to right code (tool calls test hypotheses), requires implication reasoning (triggering condition), demands evidence for the claim, and suppresses immaterial summaries by requiring a failure/impact link.
