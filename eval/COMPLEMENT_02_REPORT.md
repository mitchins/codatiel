# REVIEW-COMPLEMENT-02 — Complementarity, severity and Sol reference

**Question: which combination discovers the most genuinely important unique
defects, at what incremental cost, and does Sol materially change that answer?**

**Answer: Luna primary with selective Terra escalation.** Terra alone finds
28/38 validated root causes; adding Luna (+$0.013/PR) catches 4 more P3s;
adding Haiku (+$0.085/PR) catches 5 more including the only P2 anyone found
that Terra missed — but Haiku completes only 52% of reviews. Neither
secondary dominates (Stage C: tie). Sol changes nothing architecturally:
7/38 recall at 1.6× Terra's cost with 31 misses including two P1s; its one
exclusive (a P2 outage-counting defect) is real but cannot carry $0.206/PR.
No promotion is executed; owner-rated material usefulness remains separate.

Campaign spend: **$4.517 / $12.00** (Luna $0.3595, Haiku $2.3005, Sol
$1.857). Terra's $3.3793 belongs to the prior frozen campaign and is not
re-spent. 63 new reviews, 0 ceiling breaches, 0 prompt changes, Sol ran
fully blind to all prior findings and ledgers.

## 0. Provenance and frozen inputs

- Baselines immutable: Terra 27 results (`TERRA_01R_REPORT/EVIDENCE.json`);
  Luna FINAL-01 42-run ledger as historical corroboration only.
- Nine cases, exact SHAs, B+FINAL everywhere (bundle-0.1, 12 turns,
  40 tools, FINAL-01 39/11 reserve). Backend deltas only: Luna
  `max_tokens=3000`, Haiku adapter default 8192 (Anthropic-mandated),
  Terra/Sol uncapped + `store=false` (reasoning effort default, untuned).
- Lost raw traces (prior `/tmp` wipes) listed in `complement-02/FREEZE.md`;
  nothing reconstructed. New raw traces private at
  `complement-02/traces/` (700, SHA-256 manifests); only sanitized
  JSON committed here.
- Sol backend integration (providers/runtime_sdk/profile/tier card,
  official $2/$10/$0.10 pricing verified 2026-11-09) is the only product
  code touched; no prompts/tools/evidence/detectors changed.

## 1. What each arm found (best-of-3, 9 common cases)

38 validated root causes (P1 3, P2 13, P3 22) + 4 unresolved + 5
nonactionable classes. Per-model validated sets:

| Model | RCs | P1 | P2 | P3 | $/run | Wall | Complete |
|---|---|---|---|---|---|---|---|
| Terra (frozen) | 28 | 3 | 11 | 14 | $0.125 | 45s | 27/27 (100%) |
| Luna (new) | 10 | 1 | 2 | 7 | $0.013 | 53s | 25/27 (93%) |
| Haiku (new) | 8 | 2 | 1 | 5 | $0.085 | 82s | 14/27 (52%) |
| Sol (1 pass) | 7 | 2 | 4 | 1 | $0.206 | 25s | 9/9 (100%) |

FP instances: Terra 12/80 (incl. 6 deferred-binding LOWs now nonactionable
+ 2 converged FPs + PR27 original framing), Luna 2/15, Haiku 0/12,
Sol 0/7. Phantom `verified_proof` contamination present in all arms
(shared detector debt, excluded from discovery math throughout).

Per-case best-of-3 (validated RCs, T/L/H/Sol):

| Case | Terra | Luna | Haiku | Sol | Notable |
|---|---|---|---|---|---|
| WT4 | 0 | 0 | 0 | 0 | silence everywhere, correct |
| FW3 | 4 | 2 | 0 | 2 | Sol adds OUTAGES (P2, exclusive) |
| KL1 | 2 | 1 | 0 | 0 | pair-validator unresolved ×3 models |
| L08 | 4 | 1 | 2 | 1 | refs shared (Haiku finds both); Terra +2 exclusive |
| L06 | 4 | 1 | 1 | 1 | Terra sweeps incl. trim 3/3 |
| L10 | 2 | 1 | 0 | 1 | Sol converges on matcher facet |
| P15 | 9 | 3 | 4 | 2 | Terra dominates; Haiku 4 distinct P3/P2 |
| PR25 | 2 | 0 | 0 | 0 | Terra-only minors |
| PR27 | 1 | 1 | 1 | 0 | docstring RC found by all except Sol |

