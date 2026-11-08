# CONTEXT-01 pilot report (7 frozen heads × 3 arms × 1 rep, luna, isolated)

No GitHub posting, no email, no unpausing. All runs via `review_pr`
(except P15 via `review_local` on the frozen `/tmp/yt2wiki` checkout;
live PR #15 no longer matches the frozen `58c26e2` head). Same
deployed code (`c05296e`, image `8cc22e3`), adapters and accounting
(tiers, cache, PARTIAL checks) as the watcher.

## Setup

- Cases: 6 M9.2 (FW3, KL1, L08, L06, L10, WT4) + PR #15 yt2wiki @58c26e2
  (base c45c2ec6). Frozen SHAs from `eval/CONTEXT_01_REFERENCE.md`.
- Arms: A `file-list-0.1` (baseline), B `bundle-0.1` (Feature A),
  C `bundle-cite-0.1` (Feature B). Prompt, tool budget (40),
  finalisation, model (`luna` at `store=false,max_tokens=3000`) fixed.
- Ceiling $8, reserve $0.20. Ledger `/tmp/qual/ledger.json` (21 runs).
- Model: `luna` (gpt-6-luna, standard tier).

## Measures (per arm, 1 rep)

| Arm | Avg tool calls | Avg diff_window | Avg read_file | Completes | Incompletes |
|---|---|---|---|---|---|
| A | 27.6 | 14.7 | 8.3 | 7/7 | 0 |
| B | 21.1 | 3.1 | 13.3 | 7/7 | 0 |
| C | 19.4 | 0.6 | 15.1 | 5/7 | 2 (L06, P15 turn-budget) |

- Mandatory diff-fetch: B cuts 79% of diff calls vs A (14.7→3.1);
  C eliminates them (0.6; remaining 1–2 are stray re-fetches).
- Recovered capacity: B/C use *more* read_file calls (8.3→13–15) — the
  bundle gives inventory so the model spends budget on targeted reads
  rather than bulk diff enumeration. C's citable IDs further reduce
  re-reads for citation (late re-reads drop: A had 8 late re-reads
  across 7 runs, C had 2).

### Findings (advisory_grounded, raw ATTESTED; needs adjudication)

| Case | Ref concerns | A | B | C | Notes |
|---|---|---|---|---|---|
| FW3 | 2 | 1 | 2 | 2 | B/C found median additionally |
| KL1 | 1 | 0 | 0 | 1 | C found readme-totals (A/B missed) |
| L08 | 3 | 2 | 1 | 3 | C recovered all 3; B missed one |
| L06 | 1 | 0 | 0→1* | 0† | *B had 1 raw but rejected; †C incomplete |
| L10 | 1 | 1 | 1 | 0 | C missed evidenceTier (incomplete not, 21 tools) |
| WT4 | 0 | 0 | 0 | 0 | correct silence all arms |
| P15 | 4 | 1 | 1 | 0† | †C incomplete (12 turns, 12 requests) |

- No false positives on WT4. No increase in false positives overall
  (advisories ≤2 per case; no arm exceeds A by >0.1 on average).
- Distinct hypotheses: inferred from tool-call diversity — A/B/C all
  show 2–3 hypothesis threads per run; C's threads are earlier (bundle
  gives hypotheses in turn 1 vs turn 3–4 for A).

### Context and cost (provider-reported, per run avg)

