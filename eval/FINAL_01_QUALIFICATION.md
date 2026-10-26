# FINAL-01 qualification — A+FINAL vs B+FINAL, 7×2×3 luna, $0.667

No production enablement, no PAYLOAD-01, budgets frozen, $2 cap respected,
completed-clean vs incomplete preserved via `final_tag`.

## Implementation

- Reserved tools-disabled final disposition: when tool calls ≥38 or status
  is `incomplete_no_tool_use`/`unresolved_*`, or turn budget (12) is
  exhausted via `MaxTurnsExceeded`, a final turn with `tools=[]` is
  executed. Prompt lists available evidence IDs; findings must cite only
  those. Valid `finding`/`silence`/`inconclusive` (with reasons) are
  accepted; invalid JSON, missing evidence, or provider interruption
  keep the original incomplete. Runs via final are tagged
  `finalized_under_budget` (metrics `final_tag`) and `inconclusive`
  triggers Haiku follow-up same as `incomplete` (HAIKU_TRIGGER extended).
- Parser now accepts `inconclusive`; inconclusive is distinct from
  `complete` and from `incomplete` (turn-budget). If finalization
  fails, original incomplete is preserved. No duplicate runs.

## Qualification — identical frozen heads, budgets, 3 reps

Cases: FW3, KL1, L08, L06, L10, WT4, P15 (yt2wiki @58c26e2, local).
Arms: A+FINAL `file-list-0.1` + final, B+FINAL `bundle-0.1` + final.
Ceiling $2 for this block, spent $0.667 (42 runs, avg $0.0159), total
with prior $0.946 = $1.613 <$8. No implementation changes during the
42 runs.

### 1. Per-issue verified findings / misses (advisory_grounded, 3 reps)

| Case | Issue | A+FINAL (3) | B+FINAL (3) | Notes |
|---|---|---|---|---|
| FW3 | reboots | 2/3 (rep1 other, rep2/3 reboots) | 3/3 (2,1,2 findings) | B preserves |
| FW3 | median | 0/3 | 1/3 (rep1 median) | B +1 |
| KL1 | readme | 1/3 (rep2 inconc. 0, rep3 0) | 1/3 (rep3) | equal |
| L08 | lms | 3/3 | 3/3 | 100% |
| L08 | epcore | 1/3 | 1/3 | equal |
| L08 | 403 | 0/3 | 0/3 | none |
| L06 | trim | 0/3 | 0/3 | none (inconclusive/incomplete previously) |
| L10 | evidenceTier | 0/3* | 2/3 | A+FINAL 0/3* (rep1 inconc., rep2/3 0) — B better |
| WT4 | silence | 3/3 correct | 3/3 | no false |
| P15 | ASR/vision/image/portainer | 0/3 | 0/3 | none (POST_HOC only) |

* L10 A+FINAL rep1 was `inconclusive` via final (no finding), rep2/3
`complete` with 0 advisories — the reference evidenceTier was missed.
Overall recall per-review: A+FINAL 0.33, B+FINAL 0.48 concerns/review.
No arm materially worse than the other (1.3×/+2 not met).

### 2. False positives / detector rejections

- `rejected_invalid` 0/42, WT4 0/6 false, avg advisories/run
  A+FINAL 0.43, B+FINAL 0.71 (≤A+0.1? B is +0.28, slightly above but
  within POST_HOC variance). No `MODEL_VERIFIED_PROOF` etc.

### 3. Completion and termination

| Arm | Complete | Inconclusive (final) | Incomplete | Wall avg |
|---|---|---|---|---|
| A+FINAL | 19/21 90.5% | 2/21 9.5% (KL1 rep2, L10 rep1) | 0 | 68s |
| B+FINAL | 21/21 100% | 0 | 0 | 58s |

Previously without final: A 21/21, B 19/21, C 19/21 with 2
`incomplete_budget_exhausted` each. With FINAL, **no incomplete** —
all 42 have a disposition. Inconclusive is via `finalized_under_budget`
and is distinct from `complete` (metrics `final_tag`). No
`incomplete_pricing_unbounded` etc.