Single-review (rep1) recall: Terra 22, Luna 4, Haiku 8, Sol 7. Repetition
buys Terra +6 and Luna +6 RCs; Haiku gains nothing from reps 2-3.

## 2. Unions and incremental cost (vs cheapest baseline: Luna $0.013/PR)

| Configuration | RCs | +vs Luna | +P2+ | $/PR | $/added RC | $/added P2+ | Missed P1/P2 |
|---|---|---|---|---|---|---|---|
| Terra only | 28 | +22 | +9 | $0.125 | — (alt base) | — | 1 P2 (LOGIN) |
| Terra ∪ Luna | 32 | +22 | +9 | $0.138 | $0.09 | — (0) | 1 P2 (LOGIN) |
| Terra ∪ Haiku | 33 | +27 | +10 | $0.210 | $0.46 | $2.30 | 0 |
| Terra ∪ Luna ∪ Haiku | 37 | +27 | +10 | $0.223 | $0.36 | $2.66 | 0 |
| Sol only | 7 | −3 | — | $0.206 | dominated | — | 1 P1 + 9 P2 |
| Sol ∪ Luna | 16 | +6 | — | $0.219 | dominated | — | still 20+ |

Luna-exclusive: BIND, README, L10-REF, PROFILE (all P3).
Haiku-exclusive: LOGIN (P2), ONERROR, DENO, DOCSGATE, SETTINGS (P3).
Both secondaries but missed by Terra: **none** (pair-validator is
UNRESOLVED, corroborated ×3 models — the nearest near-miss).
Terra-only: 21 RCs.

## 3. P15 readiness-cluster answer (mission's focus question)

Terra's 8 P15 defects are **8 independent defects (distinct remedies),
4 sharing the readiness feature**: CLI-identity, env-ASR vacuity, identity
overwrite, gate-after-copy (readiness machinery); plus reprocess-captions
(lifecycle), session invalidation, save-before-test, proxy-scheme. Luna
corroborates 2 (env-ASR, reprocess — same RCs, independent discovery);
Haiku adds 4 distinct (docs-gap, login-throttle, settings-exposure, Deno).
Not one shared root cause; the feature is hole-prone, each hole needs its
own fix.

## 4. Sol reference round

- Sol alone: 7 RCs (P1 2, P2 4, P3 1), 0 FP, 9/9 complete, $0.206/run.
- Overlaps: 6/7 already in Terra (independent corroboration, strongest on
  the L10 matcher facet); vs Luna-rep1 +6, vs Haiku-rep1 +6 (excl. LOGIN).
- **Sol-exclusive: RC-FW3-OUTAGES (P2)** — per-event outage counting with
  verified double-emit across UNKNOWN telemetry gaps; base lacks the
  counting. Real, moderate, one defect.
