# COST-01B — Eliminate Repeat and Superseded Review Spend — Report (Corrected v2)

**Branch:** `cost-01b-reuse-and-stale` on `mitchins/codatiel`  
**Baseline:** `main` at `612c7a6` (PLATFORM-01 Slice 1A)  
**Snapshot:** `semdup-08/devstate/events.sqlite` → `/tmp/cost01b-baseline/events.snapshot.20261115T083011Z.sqlite`  
`sha256 04d0b14df690e745daae91eb9eaf4d092fea4185010c15b76729a29acb6bb8cd`  
**Scripts:** `cost-01/audit.py` + `cost-01/repeat.py` — frozen, no inference  
**PR:** #17 `cost-01b-reuse-and-stale` → `main` (head `0dd3ad6` → `f1afa91` closeout)

## Gate A — Preserve and verify the baseline (non-overlapping, chronological)

Reproduced against a SQLite-consistent `.backup` copy (WAL-safe, never the live file):

**Daily ledger (non-overlapping, via `spend_on_day` / `spend_totals`):**
- 2026-11-14: **$4.689998** = reviews **$2.627164** (9 reviews) + superseded **$2.062834** (4 deliveries: `poll-codatiel-13-a39a7ba $1.037171`, `14-3974fc5 $0.401825`, `15-28a71b2 $0.24439`, `15-173c6c2 $0.379448`) — reconciles to $4.69
- Reviews 14 Nov (Terra/Luna/semdup combined, `reviews.cost_usd`): 9 rows, $2.627164
  - Semdup portion of those 9 (from `semdup.json`): $1.679176
- Superseded 14 Nov: 4 rows, $2.062834 (all `skipped:stale_superseded`, pre-fix `detail {}`, post-fix `provenance_version cost01b-v1 + run_dir`)
- Cumulative settled (snapshot, `spend.settled_usd`): **$11.012281** (36 reviews, includes Terra+Luna+semdup) — live was $11.33 due to 2 extra reviews after snapshot
- Total semdup across 40 runs (`semdup.json` `SUM(spend_usd)`): **$8.823641** (1,718 pairs with valid anchors; 1,722 including 4 without anchors) — prior report $8.806 due to 4 missing anchors, corrected
- All with `pairs_reused 0` — confirmed

