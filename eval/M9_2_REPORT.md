# M9.2 — Reviewer Methodology Recovery: Report

**Verdict: `NO_IMPROVEMENT` on Luna and on Haiku.** Retain the baseline
minimal prompt (arm A). Do not adopt the guidebook port. P5 not reached.
The §8 costlier-model **escalation criterion is MET** (reported, not acted
on — M9.2 runs no Sol experiment).

## What was tested

Whether restoring a real investigation methodology (ported 01-series
guidebook, M9.2 P3) improves material-defect discovery over the minimal
prompt on real PRs, under the canonical runtime (SDK Runner, 7 I2 tools,
M7.1 evidence IDs). Prior: uncertain (guidebook failed on real PRs in 01E;
§0 table confirmed in RECOVERY.md). Frozen arms, identical
turns/budgets/settings/SHAs: A minimal+file-list, B guidebook+file-list,
C guidebook+triage (Luna only). Luna: 6 cases × 3 arms × 3 reps (54/54
ran). Haiku: 6 cases × 2 arms × 3 reps (36/36 ran after a billing block
was topped up; 33 pre-top-up attempts failed pre-response, $0, excluded).

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

## Per-arm results (Haiku, n=18 runs/arm)

| Arm | Material defect-reps | Mean hits/scored run | False accusations | Non-complete (/18) | Confirmed $ | Tools / hit |
|---|---|---|---|---|---|---|
| A minimal | **10** | **0.77** (13 scored) | 0 | 5 (empty finals) | 1.25 | 29.8 |
| B guidebook | 8 | 0.89 (9 scored) | 0 | 9 (4 empty + 3 exhausted + 2 unparseable) | 0.97 | 23.1 |

Decision rule: (1) ≥1.3× A and ≥+2 — B 0.8×/−2: **fail**. (2) false —
pass (0 both; PARTIALs are not accusations: status-api, boot-grace,
shield-runbook, acl-files, STORE-06-gate). (3) no high-materiality loss
— pass (A≥2/3 defects: readme 2/3, lms 2/3, readme-sub 2/3; B never
0/3 on any of them). (4) incompletes not worse by >1 — B worse by 4:
**fail**. **B is NO_IMPROVEMENT on Haiku.** Invalid-final rate
(empty+unparseable): A 5/18 (28%), B 6/18 (33%) — at but not over the
1/3 stop line; reported, Luna-only continuation was not needed (campaign
complete).

Consistency (Haiku reps/3): L08-lms A2 B2; L06-readme-sub A2 B1;
KL1-readme A2 B1; FW3-compose A1 B0; constants-docs A0 B2;
archive-tokens A0 B1; intro-lookup A1 B0; runbook-epcore A1 B0;
L08-epcore A0 B1; probe-modes A1 B0. WT4 silence 5/5 scored (A3 B2 —
one B run exhausted); L10 tier-trap never filed as defect; KL1
silences (A1 B1) deficient. Haiku found KL1-readme 3/9 (vs Luna 1/9)
and the archive-token defect Luna missed — model, not arm, effects.

## Escalation criterion (recorded, not acted on)

Requires reasoning-side > attention-side on **both** models, best arm
(A): Luna A 15 vs 2; Haiku A 20 vs 2. **Criterion MET on both — a
costlier-model experiment is justified whenever the owner approves a
budget for it.** (Retrospective P2 had Luna 23v19, Haiku 18v25; P4
reverses the Haiku side once billing-blocked runs are excluded.)
M9.2 does not run that experiment.

## What was not verified

- Owner spot-check of ≥5 adjudication verdicts (required before any §7;
  moot — no P5). Suggested: M92-002a, M92-024, M92-049, M92-057a,
  M92-063a (proof lines in the adjudication record).
- Whether single-rep novelties reproduce on either model.
- F1–F6 untouched (diffs never opened; no checkouts).

## Spend vs ceiling ($10.00 total; P4 ≤$6.50)

Luna confirmed $2.08 + $0.43 unconfirmed estimates on 4 incomplete runs
(≈$2.50 real); Haiku confirmed $2.22 + ~$0.44 unconfirmed estimates on
14 non-complete runs (≈$2.66 real; 33 billing-blocked attempts $0).
Total ≈ **$5.16 / $10.00** (P4 ≈ $5.16 ≤ $6.50). P5 unspent. Reserve
rule and re-projections (Luna after FW3; Haiku after FW3: ~$0.09/run)
held throughout.

## Records

- Freeze: `eval/M9_2_PREREGISTRATION.md` (arm IDs, corrected full-SHA
  case table, run plan, rule; two mechanical fixes logged in git: WT4
  head SHA typo, spend rule for billing-blocked runs).
- Evidence: `eval/M9_2_BLIND_ITEMS.json` (69; arm/model stripped,
  first-47 IDs stable across regeneration), `eval/M9_2_BLIND_MAP.json`
  (sealed), `eval/M9_2_ADJUDICATION.md` + `.json` (59 findings,
  agent-performed development evidence), `eval/M9_2_P4_TAXONOMY.json`,
  `eval/M9_2_LEDGER.json` (generated; test-enforced), run dirs under
  `/tmp/m9_2` (private, referenced by path in the ledger).
- PR #6 stays unmerged and experimental. F1–F6 reservation stands.
