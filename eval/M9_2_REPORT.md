# M9.2 — Reviewer Methodology Recovery: Report

**Verdict: `NO_IMPROVEMENT` on Luna; Haiku `BLOCKED`.** Retain the baseline
minimal prompt (arm A). Do not adopt the guidebook port. P5 not reached.
No costlier-model experiment justified.

## What was tested

Whether restoring a real investigation methodology (ported 01-series
guidebook, M9.2 P3) improves material-defect discovery over the minimal
prompt on real PRs, under the canonical runtime (SDK Runner, 7 I2 tools,
M7.1 evidence IDs). Prior: uncertain (guidebook failed on real PRs in 01E;
§0 table confirmed in RECOVERY.md). Three frozen arms, identical
turns/budgets/settings/SHAs: A minimal+file-list, B guidebook+file-list,
C guidebook+triage. 6 development cases × 3 arms × 3 reps on Luna (54/54
ran); Haiku A/B × 6 × 3 blocked by billing (33 attempts, $0 confirmed).

## Per-arm results (Luna, n=18 runs/arm)

| Arm | Material defect-reps | Mean hits/scored run | False accusations | Incompletes (/18) | Confirmed $ (+unconf. est.) | Tools / hit |
|---|---|---|---|---|---|---|
| A minimal | **14** | **1.00** (14 scored) | 0 | 4 (3 exhausted + 1 unparseable) | 0.59 (+0.32) | 24.8 |
| B guidebook | 6 | 0.35 (17 scored) | 0 | 1 (exhausted) | 0.60 (+0.11) | 74.5 |
| C guidebook+triage | 7 | 0.44 (16 scored) | 0 | 2 (empty finals) | 0.88 (+0.00) | 60.0 |

Decision rule: (1) ≥1.3× A and ≥+2 defect-reps absolute — B 0.43×/−8, C
0.50×/−7: **fail**. (2) false/run ≤ A+0.1 — pass (0 all). (3) no
high-materiality loss — pass strictly (KL1 isolation is the high item;
all arms missed it 0/9, so nothing to lose), but B lost reboots_sent
(A 3/3 → 0/3, moderate-high) and both lost trim (A 2/3 → 0/3). (4)
incompletes not worse by >1 in 18 — pass (B/C better than A). Rule 1
fails outright for both: **B and C are NO_IMPROVEMENT (worse, not tied).**

Consistency (reps/3, material defects): FW3-reboots A3 B0 C2; FW3-median
A1 B2 C0; L08-lms A3 B1 C1; L08-epcore A2 B1 C0; L06-trim A2 B0 C0;
KL1-readme A1 B0 C0; novel singles (preflight A1; compose B1 C1;
cloudflare B1 C1; 403 C1; readme-sub C1) — symmetric single-rep
novelties on all arms, i.e. noise, not an arm effect.

Controls: WT4 correct silence 9/9 (3/3/3 per arm); L10 tier-trap
correctly avoided 9/9; KL1 silences (B2 C2) and L06 silences (A1 B2 C2)
all deficient (missed reference defects). Arm A itself is unstable
across reps (median 1/3; restart 0/3 after M9's 1/1) — measurement noise
is large, but the A-vs-B/C margin (14 vs 6/7) exceeds it.

Taxonomy shift (P4 Luna, 99 pairs, same P2 rules): EXAMINED 57,
FOUND 22, NEVER 2, BUDGET 2, SUPPRESSED 7, CORRECT_NON_FINDING 9
(trap), MISREAD 0, FOUND_WRONG 0. Attention-side 4 vs reasoning-side 57:
the model reaches the decisive code and still does not convert — on Luna.

## Escalation criterion (recorded, not acted on)

Requires reasoning-side > attention-side on **both** Luna and Haiku.
Luna P4: 57 vs 4 (met). Haiku: BLOCKED, unevaluable. **Criterion not
met — no costlier-model experiment.** (Retrospective P2 had Luna 23v19,
Haiku 18v25.)

## What was not verified

- Haiku anything (billing block; 0 findings, 0 silences scored).
- Owner spot-check of ≥5 adjudication verdicts (required before any §7;
  moot — no P5).
- Whether B/C's single-rep novelties reproduce (compose, cloudflare, 403,
  readme-sub, preflight each 1/3 on their arm).
- F1–F6 untouched (diffs never opened; no checkouts).

## Spend vs ceiling ($10.00 total; P4 ≤$6.50)

Luna confirmed $2.08 + $0.43 unconfirmed estimates on 4 incomplete runs
(≈$2.50 real) over 54 runs; Haiku $0.00 confirmed. Total ≈ **$2.50 /
$10.00**. P5 unspent. Reserve rule and re-projection (after FW3: $0.39
for 9) held throughout.

## Records

- Freeze: `eval/M9_2_PREREGISTRATION.md` (arm IDs, corrected full-SHA
  case table, run plan, rule; one mechanical SHA-typo fix logged in
  git: WT4 head 38→40 hex).
- Evidence: `eval/M9_2_BLIND_ITEMS.json` (47, arm/model stripped),
  `eval/M9_2_BLIND_MAP.json` (sealed), `eval/M9_2_ADJUDICATION.md` +
  `.json` (34 findings, agent-performed development evidence),
  `eval/M9_2_P4_TAXONOMY.json`, `eval/M9_2_LEDGER.json` (generated;
  test-enforced), run dirs under `/tmp/m9_2` (private, referenced by
  path in the ledger).
- PR #6 stays unmerged and experimental. F1–F6 reservation stands.