- **Missed by Sol: 31 validated RCs**, including P1s (EPCORE, MIGRATION —
  wait: Sol FOUND migration; missed EPCORE + ... : Sol RCs are MIGRATION,
  OUTAGES, LMS, GUARD, MATCHER, CLIIDENT, RECQUAL. Missed P1s: EPCORE
  only (LMS+MIGRATION found). Correction: Sol missed 1 P1 (EPCORE) and
  9 P2s (REBOOTS, UUID, TRIM, STALE, COOKIEMASK, ENVASR, SESSION,
  SAVETEST, LOGIN) plus PR27-docstring all three small models found.
- Sol single pass cost 1×$0.206 vs three-rep unions (Terra 3×$0.125,
  Luna 3×$0.013, Haiku 3×$0.085 per case): Sol is cheaper than Terra's
  union and costlier than Luna/Haiku unions, while finding a fraction of
  each union (7 vs 28/10/7) — and Haiku's union ties Sol's count with a
  P2 Luna and Sol both miss.
- Verdict: high-capability challenger, **not a reference oracle** — 31
  misses disqualify oracle status; 0 FP + 1 exclusive P2 confirm
  capability. Architecturally dominated (see §5).

## 5. P0/P1 discoveries, misses, unresolved

- **P1 found:** MIGRATION (Terra, Sol), LMS (Terra, Luna, Haiku, Sol),
  EPCORE (Terra, Haiku). No P0 found by any arm.
- **P1 missed:** EPCORE missed by Luna and Sol; MIGRATION missed by Luna
  and Haiku; LMS missed by none.
- **Unresolved (all arms):** KL1-PAIR (3-model corroborated mechanism,
  end-to-end acceptance unproven), KL1-ARCH, FW3-TIMER, FW3-FLAPS.
  Nonactionable: cart-403 variant (no real trigger), phase-halt +
  observe-simultaneity (both-model converged misreads), STORE
  deferred pair, P15 fill.

## 6. Architecture recommendation (no execution)

Decision table (per PR; “serious” = P0/P1/P2):

| # | Configuration | $/PR | +RC vs Luna | Serious missed | FP burden | Reliability | Complexity |
|---|---|---|---|---|---|---|---|
| 1 | Terra only | $0.125 | +19 | 1 P2 | 15% inst. | 100% | single model |
| 2 | Terra + Luna | $0.138 | +23 | 1 P2 | +2 inst. | 93% 2nd | trivial union |
| 3 | Terra + Haiku | $0.210 | +24 | 0 | +0 | 52% 2nd | waste handling |
| 4 | T + L + H | $0.223 | +28 | 0 | +2 | mixed | union + triage |
| 5 | Sol only | $0.206 | −3 | 1 P1+9 P2 | 0 | 100% | single model |
| 6 | Sol + Luna | $0.219 | +7 | 20+ | +2 | 93% 2nd | union |
| 7 | **Luna + selective Terra escalation** | **≈$0.02–0.04** | **+19 on escalated** | **≈0 on escalated** | low | 93% + review | router rules |

**Recommended: #7.** Keep Luna ($0.013) as the universal default;
escalate mechanically to Terra where Luna is incomplete/finalized/
inconclusive, on large/complex PRs (L06/P15/PR25-class), or flagged
high-risk areas. Expected cost stays near Luna's (escalation fraction
small) while capturing Terra's depth where Luna is weakest — consistent
with both M10 (conditional second run on incompleteness/risk) and
TERRA_01R (selective escalation). Universal Terra+Luna (#2) is
defensible where +10% budget is free (+4 P3). Universal Haiku (#3) is
not: +68% cost for a 52%-complete secondary whose inconclusives are
mostly empty. Sol appears in no recommended configuration: dominated at
every price point (its one exclusive P2 does not carry $0.206/PR; a
targeted Sol re-review of outage-counting code is cheaper as a point fix
to the report function, already specified by RC-FW3-OUTAGES remedy).
Standing caveats: owner-rated material usefulness still separate and
required; nine cases cannot support sweeping claims — per-case table (§1)
is the evidence, aggregates are summaries.

## 7. Residual unknowns and extrapolations (labeled)

- Observed: all counts, costs, latencies, completion rates above.
- Adjudicator judgments: severities, P3-vs-nonactionable lines
  (PROFILE, ONERROR, DOCSTRING-framing), PR27 revision rationale.
- Extrapolations: per-PR cost projections assume current burn; escalation
  fraction unknown (depends on PR mix); single-review recall from rep1
  sets.
- Unknowns: live-model drift since runs; Luna novel-finding text lost
  (counts only); whether registry gates pair evidence (KL1-PAIR);
  operator intent on enhanced-default and onError signals.
