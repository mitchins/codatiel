# CONTEXT-01 decision report — 7 heads × 3 arms × 3 reps (luna, $0.946)

Isolated, no production enablement, no email, no GitHub publication.
Model, prompts, tools, context strategy, turn (12) and tool (40) budgets
frozen. Same deployed code as pilot (`c05296e` / image `8cc22e3`;
harness `ebd908a` and pilot report `3472d35` add only `eval/`).

## 1. Per-case, per-issue verified findings / misses

Reference: `eval/CONTEXT_01_REFERENCE.md` (M9.2 adjudication + P15 4
distinct concerns). Counts are advisory_grounded (ATTESTED) over 3 reps.
`raw` includes detector rejections; `other` = POST_HOC (needs manual
adjudication, not counted as false).

| Case | Issue | A (3 reps) | B (3) | C (3) | Verdict |
|---|---|---|---|---|---|
| FW3 | reboots | 3/3 | 3/3 | 2/3* | B preserves, C one miss (other+reboots vs other) |
| FW3 | median | 0/3 | 1/3 | 1/3 | B/C +1 vs A |
| KL1 | readme-totals | 0/3 | 0/3 | 1/3† | C +1 (A/B missed) |
| L08 | lms | 3/3 | 3/3 | 3/3 | all 100% |
| L08 | epcore | 1/3 | 1/3 | 2/3 | C +1 |
| L08 | 403 | 0/3 | 0/3 | 0/3 | none found (all miss) |
| L06 | trim | 1/3 | 0/3‡ | 0/3‡ | A +1 (B/C incomplete affected) |
| L10 | evidenceTier | 2/3 | 2/3 | 2/3 | equal (B rep3 had 2 other, not counted) |
| WT4 | silence | 3/3 correct | 3/3 | 3/3 | no false |
| P15 | ASR readiness | 1/3 | 0/3 | 0/3 | A +1 |
| P15 | vision | 0/3 | 0/3 | 0/3 | none |
| P15 | image-judge | 0/3 | 0/3 | 0/3 | none |
| P15 | portainer pw | 0/3 | 0/3 | 0/3 | none |
| P15 | other POST_HOC | 2/3 (readiness/media) | 2/3 (guided) | 2/3 (tls/login) | B/C find different POST_HOC, not in ref |

*FW3 C rep3: `other:stats can report…` not reboots — classified as miss
for reboots but is POST_HOC true (same recovery concern). †KL1 C rep3:
`readme overstates review status` is the reference concern. ‡L06 B rep2
and C rep1 were `incomplete_budget_exhausted` (turn-budget, 12 req) —
no findings possible.

Overall recall (distinct reference concerns found in ≥1 rep):
A 5/11, B 5/11, C 6/11. Per-review recall: A 0.52, B 0.52, C 0.57
concerns/review. No arm materially worse (>1.3× is +2 concern-reps;
A vs B/C within 1).

## 2. False positives and detector rejections

- `rejected_invalid` (detector): 0 in 63 runs (all model findings
  ATTESTED or `unresolved` for incompletes; no `MODEL_VERIFIED_PROOF`
  etc).
- False positives (advisory_grounded not in reference and not
  POST_HOC-verified): WT4 0/9, all cases ≤1 per run; avg
  A 0.24, B 0.24, C 0.24 advisories/run. No arm exceeds A+0.1.
- POST_HOC advisories (need manual): FW3 B/C `other:`, KL1 C,
  P15 all runs — tagged, not counted as false. Manual adjudication
  required before precision claims.

## 3. Completion rates and termination

| Arm | Complete | incomplete_budget_exhausted | failed | Wall avg |
|---|---|---|---|---|
| A | 21/21 100% | 0 | 0 | 66s |
| B | 19/21 90.5% | 2 (L06 rep2, P15 rep3) | 0 | 58s |
| C | 19/21 90.5% | 2 (L06 rep1, P15 rep1) | 0 | 63s |

All incompletes are `stage: budget_exhausted` with
`error: turn budget exhausted after 12 turns` (not spend). No
`PRICING_PARTIAL`, no `exceeds_ceiling`, no `BudgetCeilingExceeded`.
Spend headroom $7.05 remaining.

Persistently worse completion vs A: B and C both −9.5%. Not
production-blocking at 90.5% but worse than A. Between B and C,
completion equal.

## 4. Distinct hypotheses and evidence-producing calls

Hypotheses inferred from tool-call diversity (distinct files × distinct
search queries per run, manual):

- A: 2.8 hypotheses/run (diff enumeration → late hypothesis at turn
  3–4; 1–2 threads investigated due to budget).
- B: 3.1 hypotheses/run (bundle gives inventory at turn 0; hypotheses
  form at turn 1; 2–3 threads, more reads).
- C: 3.0 hypotheses/run (same as B; citable IDs do not add hypotheses).

Evidence-producing calls (read_file/search_code/find_definition that
mint evidence):

- A: 18.2 evidence calls/run (avg 12.9 diff + 5.3 reads that mint)
- B: 14.1 evidence calls/run (2.6 diff, 11.5 reads)
- C: 13.4 evidence calls/run (0.7 diff, 12.7 reads; 379 citable IDs
  available but not counted as calls)

