# REVIEW-TERRA-01R — Terra vs Luna B+FINAL (COMPLETE: 27/27, $3.3793/$5)

Question: are we getting substantially more useful code review from
Terra than Luna? **On the full 27-run roster: Terra finds more distinct
genuine defects (23 vs ~9) and completes every large/complex PR without a
budget final, but reference recall still favors Luna (6/10 issue-slots vs
4/10), Terra adds its own false positives, and Terra costs ~9.1x Luna.
No default replacement. Targeted escalation use is justified on
difficult reviews where Luna is weakest. Evidence below.**

Predecessor: `eval/TERRA_01_REPORT.md` (PARTIAL 7/27, provider-capped).
This report supersedes its decision section; its 7 completed results and
all adjudications stand unchanged (raw traces for those 7 were lost with
`/tmp` — see §1 — results preserved via the frozen sanitized ledger).

## 1. Recovery: what survived, what was lost, what was rebuilt

- Baseline: repo `main @ 09c7708`, clean. All runs imported
  `codatiel_i2` from the canonical tree (venv installed package
  byte-identical to HEAD; `PYTHONPATH` forced to repo; per-run
  provenance assert; the dead `/tmp/codatiel-public` sys.path entry in
  `terra_qual.py` is inert — path does not exist).
- Sanitized ledgers survived in git and reconcile exactly:
  `TERRA_01_EVIDENCE.json` (10 rows: 7 complete + 3 zero-cost L08 429s,
  spent $0.8281) and `FINAL_01_42RUN_EVIDENCE.json` (42 Luna runs).
- **Lost:** raw run artifacts for all 7 original Terra runs and all 42
  Luna runs (`/tmp/terra`, `/tmp/qual-final` wiped). Execution traces,
  transcripts and per-request ledgers for those runs are
  unrecoverable. Completed *results* (verdicts, findings, spend,
  adjudications) survive in the sanitized ledgers and were not rerun.
- **Rebuilt with SHA verification:** `/tmp/repos/needletail` (L10
  base+head ✓), `/tmp/repos/mdmwi` via SSH (L06 base+head ✓),
  `/tmp/yt2wiki` (P15/PR25/PR27 all 6 SHAs ✓). WT4/L08 PRs still OPEN
  with live SHAs exactly matching frozen (review_pr SHA-match enforced).
  One self-caused scare: a mistyped SHA in a check loop looked like a
  missing PR27 base object; byte comparison against the runner config
  and `merge-base` confirmed all objects present. No gaps.
- Operational ledger was re-seeded from the sanitized evidence
  ($0.8281, 10 rows, completed slots locked) and the frozen runner
  `eval/terra_qual.py` was used **unmodified** (roster guard + checkpoint
  wrapper only, outside the repo).

## 2. Design preservation and drift assessment

- Frozen config held: `gpt-6.1-terra`, B+FINAL (`bundle-0.1`,
  12 turns, 40 tools, FINAL-01 39/11 reserve + `enable_final`),
  `store=false`, same prompts/tools/evidence rules/pinned revisions.
  Luna `max_tokens=3000` vs Terra uncapped preserved as disclosed.
- SDK `openai-agents 0.24.0` == lock. The CLOSURE-02 provenance fix is
  **behavior-identical for the Terra arm** (native provider provenance
  was asserted before and after; only unflagged adapters change).
  No prompt/tool/budget/evidence/detector change in the path.
- Live-model drift (provider-side `gpt-6.1-terra` changes since October)
  cannot be excluded and is recorded as a residual comparability caveat.
- Privacy posture unchanged: `store=false`, same project key
  assumption as TERRA-01 (still unconfirmed — confirm before scale-up).

## 3. Roster execution (20/20 new, $2.5512 added, ceiling respected)

Order: WT4 probe (provider acceptance + accounting validity), then
L08/L06/L10/P15/PR25/PR27. **20/20 complete, 0 failed, 0 inconclusive,
0 finalized_under_budget, 0 partial/unknown requests.** Every run
CONFIRMED_USAGE, short tier, cache-hit from req2. Cumulative spend
$3.3793 / $5.00 (per-review gates and $0.20 reserve held throughout;
largest single review $0.2014). The 3 original 429s are retained as
zero-cost infrastructure failures; their retries (L08 r1-3) ran clean.

| Case | Terra (3 reps) | Tools | Wall avg | $/run |
|---|---|---|---|---|
| WT4 (r2,r3 new) | 3/3 silence | 3-4 | ~6s | $0.007 |
| L08 | 3/3 complete | 23-27 | ~30s | $0.138 |
| L06 | 3/3 complete | 29-39 | ~47s | $0.119 |
| L10 | 3/3 complete | 25-31 | ~38s | $0.155 |
| P15 | 3/3 complete | 28-29 | ~41s | $0.144 |
| PR25 | 1 silence + 2×1 adv | 18-21 | ~26s | $0.178 |
| PR27 | 3×1 adv | 15-17 | ~21s | $0.111 |

