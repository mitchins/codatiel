# M9.1 Phase B — Pre-registration: Model-Capability Ceiling Test

**Question:** is Codatiel's material-regression discovery limited primarily by
model capability or by investigation planning?

## Design (frozen before any Phase B inference)

- **Cases (5 dev, all previously executed — no fresh-case leakage):**
  1. RB7 — confirmed cross-file regression (preview-flag gap; both arms
     found it in M9).
  2. FW3 — previously missed material (median + `reboots_sent`; challenger-only
     in M9).
  3. KL1 — large change with distractors (100 files; baseline-only isolation
     finding in M9, 1/3 reps).
  4. L08 — infra cross-file (LMS/Epcore ports + 403; arms diverge, see
     adjudication correction).
  5. WT4 — known clean negative control (both arms silence in M9).
- **Arms (identical frozen revisions, tools, `max_turns=12`, `tool_budget=40`,
  evidence rules; SHA gate from Phase A3 enforced):**
  - A: Luna baseline (`canonical-instructions-0.1`).
  - B: Luna challenger (`challenger-impact-path-0.1`).
  - C: stronger model baseline (`canonical-instructions-0.1` on GPT-6.1 Sol or
    comparable — **not yet configured; see blocker**).
- **Repetitions:** 3 per arm/case (45 runs), balanced execution order
  (interleaved A/B/C, rotating start), no retries, no cherry-picking: all reps
  recorded, per-defect frequency reported.
- **Revisions:** full 40-hex SHAs from artifacts (FW3 `956f7445…→d9b61df6…`,
  RB7 `b779ae9f…→66210790…`, KL1 `dbf06e00…→694269d5…`, L08
  `aa807b86…→aff2a068…`, WT4 `93336223…→f053c2f8…`); runner aborts on mismatch.
- **Measures (per arm/case, from artifacts only):**
  - confirmed material regressions recovered (against `M9_1_ADJUDICATION.md`
    defect list, independently re-checked);
  - false or immaterial findings;
  - reached-relevant-code-but-missed-consequence vs never-reached-code
    (from `tool_calls.jsonl` + `evidence.jsonl`);
  - tool calls, completion status, USD from `metrics.json` request ledger.
- **Adjudication:** all proposed comments anonymized and independently
  adjudicated before unblinding arm labels; agent does not mark its own
  comments useful.
- **Decision rule (pre-registered):**
  - C ≫ A on confirmed recovery with bounded cost → prioritize
    quality-backend config → new private shadow qualification.
  - C ≈ A and both miss the same paths → build one bounded generic
    investigation-frontier mechanism (deterministic changed-contract +
    plausible-consumer surfacing; no repo-specific names or bug heuristics).
  - Neither improves useful discovery → retain baseline, archive challenger,
    report the ceiling.

## Blockers (no Phase B inference until all cleared)

1. **Arm C does not exist.** `providers.py` `BACKENDS = (luna, haiku, gemma)`;
   no Sol/GPT-6.1 profile, no verified pricing, no privacy posture. Configuring
   it is itself a product decision requiring operator approval.
2. **Spend ceiling not approved.** Estimate from M9 artifacts ($0.004–$0.164 /
   run, mean ≈ $0.055 Luna): 30 Luna runs ≈ $1.65; 15 Sol runs at unknown rate
   (assume up to 5× Luna pending verification ≈ $4.15) → request **$7 ceiling**
   for Phase B inference, all-or-nothing with preflight refusal as in M9.
3. **L01/L02 excluded** (unpaired/never-run); F1–F6 fresh cases reserved and
   untouched by Phase B.

## Explicit non-goals

No repo-specific fallbacks (README/ledger/catalogue), no re-tuning on held-out
(L04/L06/L08 now dev-exposed), no retroactive M9 success claim, no public
comments, no deployment, no retries, no inference beyond the approved ceiling.
