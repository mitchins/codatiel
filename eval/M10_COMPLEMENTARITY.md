# M10 P1.3 — Cheap-model complementarity: union vs repeat ($0 inference)

Source: `eval/m10_complementarity.py`. Arm A (retained minimal prompt)
only. Declared pairing, no post-hoc best-rep selection: cross-family =
mean over rep-index pairs (Luna repk ∪ Haiku repk); repeat = mean over
the 3 unordered within-model rep pairs. Defects deduplicated by
adjudicated label (`M9_2_ADJUDICATION.json` ref_item/novel; one naming
drift normalized: `novel:L06-readme-sub` → `L06-readme-sub`).
CONFIRMED primary, CONFIRMED-LOW included in unions and reported.

## Mean confirmed defects per case (6 development cases)

| Configuration | Defects/case | 2nd-run marginal | Cost/pair | $/marginal defect |
|---|---|---|---|---|
| 1× Luna | 0.78 | — | ~$0.05 | — |
| 1× Haiku | 0.61 | — | ~$0.07 | — |
| Luna + Haiku (cross) | **1.28** | +0.44 | $0.120 | ~$0.27 |
| Luna + Luna (repeat) | 1.22 | +0.17 | $0.101 | ~$0.60 |
| Haiku + Haiku (repeat) | 1.06 | +0.45 | $0.139 | ~$0.31 |

Per-case detail (cross / repeat-L / repeat-H): FW3 2.00/2.33/0.67;
KL1 1.33/1.33/1.67; L06 1.67/1.00/1.67; L08 2.00/2.00/1.67;
L10 0.67/0.67/0.67; WT4 0/0/0 (silence control, correct everywhere).

## Decision

**Cross-family is not meaningfully better than a same-spend second
run.** Luna+Haiku (1.28) vs Luna+Luna (1.22) vs Haiku+Haiku (1.06):
the gaps are within rep noise on 6 cases, and the marginal gain of the
second run is identical whether it is cross-family (+0.44) or a Haiku
repeat (+0.45). The only asymmetry: Luna repeats itself (+0.17 —
Luna's reps re-find the same defects), so Luna×2 is the worst way to
spend the second-run budget ($0.60/marginal defect).

False/low-value: no REJECTED verdicts exist in any configuration (0
false accusations everywhere, with the standing caveat that
adjudication was agent-performed). PARTIAL hedged notes attach to
Haiku-A (5; operating-constraints hedges) and Luna-B (1); Luna-A has
none.

Completion: only 10/18 cross pairs had both runs complete (Haiku
empties); a two-model strategy must define behavior when one side is
incomplete — union of available outputs, not silent downgrade to one.

**Preference order for M10: cheapest effective single model (Luna-A:
0.78 defects at ~$0.05/run, no partials, 1-min wall), with a
conditional second cheap run (Haiku, not Luna — Luna repeats itself)
only where the first run is incomplete or a high-risk area is
unresolved.** No elaborate router: the trigger is mechanical
(incomplete first review), not content-based.
