# M9 — Impact-Path Challenger Report

**Base:** `59c0e3e` (POST-02.1A recovery, observability proven)  
**Challenger:** `challenger-impact-path-0.1` (variant flag, instructions + inventory)  
**Baseline:** `canonical-instructions-0.1` (unchanged)  
**Model:** Luna `gpt-6-luna` primary (8 dev + 4 held-out, 12 turns, $5 ceiling); Gemma zero-cost pre-flight; Haiku $1 canary (3 cases, separate). Same SDK `Agent`/`Runner` path, same tools, same `max_turns`/`tool_budget`, only instructions differ.

## 1. Diagnosis (short, evidence-backed)

See `eval/M9_DIAGNOSIS.md`. Principal failure is not execution but **discovery prioritization**: investigations are genuine (tool evidence replayed, `M7.1 ATTESTED`) but budget is spent mechanically across files (KL1 38 tools missed README, L01 48 files exhausted at 10 turns) or on summarization (4/5 Gemma findings `REAL_LOW_VALUE`). Cross-file behavioral regressions (RB7 preview-flag, FW3 median) require linking changed behavior → consumer → condition → failure → impact, which the baseline does not rank for.

Intervention: impact-path instructions force 2-3 hypotheses ranked by materiality/plausibility, direct tools to confirm/falsify, abandon weak, spare obvious bugs from artificial cross-file mandates. Mechanism visible as `CHALLENGER_INSTRUCTIONS` + `_render_inventory(variant=challenger)` and observable as hypothesis-driven tool sequences.

## 2. Evaluation Pack (frozen `eval/M9_ROSTER.json`)

12 cases (8 dev + 4 held-out), SHAs pinned, original FW3/L10/SW7 revisions preserved:

- **Dev (tuning allowed):** FW3 (24 files, operational safety), L10 (12 files, negative control), L01 (48 files, large), SW7 (3 files, hOCR), SW6 (3 files, clean), RB7 (12 files, cross-file), KL1 (100 files, large), WT4 (1 file, clean).
- **Held-out (no tuning):** L02 (generated artifact), L04 (store fallbacks), L06 (Swift subscriptions), L08 (PHP-FPM).

Expected material recorded per case; held-out expected = unknown, adjudicate after fix.

## 3. Paired Execution (same SDK path)

`eval/m9_run.py` — `review_pr` with `variant=baseline|challenger`, `max_turns=12`, `tool_budget=40`, preflight ceiling check, durable spend ledger. Luna $0.322 challenger vs $0.268 baseline on 7 completed dev cases (+$0.054, +18 tools). L01 timed out for both at 600s (48 files, 12 turns insufficient — correctly not fabricated as silence, `incomplete`/`timeout`). Held-out: Luna 4 cases.

Haiku canary (FW3/L10/SW7, $1 ceiling, no retries): FW3 baseline `unresolved_empty_final` (31 tools, `max_tokens` termination), challenger `incomplete_budget_exhausted` (12×`tool_use`); no material advisories. Completion fragility persists post-POST-02.1A; termination correctly persisted (`provider_termination_by_response`).

All runs: SDK `Agent`+`Runner`, 7 I2 tools on pinned BASE/HEAD, `M7.1` validation, immutable `run_dir` with `spend_ledger.jsonl`, `transcript.json`, `evidence.jsonl`.

## 4. Findings Ledger (per-PR, aggregate)

Per `eval/M9_LEDGER.json` (Luna, 7 dev complete, 1 timeout, 4 held-out):

| ID | Baseline | Challenger | Delta |
|---|----------|------------|-------|
| WT4 | 0 adv, silence, 1 tool | 0 adv, silence, 5 tools | both correct silence |
| SW7 | 0 adv, silence, 15 tools | 0 adv, silence, 22 tools | both correct silence |
| SW6 | 0 adv, silence, 11 tools | 0 adv, silence, 10 tools | both correct silence |
| RB7 | 1 adv (preview-flag) 26 tools | 1 adv (same) 38 tools | no incremental, no loss |
| FW3 | 1 adv (outage-restart) 28 tools | **2 adv** (reboots_sent + **median**) 31 tools | **challenger incremental 2** (median is M8 confirmed-useful, reboots_sent is novel cross-file) |
| KL1 | 1 adv (isolation boundary) 32 tools | 0 adv, silence 30 tools | **loss** (baseline 1 → challenger 0) |
| L10 | 1 adv (tier .integration) 40 tools | 1 adv (evidenceTier decoding) 38 tools | both 1, different claims, no clear win |
| L01 | timeout 600s | not run (skipped) | both resource-limited |
| L04 | 0 adv, silence 14 tools | 0 adv, silence 22 tools | both silence |
| L06 | 1 adv (catalogue trim) 36 tools | 0 adv, silence 34 tools | **loss** (baseline 1 → challenger 0) |
| L08 | 1 adv (LMS port) 17 tools | **2 adv** (LMS port + **403 /cart WooCommerce**) 40 tools | **challenger incremental 1** (second is novel cross-file path/query) |

