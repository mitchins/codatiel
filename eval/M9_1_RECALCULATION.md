# M9.1 Phase A2 — Recalculation of M9 Aggregates from Run Artifacts

**Source of truth:** `/tmp/m9_test_luna/runs/**/run.json` +
`metrics.json` (`tool_calls`, `spend.usd_estimate`, `status`) +
`model_findings_raw.jsonl` line count (= ledger "advisories").
Deterministic detector `findings.jsonl` (56 docs_code etc.) is **not** the
advisory count. `publish_preview.json` is `report_only` (always 0).

## Per-case artifact inventory (Luna)

Single-run cases (artifacts match ledger tools/raw, spend differs noted):

| Case | Arm | Tools (artifact) | Raw (artifact) | USD (artifact) | USD (ledger) | Verdict |
|---|---|---|---|---|---|---|
| FW3 | base | 28 | 1 | 0.0547 | 0.042 | tools/raw match; spend +0.0127 |
| FW3 | chall | 31 | 2 | 0.0657 | 0.058 | tools/raw match; spend +0.0077 |
| RB7 | base | 26 | 1 | 0.0260 | 0.026 | match |
| RB7 | chall | 38 | 1 | 0.0615 | 0.061 | match (≈) |
| KL1 | base* | 32 | 1 | 0.0628 | 0.063 | ledger picked 1 of 3 baseline reps |
| KL1 | chall | 30 | 0 | 0.0453 | 0.045 | match |
| WT4 | base | 1 | 0 | 0.0004 | 0.008 | **spend 20× off** |
| WT4 | chall | 5 | 0 | 0.0016 | 0.012 | **spend 7× off** |
| SW6 | base | 11 | 0 | 0.0055 | 0.015 | spend 3× off |
| SW6 | chall | 10 | 0 | 0.0044 | 0.013 | spend 3× off |
| L06 | base | 36 | 1 | 0.0528 | 0.052 | match (≈) |
| L06 | chall | 34 | 0 | 0.0616 | 0.048 | **spend +0.0136** |
| L08 | base | 17 | 1 | 0.0081 | 0.022 | spend 3× off |
| L08 | chall | 40 | 2 | 0.0503 | 0.058 | spend −0.0077 |
| L04 | base | 14 | 0 | 0.0753 | 0.018 | **spend 4× off** |
| L04 | chall | 22 | 0 | 0.1640 | 0.025 | **spend 6× off** |
| SW7 | base | 15 | 0 | 0.0103 | 0.018 | SHAs differ (see below) |
| SW7 | chall | 22 | 0 | 0.0184 | 0.024 | SHAs differ |

Multi-run cases (ledger silently picked one rep; unrecorded variance):

- **KL1:** 3 baseline reps — (32t,raw1,0.0628), (34t,raw0,0.0756),
  (37t,raw0,0.0714). Ledger recorded only the finding rep. True
  single-shot discovery rate for the baseline KL1 finding: **1/3**, not 1/1.
- **L10:** 3 baseline reps — (40t,raw1,0.0521, tier-continue claim),
  (40t,raw1,0.0673, STORE-07 claim), (31t,raw0,incomplete,0.0946). 3
  challenger reps — (35t,raw1,0.0487), (38t,raw0,0.0462),
  (38t,raw1,0.0840). Ledger recorded one rep each. Finding content is
  **unstable across reps** (different claims, one silence each arm).
- **L01:** 3 baseline reps — (18t,raw1,complete,0.0782),
  (34t,raw1,complete,0.0869), (35t,raw0,incomplete,0.1253). **Zero challenger
  runs.** Ledger "baseline timeout / challenger not_run" is **false**: two of
  three baseline reps completed with a finding; the timeout was one rep, not
  the case outcome. Case is under-powered (48 files, 12 turns) but not
  uniformly exhausted.

## Reconciliation of the four flagged discrepancies

1. **Missing L02.** Roster lists `selectivetutor-website#61` (held-out) but no
   run directory exists for it under `/tmp/m9_test_luna/runs`, and the ledger
   `cases[]` array omits L02 entirely (11 entries for a 12-case roster).
   Status: **never executed**. No data was lost; the case was dropped without
   record. It must not be counted in any aggregate.
2. **Unpaired L01.** Ledger shows baseline `timeout` + challenger `not_run`.
   Artifacts show 3 baseline reps (2 complete) and 0 challenger reps. The
   "timeout" was one rep (`cb66158a`, 35 tools, incomplete, $0.1253), not the
   case. Correct record: **L01 baseline 2/3 complete with findings, 1/3
   incomplete; challenger never run — unpaired, excluded from paired
   aggregates.**
3. **Spend figures.** Ledger `spend_baseline 0.268 / spend_challenger 0.322`
   do not reproduce from artifacts. Summing the ledger-selected reps from
   artifacts: baseline ≈ 0.337, challenger ≈ 0.375 (L01 excluded, SW7
   included, L10/KL1 single-rep as ledger). WT4/SW6/L04/L08/L10 per-run spend
   differs by 3–20×, consistent with a rate-card change between the ledger
   write and the artifact `metrics.json` (or ledger hand-entry). **Neither
   figure is trustworthy without re-derivation.** Phase B must derive spend
   exclusively from `metrics.json` `request_ledger` × frozen rate card, with
   the ledger script as the sole writer (no hand entry).
4. **Ledger `run_dir`s are stale.** Every `run_dir` in `M9_LEDGER.json`
   (e.g. `/tmp/m9_test_luna/runs/6e2fe7daf6754ab4/...`) points at a
   non-existent path. Real dirs are listed above. The ledger was hand-written
   or copied from a discarded run root — **it is not a primary record.**

## SHA drift (why Phase A3 is required)

- **L06:** roster head `8466d6bc` (short) vs live PR head `4051ff05` and all
  run artifacts (`4051ff05`). Roster stale; runs reviewed the live head.
- **SW7:** roster `553bf80e→a138a529` is **wrong PR state**; live
  `scanweave#7` is `5b4ad8b3→ec1495ee` (verified via `gh`), matching the run
  artifacts. Roster confused PR6 base with PR7.
- L02 short SHAs match live PR (`d529cf4d→c392b612`); L04 shorts match live.
  FW3/L10/L01/RB7/KL1/WT4/L08 artifact SHAs match roster full SHAs.
- `service.review_pr` resolves SHAs live from `gh` with no frozen expectation,
  so drift is silent. Fix in Phase A3: `review_pr(..., expected_base,
  expected_head)` + `m9_run.py` passes roster SHAs, aborts on mismatch.

## Corrected paired aggregate (artifacts, single selected rep per arm, L01/L02 excluded)

- 9 paired cases with artifact-backed reps (WT4, SW7, SW6, RB7, FW3, KL1, L10,
  L04, L06, L08 = 10; L10 unstable, L01/L02 excluded → report 9 + L10 note).
- Advisory totals are unchanged in shape (dev 5v5, held-out 2v2) **only if**
  the ledger's cherry-picked reps are kept. Honest statement: with all reps,
  KL1 baseline is 1/3, L10 either arm is 2/3 with inconsistent claims, L01
  baseline is 2/3 complete. Single-run ledger numbers overstate stability and
  must not be used for the Phase B power calculation; Phase B uses 3 reps/arm
  and per-defect frequency.
