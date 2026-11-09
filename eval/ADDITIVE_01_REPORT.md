# REVIEW-ADDITIVE-01 — Final Report: Preserve Terra, Add Independent Coverage

Branch `exp/additive-01`. No model promotion. No production changes.
New inference spend **$2.58 / $4.00** ($2.00 live + $0.58 Haiku qual).

## Verdict: GO for Terra+Luna additive; HOLD for Haiku

The additive architecture is **BETTER**: it preserves every validated
Terra finding (sealed primary, nothing removed), adds an independently
validated P2 (plus P3s), holds zero merge false-positives, completes
11/12 runs (1 clean), and keeps the Luna cost at **+8%** of Terra-alone.

## Phase 1 — Offline counterfactual (no inference; existing artifacts)

For each of 30 matched (case, rep) pairs: complete Terra findings from
D01 arm A ∪ Luna blind-sweep findings from arm B sweeps
(`sweep-luna.txt`), validated against the frozen D01 RC table (ground
truth re-verified where novel). Follow-up-only contributions identified
via session DBs and delegation ledgers — never counted as sweep finds.

**Blind-sweep validated gains vs Terra-alone** (all PR-introduced):

- P2 RC-L10-CODABLE (2/2 reps; present in both Luna sweeps — genuine
  independent discovery).
- P3 RC-Y24-ANNOT, RC-P15-RECAP, RC-Y15-AUTOCAP (newly validated:
  auto-captioned videos rejected although the pipeline can use their
  subtitle track), RC-L06-README (newly validated: README references a
  deleted enum).

**Follow-up-observed gains** (in-session, directed — real but not
guaranteed in an independent sweep): P3 SCORM (KL1), P3 XUNIT (L10),
P3 Y22-400 (parent-directed hypothesis, Luna-confirmed with evidence).

**Haiku (C evidence):** blind sweeps 1/8 substantive (KL1-r2: three
P2-hedged probe-only robustness notes, config-unverified) — no reliable
independent value. Haiku follow-ups DID surface the Y15 P2 login finding
(substantive passage, both reps), so the value is follow-up-observable
only. Failed blind sweeps: 4/30 Luna (13%), 7/8 Haiku.

**Costs (actuals):** Luna sub $0.51/30 = **$0.017/run**. No sweep FPs
(2 UNRESOLVED: FW3-FLAPS, KL1-INSERT).

**Gate: PASS** — blind sweeps add a P2 + P3s with zero FPs at +7% cost.
Proceed to implementation.

## Phase 2 — Minimal additive implementation

`codatiel_i2/additive.py`: Terra runs canonical FIRST (unchanged
model/tools/prompt/budget, no delegation tools) and seals (`run.json` +
`shadow_result.json` immutable). Luna then sweeps blind against the same
immutable snapshot (PR-scoped session retained post-seal for follow-ups;
`fresh=False` continuation tested). Consolidation is deterministic:

- Luna findings arrive under an output contract (format only; identical
  investigative discipline).
- Each finding is anchor+evidence-validated at pinned revisions
  (span → quoted-excerpt (60%+) → per-line fallback → cited-file
  support), deduplicated vs Terra (same file + overlapping lines +
  shared substance), appended with provenance — or rejected with reason.
- Any subagent/merge failure degrades to Terra-only; Terra failure never
  masks (terra-only-primary).

Two validator recall bugs were found live and fixed with tests (10 pass):
non-contiguous excerpt quotes and `"..."`-joined multi-source evidence
initially rejected genuine finds (CODABLE, DISK, SAVE). Corrected merges
were recomputed deterministically from sealed inputs and versioned as
`additive-corrected.json` alongside as-run records — no inference
re-spent, both versions reported.

## Phase 3 — Live verification (6 PRs x 2 reps, additive Luna)

Roster: Y15, L08, L10, FW3, P15 + Y22 (KL1 blocked: upstream PR head
force-pushed mid-campaign; sha_mismatch guard refused both reps as
designed — documented in FREEZE.md with both SHAs).

- 11 additive complete + 1 clean (Y22 r1; Y22 r2 clean) of 12; 0 merge FPs.
- Terra primaries preserved baselines (live RC sets match A-arm coverage,
  incl. EPCORE/LMS P1s; Y22-404 even found live by Terra r1).
- Luna added 7 instances / 5 RCs, all previously validated:
  P2 CODABLE x2, P3 MEDIAN, P3 DISK x2, P3 RECAP, P3 SAVE-TEST (facet of
  Terra's own SAVETEST — same RC, server vs UI facet; disclosed, not a
  duplicate comment on one instance).
- Duplicates correctly suppressed (5, incl. AUTOCAP folded into CAPFLAG);
  remaining rejects all Terra-covered (zero recall loss in corrected
  merges).
- Cost: parent mean $0.154, additive total mean $0.177 → **+$0.023/run
  (+15% here, +8% on the $2.00 campaign; D01-B measured +7%)** — within
  the ~25% bound.
- Retention/cleanup/session isolation: covered by the 7 D01 session
  tests + post-seal session persistence test; live sessions queryable,
  per-tag isolation intact; follow-ups resume (38/38 in D01).

## Haiku qualification: FAIL → HOLD (0/6, $0.58)

Five empty + one MaxTurnsExceeded sweep on the SAME six cases with the
output contract. No subset evaluation per the mission rule. Haiku stays
HOLD: sweep unreliability (not finding quality) is the blocker.

## Success criteria

1. Terra findings preserved — yes (seal; live coverage matches control).
2. Independently validated P0/P1/P2 added — yes (CODABLE P2, blind +
   live, deterministic merge).
3. FP/completion acceptable — yes (0 merge FPs; 11/12 + 1 clean).
4. Cost within ~25% — yes (+8–15%).

**GO** for the Terra+Luna additive path (sealed primary, blind sweep,
deterministic merge). **HOLD** for Haiku. No further campaigns.

## Reproduction

`tests/test_additive.py` (10 pass, free). Live (paid, capped):
`additive-01/scripts/run_add03.sh luna <CASE> <REP>` with
`ADD_ROOT/ADD_CEILING_USD/ADD_RESERVE_USD` set; roster + ceiling guards
as before. Offline re-consolidation: `add03_reconsolidate.py`
(deterministic, no inference). Sanitized `eval/ADDITIVE_01_RUNS.json`
(12 runs) + `eval/ADDITIVE_01_FINDINGS.json` (7 instances / 5 RCs);
raw traces + session DBs stay in `additive-01/` (700).
