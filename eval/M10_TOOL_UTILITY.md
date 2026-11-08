# M10 P1.2 — Tool-utility and cost analysis (existing traces, $0 inference)

Source: `eval/m10_tool_utility.py` over all 90 executed M9.2 P4 runs
(`/tmp/m9_2`, archived with manifest at
`/data/coding/codatiel/archives/m9_2-20261023/`), joined with
`M9_2_BLIND_MAP.json` + `M9_2_ADJUDICATION.json` (59 verdicts: 49
CONFIRMED, 4 CONFIRMED-LOW, 6 PARTIAL, 0 REJECTED). M7 (24 traces,
4-tool era) for historical context only. M9 runs excluded: M9.1 A2
showed the M9 ledger is stale (spend 3–20× off, SHA drift) — recomputing
from M9 artifacts is not worth the budget; M9.2 P4 is the canonical
baseline.

Caveat: evidence citations show what the model *chose to cite*, not a
causal proof that the tool was necessary. Citation is a lower bound on
usefulness (a tool may contribute context without being cited) and the
model's citation habits differ by family (below).

## 1. Tool frequency (90 executed P4 runs)

| Tool | Calls (Luna 54 runs) | Calls (Haiku 36 runs) | Runs using it (/90) |
|---|---|---|---|
| diff_window | 651 | 262 | 89 |
| read_file | 480 | 235 | 89 |
| search_code | 315 | 362 | 78 |
| find_definition | 0 | 8 | 7 |
| find_callers | 0 | 3 | 3 |
| related_files | 0 | 2 | 2 |
| lookup_docs | 0 | 2 | 2 |

Luna never called a symbol/docs/related tool in 54 runs; Haiku used them
in ≤7/36 runs each. The investigation is effectively a 3-tool loop
(diff + read + search). Mean calls per executed run: Luna ~27, Haiku
~25 — both well under the 40-call budget; budget exhaustion is rare
(7/90) and concentrated in Haiku-B (3) and Luna-A (3).

## 2. Tools behind confirmed findings (evidence refs cited)

| Tool | Cited by CONFIRMED/LOW | Cited by PARTIAL |
|---|---|---|
| read_file | 99 | 6 |
| search_code | 58 | 11 |
| diff_window | 13 | 10 |
| find_definition / find_callers / related_files / lookup_docs | 0 | 0 |

No confirmed finding rests on symbol/docs/related evidence. That does
not prove them useless (models may not reach for them), but they
contribute zero measured downstream benefit across 90 runs and 53
confirmed-or-low findings. Family habit: Luna cites read_file (99 of
its 112 confirmed refs); Haiku cites search_code most (57 of 101).
Luna diffs heavily (651 calls, 12/run) yet cites diff output only
~5 times — the diff listing drives navigation, the read output carries
the proof.

## 3. Detector pipelines → verdicts (59 filed findings)

| Detector | CONFIRMED | CONFIRMED-LOW | PARTIAL |
|---|---|---|---|
| contracts | 28 | 1 | 0 |
| docs_code | 13 | 1 | 1 |
| operating_constraints | 7 | 0 | 5 |
| authorization | 1 | 0 | 0 |
| semantic_twins | 0 | 2 | 0 |

All 5 operating-constraints PARTIALs are the hedged Haiku observations
(status-api, boot-grace, shield-runbook, acl-files, STORE-06 gate).
The adjudicator rejected nothing the models filed — consistent with the
models' own verification filter, but "0 false accusations" still rests
on agent adjudication (owner spot-check pending since M9.2).

## 4. Deterministic docs_code candidates

38,619 deterministic docs_code candidates generated across 90 runs →
515 referenced in verified `findings.jsonl` (1.3%) → 15 authored as
model findings → 14 confirmed-or-low + 1 partial. The deterministic
layer over-generates by ~3 orders of magnitude; the model + verification
funnel is the entire precision mechanism. Candidate generation itself is
free (no model spend); keep it, but do not mistake candidate volume for
recall.

## 5. Completion, cost, and yield

| Config | Complete | Non-complete (/18) | Median $/complete | Median wall | Cost/confirmed defect-rep |
|---|---|---|---|---|---|
| Luna-A | 14 | 4 | $0.041 | 52 s | ~$0.065 (14 reps) |
| Luna-B/C | 33/34 | 1/2 | $0.036–0.041 | 56–69 s | — (not baseline) |
| Haiku-A | 13 | 5 | $0.082 | 166 s | ~$0.27 (10 reps) |
| Haiku-B | 9 | 9 | $0.065 | 124 s | — (not baseline) |

Token scale per 18-run cell: Luna ~8M in / 70k out; Haiku ~10–12M in /
~400k out (Haiku emits ~5× the output tokens). Haiku costs ~2× Luna per
completed run and ~4× per confirmed defect-rep, with worse completion.

Distinct root-cause defects confirmed (both models, all arms,
case-level dedup): **21** (22 labels; `L06-readme-sub` was recorded once
as ref_item and once as novel — adjudication naming drift, counted
once). 37 case-level detections across 59 filed findings → the same
defect is re-found across reps: **do not count reps as recall**.

## 6. M7 context (not comparable, recorded for continuity)

24 traces, 4-tool menu (diff_window 109, read_file 125, search_code 90,
find_callers 4 — no definition/docs/related tools existed): 17
complete, 3 budget-exhausted, 4 failed. Same 3-tool dominance two
harness generations earlier; the symbol tools were added since and
remain unused by both model families.

## Bottom line for P1.3/P2

- The usable evidence pipeline is diff+read+search. Symbol/docs/related
  tools are dead weight *as currently prompted* — candidates for removal
  or for a targeted repair experiment, not for the M10 qualification
  path (prompt is frozen).
- Baseline economics: Luna-A ≈ $0.04 + ~1 min per completed review,
  ≈ $0.065 per confirmed defect-rep; Haiku-A ≈ 2× cost, 3× wall time,
  worse completion.
- 21 distinct confirmed defects exist to power the P1.3 union-vs-repeat
  comparison; reps must be deduplicated by defect, not counted raw.
