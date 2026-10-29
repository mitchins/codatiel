# REVIEW-TERRA-01 — Terra vs Luna B+FINAL (PARTIAL: 7/27, provider-capped)

Question: are we getting substantially more useful code review from
Terra than Luna? **On the completed 7 runs: Terra surfaces more genuine
defects per review, but reference recall is tied, Terra adds its own
false positives, costs ~8.6× Luna, and 20/27 runs (including every
large/complex case) never ran. No promotion. Evidence below; stop.**

## 1. Status: stopped by provider project cap, not the $5 ceiling

- 7/27 reviews complete (WT4×1, FW3×3, KL1×3), spent **$0.8281 / $5.00**.
  L08×3 failed pre-inference with `429 project_spend_limit_exceeded`
  (usd 0.0, correctly uncounted); L06/L10/P15/PR25/PR27 never started.
- The cap is project-wide (Code Review project): Luna verified blocked
  too (same 429, $0 spend). Production unaffected (watcher paused,
  170/170 repos paused, no dispatch possible).
- Sanitized ledger: `eval/TERRA_01_EVIDENCE.json` (10 rows incl. 3
  failed; finding-level rows with independent adjudication).
  Artifacts: `/tmp/terra/{runs,work}/terra/B/*`, ledger
  `/tmp/terra/ledger.json`.

## 2. Configuration parity

B arm preserved exactly: `bundle-0.1`, same instructions, tools,
evidence rules, 12 turns, 40 calls, FINAL-01 39/11 reserve +
`enable_final`. Only the model changed (`gpt-6.1-terra`) plus strictly
necessary integration: `terra` backend in `providers.py`/`runtime_sdk.py`
(plain model id + `store=false`, same as Luna), verified official rate
card added 2026-10-28 (input $1.00/M, output $5.00/M, cached $0.05/M,
write $1.25/M; long >272K 2×/1.5× — ~10× Luna), tier accounting
extended to `terra` with Luna's OpenAI semantics. One disclosed
difference: Luna harness used `max_tokens=3000`; Terra used uncapped
`store=false` (no model-specific tuning either way).
Verified live: Responses API tool calling, structured-output final,
prompt-cache hits from req2, CONFIRMED_USAGE accounting with priced
tiers (smoke + all 7 runs). Privacy assumption flagged: same project
key presumed `STANDARD_API_NO_TRAINING`; confirm before any scale-up.
Luna controls: existing B+FINAL runs (`/tmp/qual-final`, same bundle,
budgets, final) for FW3/KL1/WT4 — parity holds for these cases.

## 3. Per-case and aggregate known-defect recall: Terra vs Luna (tie)

| Reference issue | Terra (3 reps) | Luna B qual-final (3 reps) |
|---|---|---|
| FW3-reboots (store.py reserve→reboot) | **3/3** | **3/3** |
| FW3-median (store.py:843-846 upper-middle) | 0/3 | 0/3 |
| KL1-readme-totals (README:61 48/32 vs ledger 67/73) | 0/3 | 0/3 |
| WT4 silence | 1/1 correct | 3/3 correct |

No recall advantage either way on the completed cases. The two
secondary references are missed by both models (median and
readme-totals live in PR-new report/spec text both models walked past).

## 4. New genuine defects found exclusively by Terra (5, all verified)