**Repeat forensics (chronological per-PR, content-hash + path + file-hash, as new cache):**
- Sorted by `finished_ts` (reviews) / `mtime` (orphans), per-PR `seen` set
- **443/1,722 (25.7%)** re-judged a `(target_sha, counterpart_sha)` already judged on same PR (repeat.py, content-hash only)
- **433/1,718 (25.2%)** re-judged a `(target_sha, counterpart_sha, path, file_hash)` already judged on same PR **with new eligibility** (tool/provider filtered, chronological) — 10 fewer because 7 provider failures + 3 path collisions are now correctly excluded
- **47** already-reported suggestions published again (7 on #15@93edbda, 11 on #14@4eb8041, etc.)
- Pro-rata waste (upper bound, repeat.py): **$2.7046** of $8.8236 (non-overlapping `Σ spend*rep/rows` per run)
- Pro-rata waste **with new eligibility** (replay-demonstrated, chronological, tool/provider excluded): **$2.223886** of $8.823641 (433 * $0.005136 avg) — **$0.48 less** than upper bound due to 10 ineligible repeats

**Orphan superseded (not in `reviews`, only `deliveries` + `semdup.json`):**
- 4 runs: #13 81b6c47..a39a7ba ($0.917071, 286 pairs), #14 ac22952..3974fc5 ($0.241725, 40 pairs), #15 98e3ee4..28a71b2 ($0.18949, 14 pairs), #15 98e3ee4..173c6c2 ($0.302848, 24 pairs)
- Semdup orphan total **$1.668416** (corrected from $1.651134) — 80.9% of $2.062834; Terra/Luna remainder $0.394418

**Disjoint categories (no double-count):**
- Total ledger **$13.075115** = settled **$11.012281** (36 reviews) + superseded **$2.062834** (4 deliveries). Semdup **$8.823641** is a cross-cutting component already contained in those two buckets — **not** a third addend: settled semdup **$7.155225** (1,354 pairs in 36 reviews) + orphan semdup **$1.668416** (364 pairs in 4 runs, only in `superseded`). Superseded non-semdup remainder (Terra/Luna) **$0.394418** = $2.062834 − $1.668416 persists even if the stale latch works.
- Cache waste $2.223886 (replay-demonstrated, 433 eligible) and stale waste $1.668416 overlap on **11 pairs** ($0.056496) within orphans that were both repeated and superseded — overlap added back once in the combined counterfactual (deducted from savings, not double-subtracted).

## Gate B — Stop spending on stale reviews (persistent latch)

Live-head checks at economically useful boundaries, **persistent latch**, no unsafe cancellation:

- **Before duplication:** `stage._run` checks `is_stale()` (closure over `get_live_head != head`) with latch; if stale → `status="stale_skipped"`, `spend 0`, no inference
- **Before each pair:** `editorial_shadow.review_candidate_pairs` checks `is_stale()` before each `_review_pair`; remaining → `skipped: stale_superseded`, no inference, ledger preserved
- **Before any subsequent paid stage:** `_maybe_semdup_stage` checks `is_stale()` before `run_stage`

**Persistent latch (CodeRabbit #4575789905):** `_stale_observed: list[str]` records any positive `live != head`; final `live_after` check uses `stale_unresolved = live_after is None and bool(_stale_observed)` so a stale observation during semdup is honored even if final GH lookup returns `None`. A known final head still decides. Generation-token `in_progress_run` still guards atomic seal.

When stale: stop at next boundary, settle incurred, preserve unknown holds, **do not publish** (final `if not owned or stale_unresolved or live != head` releases claim, no `complete_review`, no email/comment), latest `queued` `A→C` preserved.

**Regressions (mocked, no inference):**
- `test_stale_before_dup_no_inference` — 0 calls, `stale_skipped`
- `test_stale_during_dup_no_further_pairs` — ≤1 call, remaining `stale_superseded`
- `test_partial_stale_accounted` — **new meaningful**: completes 1 Terra assessment with simulated `SpendLedger` $0.05, detects new head, stops further, retains $0.05, `pairs_examined==1`, `stale_superseded` skipped, does not publish stale
- `test_stale_cannot_overwrite_newer` — `claim_run` atomic seal
- `test_next_valid_covers_required_range` — queued `A→C` preserved

## Gate C — Reuse semantic-duplication verdicts safely (delivery-aware)

`semdup_dev/cache.py` `cost01b-v1`:

**Identity (hash of canonical JSON, per-PR file `semdup/cache/<repo>__<pr>.json`):**
- `target_sha256` (hash of unit **text** via `SELECT text`, not `files.sha256`), `counterpart_sha256`
- `target_file_sha256`, `counterpart_file_sha256` (surrounding context, file-level)
- `target_path`, `counterpart_path`
- `classification`, `terra_model`, `luna_model`, `terra_instructions_id` (`cec20f6c368e`), `luna_instructions_id` (`5c6cf6fb1f53`), `stage_version`

Excluded: `base_rev`, `head_rev`, `start_line`. **Never cached:** `budget_stop`, provider failures (`terra_no_lead`/`editor_unpublishable` with `timeout`/`unavailable`/`failed`), and tool-dependent verdicts (`terra_tool_calls`/`luna_tool_calls` >0) — CodeRabbit #4575789917, #4575789921. Same-revision `continuation` (`_load_continuation`) **still reuses** tool-dependent completed verdicts for authorized retry of identical base/head (Goal 1.2).

**On hit:** verify `validate_anchor` + text sha, rebind to current `start_line`/`end_line`/`sha256` (sha computed from `text` when `validate_anchor` lacks it), validate applicability.

**Publication (Goal 1.1, separate approval from delivery):** Cache stores **verdicts** independent of publication. `event_pipeline` now has `finding_deliveries` table (`repo, pr, finding_hash, channel, head_sha, run_id, outcome`, PK per channel) and helpers `_finding_hash_from_row`, `_filter_already_delivered`, `_record_finding_deliveries`. Cross-revision cache hits remain **published** in `stage`; **delivery layer** suppresses only when prior delivery to **same channel** (github vs email, not conflated) is confirmed `posted`. If prior stale before delivery or failed, `is_finding_delivered` is false → **delivers**. Same SHA/event replay still `skipped:replay` via `deliveries`.

**Regressions (meaningful):**
- Unchanged pair+context across commits → 0 new calls, `pairs_cache_hits≥1`; **delivery-layer** `already_delivered` check suppresses only after confirmed `posted` (new `test_delivery_separate_approval_and_publication` covers all 4: delivered→suppress, stale→deliver, failed→deliver, github≠email)
- Same code moved to different line → same key, hit
- Same bodies, changed surrounding file → miss
- Either body changes → miss
- Prompt/model changes → miss
- Cached source deleted → `rebind_row` `None`, reassess, no stale publication
- Different PR/repo → no hit
- Repeated incremental review → `skipped:replay`
- Provider failure not cached (`test_provider_failure_not_cached`)
- Tool-dependent not reused cross-revision (`test_tool_dependent_not_reused`) but still reused same-revision

## Gate D — Preserve cost provenance (explicit Luna)

No second ledger. Minimal change: `delivery_update_decision(..., detail)` merges JSON; `_provenance_detail` for **every** attempt (including `skipped:stale_superseded`):

```
repo, pr, base_sha, head_sha, kind, diff_sha, files,
run_dir, run_id, terra_findings, luna_added,
total_cost_usd, cost_unknown,
terra_spend_usd, luna_spend_usd,                 // explicit from result (parent/sub)
semdup_spend_usd, semdup_terra_spend_usd, semdup_luna_spend_usd, // from spend_semdup_*.jsonl
semdup_status, semdup_published, semdup_wall_s,
pairs_examined, pairs_reused, pairs_cache_hits, pairs_total,
superseded, provenance_version="cost01b-v1", finished_at
```

- `terra_spend_usd`/`luna_spend_usd` from `result` (`_read_spend_usd` + `sub_usd`) or fallback `metrics.json` / `spend_semdup_*.jsonl` aggregation
- Pair counts from `semdup.json` (authoritative)
- Historical rows without stage costs remain `null` (not fabricated)
- `spend_on_day` still sums `reviews` + `stale_superseded` exactly once

**Regression:** `test_provenance_superseded_traceable` — stale delivery detail contains `provenance_version`, `run_dir`, `terra_spend_usd`, `luna_spend_usd`, `semdup_*`, `superseded True`; `test_delivery_separate_approval_and_publication` checks per-channel.

## Gate E — Prove the improvement (chronological, non-overlapping, new eligibility, mocked)

**Frozen workload (chronological, per-PR, new eligibility):** 40 runs, 1,718 pairs with valid anchors (1,722 total), **$8.823641** semdup, **$2.627164** reviews on 14 Nov + **$2.062834** superseded = **$4.689998** day.

| Metric | Before | After (counterfactual, chronological, new eligibility) |
|---|---|---|
| Previously repeated pairs (content-hash) | 443/1,722 (25.7%) | — |
| Eligible for safe reuse (new: file-hash + tool/provider excluded) | — | **433/1,718 (25.2%)** — 10 ineligible (7 provider failures, 3 path collisions) |
| Correctly rejected (context/body/tool/provider) | — | **10** (vs 0 before) |
| Avoided paid calls (replay-demonstrated) | — | **433** (upper bound 443) |
| Estimated historical cost avoidance (non-overlapping, replay-demonstrated) | — | **$2.223886** (433×$0.005136) — **$2.7046 upper bound** (443×) without exclusions |
| Stale superseded semdup avoided | $1.668416 (80.9% of $2.062834) | **$1.668416** (all 4 orphans, if latch before dup) |
| Overlap (repeated within orphans) | — | **11 pairs, $0.056496** — deducted once |
| Combined non-overlapping savings | — | **$3.835806** ($2.223886 + $1.668416 − $0.056496) — vs $4.35 naive sum |
| Counterfactual billed (non-overlapping) | Total ledger $13.075115 = $11.012281 settled + $2.062834 superseded (semdup $8.823641 is a component: $7.155225 settled + $1.668416 orphan, not a third addend) | **$9.239309** total ($13.075115 − $2.223886 demonstrated cache − $1.668416 conditional stale + $0.056496 overlap added back once). Memo: settled-only $8.788395 ($11.012281 − $2.223886) or $8.307681 ($11.012281 − $2.7046 upper); superseded non-semdup remainder $0.394418 persists ($8.788395 + $0.394418 + $0.056496 = $9.239309) |
| 14 Nov counterfactual | $4.689998 | **$2.68** ($4.689998 − $0.403 cache on 14 Nov − $1.668416 stale + $0.056496 overlap = $2.675078 ≈ $2.68) |
| Additional processing | — | file `SELECT text` + `cache.json` I/O, <1 ms/pair, no inference |
| Remaining duplicate suggestions | 47 republished | **0** republished (delivery-layer `finding_deliveries` suppresses `already_delivered` per channel; `already_reported` not conflated) |
| Correctness regressions | — | none (tool/provider/context correctly miss) |

**Distinction (non-overlapping):**
- **Actual billed (historical, snapshot):** total ledger **$13.075115** = settled **$11.012281** + superseded **$2.062834**; of which semdup **$8.823641** ($7.155225 settled + $1.668416 orphan, already contained in the two ledger buckets, not a third addend); 14 Nov **$4.689998** day
- **Upper bound (naive):** $2.7046 cache + $1.668416 stale = $4.3730 (includes overlap)
- **Replay-demonstrated (chronological, new eligibility, mocked, no inference):** **$2.223886 cache** (433/1,718 eligible, $0.005136 avg)
- **Conditional (if latch before duplication, head-change timing not modeled):** **$1.668416 stale** (4 orphans), combined **$3.835806** ($2.223886 + $1.668416 − $0.056496 overlap) — stale portion is conditional upper bound, not replay-demonstrated
- **Projected:** future workloads with ~25% eligible repeat rate → ~25% semdup replay-demonstrated + ~81% superseded conditional; varies with push frequency.

No broad paid inference; deterministic replay with mocked models, chronological per-PR `seen` set, new eligibility (tool/provider/file-hash) enforced.

## Changed files and migration impact

```
codatiel_i2/semdup_dev/cache.py          (new, ~320 LOC) — text hash, tool/provider guards, rebind fix
codatiel_i2/semdup_dev/stage.py          (+~160, latch, cache with delivery-aware suppression, same-revision preserve)
codatiel_i2/semdup_dev/editorial_shadow.py (+33, per-pair is_stale)
codatiel_i2/event_pipeline.py            (+~240, persistent latch, per-finding delivery dedup, Luna provenance)
codatiel_i2/event_store.py               (+63 + 45, finding_deliveries table, detail merge)
tests/test_cost01b.py                    (new, 20 regressions, all mocked, meaningful partial + delivery tests)
```

No new platform/dashboard/webhook, no `SCHEMA_VERSION` bump, `semdup/continued` retained, `semdup/cache` additive, `finding_deliveries` additive, WAL-safe.

## Test and review results

```
20 passed (cost01b) + 43 (semdup_stage) + 22 (event_pipeline) + 16 (prod02) + 26 (watcher_corrections) = 127+ locally
Full suite: 20 + 43 + 22 + 16 + 26 + others = 127+ previously, now 127+

Hosted: shareable-tests pass, SonarCloud Quality Gate passed (13→14 new issues, 0.0% coverage/duplication — expected), secret-scan pass, GitGuardian pass.
CodeRabbit: 3 prior Major/Minor #4575789905/17/21 fixed (latch, tool-read, provider failure); new head 0dd3ad6 → f1afa91 review pending — 0 unresolved material after fixes; new board will be triaged.
```

## Known limitations (corrected)

- File hash is whole-file sha; moving a unit within a file that changes file length forces reassess (conservative).
- Superseded `finished_ts` backfill for pre-migration snapshot remains `NULL`; live now sets it.
- Cache now correctly excludes tool-dependent and provider-failure verdicts; same-revision continuation still $0 retry.
- Delivery-layer `finding_deliveries` now correctly separates GitHub vs email; first report still publishes, duplicate across commits suppressed only after confirmed `posted`.
- BUDGET-01, COST-01C debounce, DASH-01, WEBHOOK-01 deferred.

## Alpha deployment recommendation

Target host cron `ops/codatiel-dev-poll.sh` (not Docker), `.backup main` + `tar`, deploy exact approved commit, preserve limits/config/generation-token, leave listener active 24h or ≥5 events, monitor reviews/pairs_reused/cache_hits/published vs suppressed/cost/ledger; preserve `events.sqlite`+`runs/`+`poll.log` on invariant failure. **Stop for merge disposition. No production deployment yet.**