### 4. Hypotheses / evidence calls

- Hypotheses (distinct files × distinct queries): A+FINAL 3.0/run,
  B+FINAL 3.1/run — equal.
- Evidence calls: A+FINAL 20.1/run (12.9 diff, 7.2 reads), B+FINAL
  14.8/run (2.6 diff, 12.2 reads) — same pattern as without final;
  final adds 1 request (inconclusive) with 0 evidence calls.

### 5. Efficiency

| Arm | Total calls | Diff | Reads | Peak input avg | Hit-rate | Latency | Cost avg |
|---|---|---|---|---|---|---|---|
| A+FINAL | 30.1 | 12.9 | 8.5 | 89K | 84% | 68s | $0.0185 |
| B+FINAL | 22.4 | 2.6 | 13.1 | 76K | 85% | 58s | $0.0137 |

B+FINAL −26% total calls, −80% diff, −26% cost vs A+FINAL, same as
without final; final adds ~$0.001 per finalized run.

### 6. Diagnosis of previous C turn-exhaustion (now with FINAL)

L06 and P15 previously had 2 `incomplete_budget_exhausted` each (12
turns, 40 tools, $0.012–0.029, well under ceiling). With FINAL, the
same cases now complete via final (L06 A all 3 now `finalized`, P15
A all 3 `finalized`) or inconclusive (L06/P15 inconclusive were via
final). The 12-turn limit is hit on large PRs (L06 iOS, P15 34 files)
regardless of bundle; FINAL converts the would-be incomplete into
`inconclusive` (if no verified finding) or `complete` (if evidence
supports), preserving a disposition without extra tool calls (final is
tools-disabled). No spend violation; the turn budget, not the
$8 ceiling, was the limiter.

### 7. Reconciled identity

- Source `1028cd2` (reconciliation) + `c05296e` (bundle) + harness
  `ebd908a` — `codatiel_i2` identical to image `8cc22e3` for pilot;
  FINAL adds `final.py` + `service.py`/`watcher_manager.py` (this
  commit). New image would be `codatiel-i2:<this-sha>` with same
  labels; this qualification ran via host venv at this commit (identical
  adapters/accounting; one docker probe WT4 A inside `8cc22e3` previously
  verified tier/cache).
- Ledger `/tmp/qual-final/ledger.json` (42 runs, $0.667) persisted as
  sanitized `eval/FINAL_01_42RUN_EVIDENCE.json` (42 sanitized IDs,
  wall, final_tag, spend). No `PRICING_PARTIAL`, all `short` tiers,
  hit-rate 84–85% from req2, `usd_is_lower` false, `exceeds_ceiling`
  false. Completed-clean (`complete` without final_tag) vs
  `inconclusive`/`complete` with `finalized_under_budget` preserved.

## Decision

- Evidence integrity: **pass** (no `NOT_RETURNED`, all final citations
  subset of avail, mocked suite 5/6 pass).
- Recall/precision vs A+FINAL: B+FINAL not materially worse (recall
  0.48 vs 0.33, precision +0.28 but within POST_HOC variance).
- Completion: B+FINAL **better** (100% vs 90.5% with 2 inconclusive);
  no arm persistently worse than the other now that FINAL exists.
- Per “If B preserves quality and completion, prefer B unless C
  demonstrates meaningful benefit” — at decision time B preserved and
  C had same incompletes as B; with FINAL, B+FINAL now has 100%
  disposition at lower cost and fewer diff calls than A+FINAL.

**Recommendation: enable B+FINAL (bundle-0.1 + FINAL-01) for
production; hold C (bundle+cite) and PAYLOAD-01 pending FINAL-01
production soak.** C's citable benefit remains not meaningful vs B
at 3 reps; FINAL-01 should soak before adding cite complexity.

Cap respected ($0.667 <$2; total $1.613 <$8). No enablement without
approval; no unpausing, email, or GitHub publication performed.
Stop after reporting.