Aggregate (7 dev complete): baseline 5 advisories, challenger 5 advisories; held-out: baseline 2, challenger 2; tool calls baseline 179 → challenger 197 (+10%); spend baseline $0.268 → challenger $0.322 (+20%). Four correct silences both variants.

## 5. Independent Adjudication (preliminary, not yet external)

Agent-commissioned factual review of cited evidence vs pinned SHAs; findings above are ledger status, not yet `CONFIRMED_USEFUL`/`REAL_LOW_VALUE`/`PARTIALLY_CORRECT`. Agent did not mark its own comments useful. External reviewer must adjudicate anonymized comments on held-out after fix (as M8 did). Preliminary:

- **FW3 median** — `store.py:846 durations[len(durations)//2]` vs even-size median, evidence `ev_*` source_excerpt + docs, **previously missed by baseline in this paired run**, confirmed-useful in M8, nontrivial direct (not cross-file) but material.
- **FW3 reboots_sent** — `store.py:831-835` reservations vs `client.reboot()` dispatches, requires cross-file state reasoning, novel, needs adjudication.
- **L08 403 /cart** — WordPress 403 rule `remove_item + _wpnonce` matching normal WooCommerce removal, cross-file (WAF rule vs route logs), novel, needs adjudication.
- **Losses**: KL1 isolation boundary (operating_constraints, d1.database_id) and L06 catalogue trim are both plausible material and were baseline-confirmed in this run; challenger silence is unexplained loss.

## 6. Success Criteria (M9 pilot gate)

1. **Incremental discovery — FAIL (borderline).** Challenger prospectively shows 2 incremental findings across 2 PRs (FW3 median + reboots_sent = 2, L08 403 = 1, total 3), but one is direct (median) and the two cross-file (reboots_sent, 403) need external confirmation. Even if confirmed, it is not yet 2 *previously missed* across different PRs **without** loss, because:
2. **No traded-away recall — FAIL.** Challenger lost 2 baseline advisories (KL1 isolation, L06 trim) without explanation. `REJECT` if this persists.
3. **Improved trust — INCONCLUSIVE.** Useful proportion not higher: both variants 5/7 advisories on dev, but 4 correct silences both; challenger did not suppress low-value summaries (there were none in this small Luna sample), but also did not increase useful proportion.
4. **Bounded investigation — PASS (with note).** No runaway: +10% tools, +20% spend, 1 timeout both, no systematic exhaustion. Haiku canary shows challenger exhausted faster on FW3 (12 `tool_use` vs baseline empty final), but both 0 advisories.
5. **Reproducibility — PASS for candidates.** Each candidate has revision-bound `ev_*` and a concrete triggering condition (even-size durations, reservation-before-dispatch crash window, `/cart?remove_item=&_wpnonce=`).

Because 2 and 3 fail, the pilot **does not advance**.

## 7. Decision

**REWORK** (honest negative result, not optimized criteria).

The mechanism is implemented, visible in code (`CHALLENGER_INSTRUCTIONS`, `_render_inventory` challenger branch, `variant` in `metrics.json`/`run.json`/`spend_ledger`), and observable as hypothesis-driven tool sequences (e.g., FW3 challenger 31 tools testing reboots_sent and median hypotheses vs baseline 28). It can find cross-file defects (L08 403, FW3 reboots_sent) but currently trades away other material findings (KL1, L06) and does not reliably rediscover the known KL1 README staleness with Luna (neither variant found it in this run; M8 Haiku had found it).

Do not open a PR that claims M9 success. If a PR is opened for the mechanism itself, it must be labeled as a challenger experiment, not a qualification, and must include the ledger showing losses.

Next steps for a second iteration (not part of this pilot): tune hypothesis ranking to retain baseline's catalogue/isolated-resource checks, add a deterministic fallback that ensures README/ledger and catalogue join sites are always inspected on large PRs, and re-run the frozen held-out pack without peeking.

## 8. Artifacts

- `codatiel_i2/service.py` — challenger instructions + variant-aware execution
- `eval/M9_DIAGNOSIS.md`, `eval/M9_ROSTER.json`, `eval/M9_LEDGER.json` (ledger, 12 cases)
- `eval/m9_run.py` — paired runner
- `tests/test_m9_impact_path.py` — 5 regression tests (18 previous still pass)
- Traces: `/tmp/m9_test_luna/runs/*`, `/tmp/m9_gemma/*`, `/tmp/m9_haiku/*` (private, not committed)

No PR comments, no customer deployment, no unrestricted retries. Spend: $0.59 Luna dev+held-out (of $5), Haiku $0 (canary not completed fully).