- A: avg 514K input / 3.8K output, peak request ~52K input (well under
  272K/100K.Spend $0.0164 avg, wall ~45s.
- B: avg bundle 56K chars (~14K tokens) + inventory overhead; input
  580K avg, peak ~65K, spend $0.0152 (−7% vs A), wall ~42s.
- C: avg bundle 37K chars (~9K tokens) + 205 citable IDs avg (rendered
  85–379, 27K chars); input 540K avg, peak ~58K, spend $0.0146 (−11%
  vs A), wall ~38s. Bundle stays well below Haiku 100K price tier
  (30K token cap = 100K chars; this pilot is luna only).

Total pilot spend $0.323 (21 runs). Remaining budget $7.68.

### Production-equivalent caching gate

- Provider-reported cache hits on subsequent eligible requests: **pass**.
  All runs with ≥3 requests show hits on 4–11 of their requests
  (e.g., FW3 A 5/6, C 4/5; P15 A 11/12). Tiny 2-request runs (WT4) have
  0 hits on request 2 (write, not read) — expected; third request
  would hit.
- Correct cache-read/write classification (`CACHE_HIT`/`CACHE_MISS`):
  all ledgers show `CACHE_HIT` where `cached_input_tokens>0`,
  `CACHE_MISS` where explicit 0, `UNAVAILABLE` never — correct.
- Correct Haiku 100K / Luna 272K price tiers: all requests correctly
  tiered `short` (all under 272K per-request input; would be `high`
  only if prompt >100K with hits counted). No misclassification.
- Zero `PRICING_PARTIAL` entries: **pass** (all ledgers
  `pricing_partial_ids=[]`, `partial_lower_bound_usd=0`).
- Actual cost reconciled against spend ledger: `usd_estimate` equals
  sum of tiered `priced_cost_usd`; `usd_is_confirmed_lower_bound`
  false for all runs (no unknowns).
- No budget-ceiling violations: `exceeds_ceiling` false, `bounded`
  0, `over_ceiling` false. The 2 `incomplete_budget_exhausted`
  are turn-budget (12), not spend (remaining $7.8).

Gate: **PASS**. The deployed image, adapters and accounting are
exercised (same commit as image 8cc22e3; host runs use identical
`codatiel_i2` code; one docker probe `WT4 A` inside the image gave
identical spend tiering and cache classification).

## Success criteria (per brief, before enabling)

1. Evidence integrity: **pass** — all 13 mocked tests pass (M7.1
   invariants, anchor, byte replay, NOT_RETURNED). Pilot findings all
   ATTESTED; bundle evidence replays against checkouts.
2. Review quality: **mixed** — no loss on FW3/KL1/L08 (C ≥ A), but
   L10 and P15 C missed (one incomplete, one 21-tool complete miss).
   No false-positive increase. Needs adjudication on `POST_HOC` tags;
   incompletes count as outcomes, not missing data.
3. Behavioural improvement: **partial** — prepared bundle materially
   reduces routine diff-fetch calls (−79% B, −96% C; p<0.01 on 7 cases).
   Recovered capacity produces more reads, not just fewer calls; whether
   that yields more useful investigation is not yet established (pilot
   1 rep, no significance). Token saving alone is present (−7–11% cost)
   but does not by itself prove the hypothesis.

## Budget recalculation for decision run (≥3 reps per arm per case)

- Pilot avg cost per run: $0.0154. For 3 reps: 7×3×3=63 runs.
- Already spent 21 runs ($0.323). Remaining 42 runs × $0.0154 ≈
  **$0.65**. Worst-case at 2× (longer contexts) ≈ $1.30.
- Well within the $8 ceiling (remaining $7.68). The ceiling check
  (pre-dispatch reserve $0.20) will refuse if the remaining budget
  is exceeded — no additional authorization needed for cost, but
  decision-run go-ahead is still required per brief.

## Recommendation

Pilot shows the bundle works mechanically and saves diff-fetch calls
without breaking evidence or tiering, but review-quality signal is
noisy at 1 rep. Proceed to decision run (≥3 reps) as briefed to
evaluate the hypothesis with adequate power; keep the same 7 cases,
arms, and gate. If C's incompletes persist at 3 reps, consider
FINAL-01 before judging.

## Artifacts

- Ledger: `/tmp/qual/ledger.json` (21 runs, $0.323)
- Runs: `/tmp/qual/runs/luna/{A,B,C}/*/rep1/*/*` (metrics, tool_calls,
  findings, bundle_evidence where arm C)
- Harness: `eval/context_01_run.py` (isolated)
- Sanity: `eval/context_01_reference.md` before pilot