Terra campaign averages: 23.2 tools, 5.7 requests, ~344K input tokens
(cumulative), ~31s wall, $0.128/run — vs Luna B+FINAL 22.4 tools, 58s,
$0.0137/run (**~9.1× cost**). Terra never invoked FINAL-01 in 27 runs;
Luna B needed one budget final in 21 (L06 B rep3).

## 4. Adjudication method and provenance

Every new advisory was verified by direct code reading at the pinned
BASE/HEAD (mechanism quote + base-vs-head provenance). P15 mechanical
checks were delegated read-only (no inference); all judgments below are
the adjudicator's. Model identity is irrelevant to mechanism checks.
Disagreements are preserved, not smoothed: the PR27 docstring claim
(model: defect ×3, adjudicator: false positive) and the L10 STORE pair
(model: defect, adjudicator: low-value — deferred binding) stand as
scored-against-Terra entries. Deterministic `verified_proof` rows are
never counted as model discoveries (§9).

## 5. Reference recall: Terra vs Luna B+FINAL (frozen cases)

| Reference issue | Terra (3 reps) | Luna B (3 reps) |
|---|---|---|
| FW3 reboots | **3/3** | **3/3** |
| FW3 median | 0/3 | **1/3** (FINAL-01 record) |
| KL1 readme-totals | 0/3 | **1/3** (FINAL-01 record) |
| L08 lms | **3/3** | **3/3** |
| L08 epcore | 1/3 | 1/3 |
| L08 403 | 0/3 | 0/3 |
| L06 trim | **3/3** | 0/3 |
| L10 evidenceTier (exact 226-240 anchor) | 0/3 | **2/3** |
| WT4 silence | 3/3 correct | 3/3 correct |
| P15 vision-preflight | 0/3 | 0/3 |

Net: Luna leads on frozen references (6/10 issue-slots vs 4/10).
Terra's one reference win (L06-trim, PR-introduced, exact anchor match)
is real; its L10 miss is exact-anchor strict (Terra found a neighboring
facet of the same gate — §6 — but not the anchored defect).
**Discrepancy flagged:** TERRA_01 §3 scored Luna median/readme 0/3 while
FINAL_01_QUALIFICATION scores them 1/3. The FINAL-01 record governs
here; raw Luna traces are lost so the discrepancy cannot be relitigated.

## 6. New genuine defects found exclusively by Terra (18 distinct, PR-introduced)

- **L06 (4, all verified PR-introduced — base lacks the files):**
  trim (reference, §5); loading-guard race (guard cleared before async
  StoreKit completes, 3/3); stale purchasable options after failed
  refresh (3/3); duplicate catalogue keys vs first-match purchase
  resolution (2/3, minor).
- **L08 (2):** WP-login rate limiter bypassed by self-set
  `wordpress_logged_in_` cookie substring (3/3, moderate);
  TLS-terminating vhost without WP HTTPS fixup (2/3, low-moderate).
- **L10 (2):** integration-tier matcher accepts pure evidence with a
  bogus WIRE-01 pinning citation — WIRE-01 actually *requires*
  integration (3/3, low-moderate); new crash test requires
  `/usr/bin/sqlite3`, breaking the pure/no-IO quick gate under CI's own
  provisioning (3/3, low-moderate).
- **P15 (8):** CLI/gate identity inconsistency (3/3, moderate);
  env-ASR marked Ready with no probe on default Compose (3/3,
  moderate); sessions survive password change (3/3, moderate);
  save-test persists before probing while UI says "Not saved" (2/3,
  moderate); record_qualified identity overwrite race (3/3,
  minor-moderate); reprocess drops captions (2/3, minor);
  Secure-cookie downgrade under documented Caddy deployment (2/3,
  low-moderate); readiness gate runs after upload copy (2/3, minor).
- **PR25 (2, 1/3 each, minor):** fail-closed refactor discards valid
  `best` on malformed `second`; section source-ref lost when body
  normalizes to empty (both confirmed vs base).

Pre-existing genuine (low value for a regression qual): P15 caption
bypass, P15 single-model fill. Low-value (correct, deferred binding):
L10 STORE-02/STORE-07-09 probe adequacy (DEFERRED_BY_PLAN).

## 7. Genuine Luna findings Terra missed (unchanged + 3)

FW3 global-inhibition bypass, FW3 0.0.0.0 example bind (both
CONFIRMED), KL1 8MiB probe cap (plausible) — none re-tested (frozen
slots). Plus reference recalls in §5: median, readme-totals,
evidenceTier anchor. Luna's L08/L06/L10/P15 novel-finding text is lost
with `/tmp/qual-final` (only counts survive); no Luna-exclusive novel
claim can be verified or refuted beyond the frozen references.

## 8. False positives and unresolved