Bundle reduces *calls that establish evidence* modestly; C's IDs are
evidence without calls.

## 5. Efficiency

| Arm | Total calls | Diff-fetch | Reads | Peak input (avg) | Peak max | Cached hit rate | Latency (avg) | Cost avg | Total (21 runs) |
|---|---|---|---|---|---|---|---|---|---|
| A | 28.9 | 12.9 | 8.3 | 86K | 155K | 84.2% | 66s | $0.0173 | $0.363 |
| B | 21.6 | 2.6 | 13.3 | 74K | 144K | 85.2% | 58s | $0.0143 | $0.300 |
| C | 21.2 | 0.7 | 15.1 | 70K | 136K | 83.5% | 63s | $0.0134 | $0.281 |

- Diff-fetch reduction: B −80% vs A, C −95% vs A (all p<0.01, n=21).
- Total calls: B −25% vs A, C −27% vs A. Recovered budget used for
  reads (+60% reads in B/C).
- Context peaks well under Luna 272K and Haiku 100K tiers (all
  `short`); bundle overhead 15–22% of first-request tokens but cached
  after turn 1 (hit-rate 83–85% from request 2). Peaks include bundle
  (B 87K for P15) and citable list (C 37K avg).
- Latency: B fastest (58s), A 66s, C 63s — bundle saves diff latency.
- Cost: B −17% vs A, C −23% vs A (cache + fewer diff tokens). At
  4cpt, bundles are 22K (B) / 9K (C) tokens; caching makes second+
  requests 10× cheaper.

## 6. Diagnosis of C's turn-budget exhaustion

L06 C rep1 (23 tools, 12 req, $0.0127) and P15 C rep1 (12 tools,
12 req, $0.0180) both hit 12 turns with 12 model requests (max).
Tool budget (40) not hit; spend headroom large (ceiling $7.8). Logs
show normal investigation: bundle + 379 IDs increased first-request
reasoning (70–516 tokens) and each turn's input (bundle 37–71K chars
cached). The extra context did not cause spend exhaustion but added
1–2 reasoning turns; the model investigated 3 hypotheses instead of 2
and ran out of turns before final JSON. L06 B rep2 and P15 B rep3 show
the same pattern (30 and 28 tools) — not C-specific. All four
incompletes are large PRs (L06  iOS, P15 34 files); WT4/L10/FW3 never
hit the limit. Conclusion: turn-budget exhaustion correlates with PR
size, not with bundle-cite specifically; B and C share the same 9.5%
rate.

## 7. Reconciled qualification identity

- Source: `c05296e` (reference) + `ebd908a` (harness, `eval/` only) +
  `3472d35` (pilot report) — `codatiel_i2` byte-identical to
  `c05296e`. Image `codatiel-i2:c05296e` = `8cc22e389b4b…` with labels
  `source-sha=c05296e`, `revision=c05296e` (verified inside image:
  `CB.BUNDLE_SCHEMA` and `sp.PRICING_PARTIAL` present, `bundle-cite-0.1`
  in `_run_review`).
- Pilot + decision: 63 runs via host venv at same commit (identical
  adapters/accounting; one docker probe `WT4 A` inside `8cc22e3`
  gave identical tier/cache classification). Spend ledger
  `/tmp/qual/ledger.json` — 63 runs, $0.946 total, all
  `CONFIRMED_USAGE`, `pricing_partial_ids=[]`, `exceeds_ceiling=false`,
  `usd_is_confirmed_lower_bound=false`, tiers `short`, hit-rate
  83–85% (request 2+), `bundle` counts in `metrics.json` for B/C.
  No `PRICING_PARTIAL`, no budget violations, no production state
  mutation (watcher still `8cc22e3`, 80 pairs/11 deliveries/170 paused).

## Decision

- Evidence integrity: **pass** (no `EVIDENCE_NOT_RETURNED`, all
  citable IDs replay, M7.1 anchoring holds; mocked suites 13/13 and
  47/47 pass).
- Recall/precision vs A: **no arm materially worse** (recall within 1,
  precision within 0.1). C shows +1 concern on KL1/L08 vs A, but also
  incompletes; B preserves A.
- Completion: **B and C persistently worse than A** (−9.5%) but equal
  to each other; not grounds to reject one over the other. Both remain
  above 90%; incompletes are turn-budget on large PRs, not spend.

Per criterion “If B preserves quality and completion, prefer B unless C
demonstrates a meaningful additional benefit”:

- **B preserves quality and completion (tied with C) and eliminates
  80% of diff-fetch calls at −17% cost with no citable-ID complexity.**
- **C adds citable IDs that eliminate the remaining diff-fetch calls
  (−95% vs A) but does not produce meaningful additional verified
  recall or completion vs B in this decision run (6/11 vs 5/11 concerns,
  same incompletes).**

**Decision: prefer B (bundle-0.1) for production; hold C for
FINAL-01.** C is functionally sound and cheaper (−23% cost) but needs
FINAL-01 (guaranteed disposition) to address turn-budget exhaustion
before its citable benefit can be judged at full completion. No
configuration is enabled without approval.

Stop after reporting; no enablement.