All PR_INTRODUCED (rc2 / PR #1 scope confirmed against base):

1. **FW3 compose /data migration** (3/3 reps) — base mounts
   `./data:/data`, head mounts named `watchdog_data:/data`, 24-file
   diff contains no migration: upgrading installations lose latches,
   cooldowns, attempt budgets (moderate; Luna 0/3).
2. **FW3 latched_incidents count** (reps 1,3) — `report()` counts
   `state IN ('latched','acknowledged')`: resolved disappear,
   acknowledged-but-never-latched counted; mechanism exact (minor).
3. **FW3 restart attribution** (rep 3) — `outage_rebooted` is
   runtime-only (set at reservation); restart re-adopts latch state
   but not the flag → SELF_RECOVERED despite durable attempt row
   (minor).
4. **KL1 fixture 65v64** (rep 1) — `range-probe.txt` is 65 bytes;
   three corpus cases expect `Content-Range: bytes 0-7/64`
   (byte-verified; low-moderate).
5. **KL1 D1 UUID check** (rep 1) — probe manifest validator compares
   created resources by (type, name) only, ignoring recorded IDs
   (minor).

## 5. Genuine Luna findings missed by Terra (2 confirmed + 1 plausible)

1. **FW3 global inhibition ignores stability** (Luna rep 2) — new-in-PR
   gate `any_flowing` counts any flowing camera as healthy peer with no
   stability duration (policy.py:1332-335, absent at base). Terra's
   phase-halt finding is a *different* gate that does enforce
   stability. TRUE_POSITIVE, PR_INTRODUCED — Terra missed.
2. **FW3 0.0.0.0 example bind** (Luna rep 2) — example flips to
   `0.0.0.0` while `http_api.py` registers /health|/stats|/history|
   /report with no auth (verified: `add_get` ×4, no auth checks).
   Host publish is loopback-only, so low severity but genuine config
   hazard. Terra missed.
3. **KL1 archive 8MiB probe cap** (Luna rep 3) — plausible (Worker has
   only per-request + aggregate caps, no central-directory-specific
   cap) but the frozen-contract side not located: UNRESOLVED-leaning-true.

## 6. False positives

- **Terra advisory FPs (2 distinct):** observe-mode simultaneity
  (documented intended semantics, policy.py:792-808); phase-halt
  stability (peer stability IS enforced; cited guarantee holds while
  dark). **Unresolved (5):** FW3 outage-timer/frame-flaps, KL1
  pair-validator, KL1 archive-concurrency ×2 (mechanisms match code,
  violated-contract side not located).
- **Contract class (deterministic validator, MODEL-INDEPENDENT):**
  identical issue_keys for Luna and Terra (FW3: same 14; KL1: same 30;
  all reps). Spot-verified systematically false for both: /health,
  /stats, /history, /report ARE implemented (inverted claims);
  CAMERA_PORCH_PASSWORD IS consumed; SCORM IS provided (head README +
  AGENTS + contract tests); README-m0 mention ≠ readme-totals issue.
  Same /media/ failure mode as CANARY-TRUTH-01 (prose/caps-words
  converted to phantom contracts). Shared detector debt, not a Terra
  regression — but it dominates finding counts (14+30 verified_proof
  per run) and Terra's 0.97 confidences on it are miscalibrated.
- **/media/ repeat:** NOT repeated in completed runs — but no /media/
  case ran (PR25/PR27 unrun). Untested on the triggering pattern.

## 7. Distinct findings across repetitions (advisory_grounded)

- Terra FW3 distinct: 6 (rep1) → 7 (reps 1–2, +flaps) → 8 (reps 1–3,
  +restart). Luna FW3 B: 1–3 → 3 → 3 ({reboots, inhibition, bind}).
- Terra KL1 distinct advisories: 2 → 3 → 5. Luna KL1 B: 0 → 0 → 1.
- Verified_proof classes are repetition-invariant (same IDs all reps,
  both models).

## 8. Completion, cost, cache, latency (7 runs)

- Terra: **7/7 complete, 0 finalized_under_budget, 0 inconclusive,
  0 incomplete.** (Luna needed finals on L06/P15-class PRs — untested
  for Terra; the completion comparison is incomplete where it matters.)
- Cost: Terra **$0.118 avg/run** ($0.0065 WT4 – $0.201 KL1) vs Luna
  B+FINAL $0.0137 avg → **~8.6×**. At this burn the 27-run block
  projects ≈ $2.5–3.2 (within $5, but large-case variance unmeasured).
- Cache/accounting: all `short` tier, 0 partial, 0 unknown on priced
  runs; cache-read hits from req2 every run (4–8 hit reqs). CONFIRMED
  except finalized-shaped runs (same conservative UNKNOWN rule as Luna).
- Latency/effort: Terra avg **102s wall, 25 tool calls** vs Luna B
  ~58s, ~22 calls.

## 9. Finding catalog (abridged; full text in evidence JSON)

- FW3 Terra: reboots_sent (`store.py:830-835`), compose /data
  (`compose.yaml:24-26`), latched count (`store.py:836-839`),
  restart attribution (`policy.py:523-536`), observe simultaneity
  (`policy.py:750-808`, FP), phase-halt (`policy.py:965-981`, FP),
  outage-timer (`policy.py:655-661`, UNRESOLVED).
- KL1 Terra: fixture range (`corpus.json:95,150,205` + 65-byte asset),
  D1 UUID (`run_deployed_probe.py:116-133`), pair modes
  (`run_deployed_probe.py:162-166`, UNRESOLVED), archive concurrency
  (`worker.js:261-272,325-330`, UNRESOLVED), 30 contract rows (FP class).
- Luna-only: inhibition (`policy.py:1332-335`), bind
  (`config.example.yaml:55-62` + `http_api.py:144-147`),
  probe cap (`worker.js`, plausible).

## Decision (stop — do not promote)

Terra earns its keep on FW3 depth (4 genuine exclusive finds incl. a
moderate upgrade hazard) but: reference recall tied, both secondary
references missed by both, Terra adds 2 advisory FPs + 5 unresolved,
the dominant contract-FP load is shared, cost is 8.6×, and the cases
that would test Terra's completion edge (L06/L10/P15/PR25/PR27 — where
Luna needed budget finals) never ran. **Recommendation: no promotion;
rerun-or-continue the remaining 20 after the project cap reset, then
decide. If the cap cannot be raised, treat this partial as the result:
Terra is a stronger finder and a noisier one, at 8.6× the price.**
No PAYLOAD-01, no C, no new architecture; qualification artifacts
isolated from production (watcher untouched, all repos stay paused).