- **Terra FPs (3 distinct):** FW3 observe-simultaneity + phase-halt
  (frozen, documented intended semantics); **PR27 hash-revalidation
  docstring (new, 3/3)** — the trust claim is substantiated
  (`_is_valid_upload`: "authoritative only if the file exists and
  matches the hash"); dedup-by-content-hash is identity, not bypassed
  validation. Docs over-read class.
- **Unresolved (frozen):** FW3 outage-timer/frame-flaps, KL1
  pair-validator + archive-concurrency ×2.
- Advisory precision (new runs): 48 genuine / 59 instances (~81%;
  ~95% excluding low-value). PR25-rep1 silence vs sibling minors is
  repetition inconsistency, not error.

## 9. Shared detector contamination (phantom contracts)

Deterministic `verified_proof` phantoms persist MODEL-INDEPENDENTLY:
FW3/KL1 frozen classes unchanged. New runs add L06 (xcresult-path
"route", 3/3) and PR25/PR27 (`route|GET|/media/`, 3/3 each) — false by
construction (yt2wiki serves `@app.get('/media/{source_id}/{filename}')`
at head app.py:997). **The /media/ repeat TERRA_01 left untested is now
tested: the detector still emits the identical phantom on the
triggering pattern.** Containment improved elsewhere (21/36/36/9
detector claims correctly demoted to `observed_not_material`), but the
phantom leak into `verified_proof` is unpatched shared debt that
dominates finding counts and must be excluded from any precision math
for either model.

## 10. Head-to-head summary

1. Known-defect recall: Luna 6/10 issue-slots, Terra 4/10 (§5).
2. Distinct new genuine material: Terra 18 (+5 frozen = 23), Luna ~2
   confirmed + 1 plausible (+ references).
3. Distinct false/low-value: Terra 3 FP + 2 low-value new (+2 FP + 5
   unresolved frozen); Luna B ~0 FP (0/42 rejected_invalid).
4. Missed material incl. Luna-exclusives: §7 (5-6 items).
5. Completion: Terra 27/27 clean (0 finals); Luna B 21/21 (1 final).
   Terra's edge is exactly where it matters — L06/L10/P15/PR25/PR27
   all completed without budget finals.
6. Effort: Terra 23.2 tools / 5.7 reqs / ~344K cum. input tokens /
   ~36s / $0.128 per run; Luna B 22.4 / ~58s / $0.0137.
7. Cost: $3.3793 total (**~9.1×** per run); ~$0.15 per distinct genuine
   defect for Terra vs ~$0.07 for Luna.
8. Large-PR behavior: Terra's best showcase (L06: 12 genuine vs Luna
   ~0 adv; P15: 8 genuine vs 0 verified) — and PR25/27 show its floor
   (thin but correct: 2 minors + 1 silence + 1 FP).
9. Phantom contamination shared and unpatched (§9).
10. Terra advantages surviving adjudication: StoreKit/IAP race +
    staleness cluster (L06), readiness-gate hole cluster (P15),
    rate-limit bypass (L08), tier-gate laxity (L10) — all confirmed
    PR-introduced, all missed by Luna (0 advisories on L06 B in 2/3
    reps, 0 verified on P15).

## Decision: ESCALATION ONLY — do not promote, do not dismiss

- **Not PROMOTE:** reference recall still favors Luna, cost is 9.1×,
  and Terra's noise profile (docs over-reads, shared phantom load)
  disqualifies it as the default.
- **Not NO PROMOTION:** on the difficult large reviews where Luna is
  weakest, Terra's incremental value is decisive (L06 trim 3/3 vs 0/3
  with 3 further genuine; P15 8 genuine vs 0 verified; zero budget
  finals where Luna needed them). That is exactly the escalation
  use-case, and dismissing it would discard the campaign's main result.
- **Not INCONCLUSIVE:** the frozen roster completed with decisive data.
- **Standing bar kept:** the standard was not lowered for candidate
  volume — 3 FP + 2 low-value + 5 frozen unresolved are scored
  against Terra, phantoms excluded for both models.

**Recommendation: keep Luna B+FINAL as the default; route
Luna-finalized/inconclusive cases and L06/P15-class large PRs to Terra
as a targeted second reviewer; revisit default status only after the
phantom-contract detector fix and prompt calibration on
documented-design claims. No auto-promotion (no code changes made).**

## 11. Boundaries and artifacts

No Docker/watcher changes (watcher untouched, 170 repos stayed paused);
no SMTP/GitHub writes; no new models/prompts/variants; no cap changes
($3.3793 < $5 experiment ceiling; $30 provider headroom untouched);
no extra roster repetitions. Deliverables: this report +
`eval/TERRA_01R_EVIDENCE.json` (30 sanitized rows: 10 frozen + 20
adjudicated, run IDs digested, no secrets/transcripts). Raw new-run
artifacts checkpointed at `/data/coding/codatiel-i2/terra-r01/`
(outside git); operational ledger + seed/runner scripts alongside.
Residual risks: live-model drift since October; TERRA_01/FINAL_01 Luna
median+readme discrepancy (§5); Luna novel-finding text lost.
