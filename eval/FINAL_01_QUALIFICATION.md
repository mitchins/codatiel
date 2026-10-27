# FINAL-01 qualification — A+FINAL vs B+FINAL, 7×2×3 luna, $0.621

No production enablement, no PAYLOAD-01, budgets frozen, $2 cap respected,
completed-clean vs incomplete preserved via `final_tag`.

## Implementation

- Reserved tools-disabled final disposition: when turn budget (12) is
  exhausted via `MaxTurnsExceeded` (investigation gets 11 turns, final
  gets 1) or status is `incomplete_no_tool_use`/`unresolved_*`, a final
  turn with `tools=[]` is executed. Prompt lists available evidence IDs;
  findings must cite only those. Valid `finding`/`silence`/`inconclusive`
  (with reasons) are accepted and tagged `finalized_under_budget`;
  invalid JSON, missing evidence, or provider interruption keep the
  original incomplete. `inconclusive` triggers Haiku follow-up same as
  `incomplete` (HAIKU_TRIGGER extended). Parser now accepts
  `inconclusive`; it is distinct from `complete` and from `incomplete`.
  If investigation completed cleanly (finding/silence) with 39/11 budget,
  no final is done — preserving completed-clean vs inconclusive.

## Qualification — identical frozen heads, budgets, 3 reps

Cases: FW3, KL1, L08, L06, L10, WT4, P15 (yt2wiki @58c26e2, local).
Arms: A+FINAL `file-list-0.1` + final, B+FINAL `bundle-0.1` + final.
Ceiling $2 for this block, spent **$0.621** (42 runs, avg $0.0148), total
with prior $0.946 = $1.567 <$8. No implementation changes during the
42 runs (fixed code `109ee2e`, image `ddfaa3d...`).

### 1. Per-issue verified findings / misses (advisory_grounded, 3 reps)

| Case | Issue | A+FINAL (3) | B+FINAL (3) | Notes |
|---|---|---|---|---|
| FW3 | reboots | 2/3 | 3/3 | B preserves |
| FW3 | median | 0/3 | 1/3 | B +1 |
| KL1 | readme | 0/3 | 1/3 | B +1 |
| L08 | lms | 3/3 | 3/3 | 100% |
| L08 | epcore | 1/3 | 1/3 | equal |
| L08 | 403 | 0/3 | 0/3 | none |
| L06 | trim | 0/3 | 0/3 | none (both had 1 finalized) |
| L10 | evidenceTier | 0/3 | 2/3 | B better (A had 1 inconclusive) |
| WT4 | silence | 3/3 correct | 3/3 | no false |
| P15 | 4 historical / 1 at H5 + 4 POST_HOC | 0/3 | 0/3 | none verified (POST_HOC only, excluded) |

Historical-reference (12 distinct concerns across PR evolution) vs
verified-defect recall at frozen heads: P15 contributes 4 to the
historical 12 but only 1 (vision preflight) is present at frozen H5
`58c26e2`; the 4 additional P15 advisories found are POST_HOC
(readiness/media, guided save, tls, login) and excluded from recall.
Recall per-review above is verified-defect recall (advisory_grounded
matching a present defect), not historical-reference agreement.

Recall per-review: A+FINAL 0.33, B+FINAL 0.48. No arm materially worse
(1.3×/+2).

### 2. False positives / detector

- `rejected_invalid` 0/42, WT4 0/6 false, avg advisories/run 0.81 A+FINAL
  (17/21) vs 0.76 B+FINAL (16/21) — from immutable
  `eval/FINAL_01_42RUN_EVIDENCE.json` (33 total). No `MODEL_VERIFIED_PROOF`.

### 3. Completion

| Arm | Complete | Inconclusive (final) | Incomplete | Wall avg |
|---|---|---|---|---|
| A+FINAL | 20/21 95.2% | 1/21 4.8% (L10 rep3) | 0 | 62s |
| B+FINAL | 21/21 100% | 0 | 0 | 58s |

Previously without FINAL: A 21/21, B 19/21 with 2 incomplete. With
FINAL, **no incomplete** — all 42 have a disposition. Inconclusive is
via `finalized_under_budget` and is distinct from `complete` (metrics
`final_tag`). No `incomplete_*`, no `unresolved_*`.

### 4. Hypotheses / evidence calls

- Hypotheses: A+FINAL 3.0/run, B+FINAL 3.1/run — equal.
- Evidence calls: A+FINAL 20.1/run (12.9 diff), B+FINAL 14.8/run
  (2.6 diff, −80% diff, −26% total) — same as without FINAL; final
  adds 0 evidence calls (tools=[]).

### 5. Efficiency

| Arm | Total calls | Diff | Reads | Peak avg | Hit-rate | Latency | Cost avg |
|---|---|---|---|---|---|---|---|
| A+FINAL | 30.1 | 12.9 | 8.5 | 89K | 84% | 62s | $0.0158 |
| B+FINAL | 22.4 | 2.6 | 13.1 | 76K | 85% | 58s | $0.0137 |

B+FINAL −26% total, −80% diff, −13% cost vs A+FINAL; final adds
~$0.001 per finalized run. All `short` tiers, hit-rate 84–85% from
req2.

### 6. Diagnosis

L06 and P15 previously had `incomplete_budget_exhausted` (12 turns,
40 tools) at $0.012–0.029, well under $8 — turn-budget, not spend.
With FINAL (reserve 1/1), the same large PRs now complete via
`finalized_under_budget` — exactly 4 in the fixed-code 42 runs
(`eval/FINAL_01_42RUN_EVIDENCE.json`, `final_tag` set): L06 A rep1
(complete, 0 advisories, $0.0198), L06 A rep2 (complete, 0 advisories,
$0.0186), L06 B rep3 (complete, 0 advisories, $0.0166), L10 A rep3
(inconclusive, 0 advisories, $0.0290) — no `incomplete` remains. (An
earlier pre-fix draft listed 7 including P15 A; that belonged to the
superseded $0.667 block and is corrected here.) No spend
violation; turn budget was the limiter, now handled.

### 7. Identity

- Source `109ee2e` (this commit, FINAL-01 with reserve), image
  `codatiel-i2:109ee2e` (`ddfaa3d...`, labels `source-sha=109ee2e`),
  built from `109ee2e` (code identical to host runs). Qualification
  ran via host venv at `109ee2e` (identical adapters/accounting; prior
  docker probe WT4 A inside `8cc22e3` verified tier/cache).
- Ledger `/tmp/qual-final/ledger.json` (42 runs, $0.621) persisted as
  sanitized `eval/FINAL_01_42RUN_EVIDENCE.json` (42 sanitized IDs,
  wall, final_tag, spend). No `PRICING_PARTIAL`, all `short`, hit-rate
  84–85% from req2, `usd_is_lower` false, `exceeds_ceiling` false.
  Completed-clean (`complete` without tag) vs `inconclusive`/`complete`
  with `finalized_under_budget` preserved.

## Decision

- Evidence integrity: **pass** (no `NOT_RETURNED`, all final citations
  subset, mocked 5/5 pass).
- Recall/precision vs A+FINAL: B+FINAL not materially worse.
- Completion: B+FINAL **better** (100% vs 95.2% with 1 inconclusive);
  no persistently worse.

**Recommendation: enable B+FINAL (bundle-0.1 + FINAL-01).** Hold C and
PAYLOAD-01 pending soak. Cap respected ($0.621 < $2). No enablement
without approval.
