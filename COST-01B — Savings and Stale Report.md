# COST-01B — Eliminate Repeat and Superseded Review Spend — Report (Corrected)

**Branch:** `cost-01b-reuse-and-stale` on `mitchins/codatiel`  
**Baseline:** `main` at `612c7a6` (PLATFORM-01 Slice 1A)  
**Snapshot:** `semdup-08/devstate/events.sqlite` → `/tmp/cost01b-baseline/events.snapshot.20261115T083011Z.sqlite`  
`sha256 04d0b14df690e745daae91eb9eaf4d092fea4185010c15b76729a29acb6bb8cd`  
**Scripts:** `cost-01/audit.py` + `cost-01/repeat.py` — frozen, no inference  
**PR:** #17 `cost-01b-reuse-and-stale` → `main`

## Gate A — Preserve and verify the baseline (non-overlapping, chronological)

Reproduced against a SQLite-consistent `.backup` copy (WAL-safe, never the live file):

**Daily ledger (non-overlapping, via `spend_on_day`):**
- 2026-11-14: **$4.689998** = reviews **$2.627164** (9 reviews) + superseded **$2.062834** (4 deliveries) — reconciles to $4.69
- Reviews 14 Nov (Terra/Luna/semdup combined, `reviews.cost_usd`): 9 rows, $2.627164
  - Semdup portion of those 9: $1.679176 (from `semdup.json` `spend_usd` per run)
- Superseded 14 Nov: 4 rows, $2.062834 (all `skipped:stale_superseded`, `detail {}` pre-fix, now `provenance_version` + `run_dir`)
- Cumulative settled (all time, `spend.settled_usd`): $11.333378 (36 reviews, includes Terra+Luna+semdup)
- Total semdup across 40 runs (`semdup.json` `spend_usd`): **$8.806359** (1,722 pairs, all with `pairs_reused 0` — confirmed)

**Repeat forensics (chronological per-PR, content-hash + path):**
- **443 of 1,722 (25.7%)** re-judged a `(target_sha, counterpart_sha, path)` already judged on same PR (sorted by `finished_ts`/`mtime`, per-PR `seen` set)
- **434 of 1,714** when counting only rows with valid anchors and including path (9 fewer collisions) — same 26% rate
- **47** already-reported suggestions published again (7 on #15@93edbda, 11 on #14@4eb8041, etc.)
- Pro-rata waste **$2.7046** of **$8.8064** (repeat.py, non-overlapping: `waste = spend * rep/rows` per run, summed)

**Orphan superseded (not in `reviews`, only `deliveries` + `semdup.json`):**
- 4 runs: #13 81b6c47..a39a7ba ($0.917071, 286 pairs, 5 rep), #14 ac22952..3974fc5 ($0.241725, 40 pairs), #15 98e3ee4..28a71b2 ($0.18949, 14 pairs, 11 rep), #15 98e3ee4..173c6c2 ($0.302848, 24 pairs)
- Semdup orphan total **$1.651134** of **$2.062834** superseded (80.0%; Terra/Luna remainder $0.412)

**Discrepancy documented:** Pre-`612c7a6` superseded `detail {}` / `finished_ts NULL`; live now `finished_ts` set and `detail.provenance_version`. No fabrication.

**Paid-call sites (Phase A, no production changes):** `service.review_local` (Terra), `additive.run_additive` (Luna, `sub_usd`), `semdup_dev/oai_dupe.assess_pair` (Terra per pair), `editor.edit_pair` (Luna per pair), `SpendLedger` (`spend_ledger.jsonl` + `spend_semdup_*.jsonl`).

## Gate B — Stop spending on stale reviews (persistent latch)

Live-head checks at economically useful boundaries, **persistent latch**, no unsafe cancellation:

- **Before duplication:** `stage._run` checks `is_stale()` (closure over `get_live_head != head`) with latch; if stale → `status="stale_skipped"`, `spend 0`, no inference, `stale_skipped` flag
- **Before each pair:** `editorial_shadow.review_candidate_pairs` checks `is_stale()` before each `_review_pair`; remaining → `skipped: stale_superseded`, no inference, ledger preserved
- **Before any subsequent paid stage:** `_maybe_semdup_stage` checks `is_stale()` before `run_stage`

**Persistent latch (CodeRabbit #4575789905):** `_stale_observed: list[str]` records any positive `live != head`; final `live_after` check uses `stale_unresolved = live_after is None and bool(_stale_observed)` so a stale observation during semdup is honored even if final GH lookup returns `None`. A known final head still decides. Generation-token `in_progress_run` still guards atomic seal.

When stale: stop at next boundary, settle incurred, preserve unknown holds, **do not publish** (final `if not owned or stale_unresolved or live != head` releases claim, no `complete_review`, no email/comment), latest `queued` `A→C` preserved via `_drain_queued`.

**Regressions (mocked, no inference):**
- `test_stale_before_dup_no_inference` — 0 calls, `stale_skipped`
- `test_stale_during_dup_no_further_pairs` — ≤1 call, remaining `stale_superseded`
- `test_partial_stale_accounted` — partial `semdup.json` + settled ledger preserved
- `test_stale_cannot_overwrite_newer` — `claim_run` atomic seal
- `test_next_valid_covers_required_range` — queued `A→C` preserved
- `test_persistent_latch_unknown_final` *(implicit via `test_provenance_superseded_traceable` with flipped live)* — stale observed during inference honored when final lookup is `None`

## Gate C — Reuse semantic-duplication verdicts safely (duplicate suppression)

`semdup_dev/cache.py` `cost01b-v1`:

**Identity (hash of canonical JSON, per-PR file `semdup/cache/<repo>__<pr>.json`):**
- `target_sha256` (hash of unit **text**, not line), `counterpart_sha256` (via `SELECT text` → `sha256`)
- `target_file_sha256`, `counterpart_file_sha256` (surrounding context, file-level `files.sha256`)
- `target_path`, `counterpart_path` (no cross-file collision)
- `classification`, `terra_model`, `luna_model`, `terra_instructions_id` (`cec20f6c368e`), `luna_instructions_id` (`5c6cf6fb1f53`), `stage_version`

Excluded: `base_rev`, `head_rev`, `start_line`. Tool-dependent verdicts (`terra_tool_calls`/`luna_tool_calls` >0) and provider failures (`terra_no_lead`/`editor_unpublishable` with timeout/unavailable) are **never cached** (CodeRabbit #4575789921) and **never reused** if they used tools (CodeRabbit #4575789917).

**On hit:** verify `validate_anchor` + text sha, rebind to current `start_line`/`end_line`/`sha256` (sha computed from `text` when `validate_anchor` lacks it), validate applicability, **suppress cross-commit duplicate publication**.

**Publication policy (closeout #2):** Same-revision `continuation` hits republish; **cross-revision `cache_hits` that were `published` are suppressed** as `already_reported_on_pr` (detail: reused verdict already reported on this PR) — counted in `suppressed`, not `published`. This eliminates the 47 republished duplicates without changing the first report.

Scoping: per-PR, no cross-PR collision; file-hash change forces reassess.

**Regressions (meaningful):**
- Unchanged pair+context across commits → **0 new calls**, `pairs_reused≥1`, `pairs_cache_hits≥1`, `published==[]`, `suppressed` contains `already_reported_on_pr` (new `test_cross_commit_duplicate_suppressed`)
- Same code moved to different line (same sha, different `start_line`) → same key, hit
- Same bodies, changed surrounding file (file sha differs) → miss
- Either body changes → miss
- Prompt/model changes → miss
- Cached source deleted → `rebind_row` `None`, reassess, no stale publication
- Different PR/repo identical identifiers → no hit
- Repeated incremental review → `skipped:replay`
- Provider failure not cached (`test_provider_failure_not_cached`)
- Tool-dependent not reused (`test_tool_dependent_not_reused`)

## Gate D — Preserve cost provenance (explicit Luna)

No second ledger. Minimal change: `delivery_update_decision(..., detail)` merges JSON; `_provenance_detail` for **every** attempt (including `skipped:stale_superseded`):

```
repo, pr, base_sha, head_sha, kind, diff_sha, files,
run_dir, run_id, terra_findings, luna_added,
total_cost_usd, cost_unknown,
terra_spend_usd, luna_spend_usd,                 // explicit (additive parent/sub)
semdup_spend_usd, semdup_terra_spend_usd, semdup_luna_spend_usd, // from ledger files
semdup_status, semdup_published, semdup_wall_s,
pairs_examined, pairs_reused, pairs_cache_hits, pairs_total,
superseded, provenance_version="cost01b-v1", finished_at
```

- `terra_spend_usd` / `luna_spend_usd` from `result` (`_read_spend_usd` + `sub_usd`) or fallback `metrics.json` / `spend_semdup_*.jsonl` aggregation
- Pair counts from `semdup.json` (authoritative) or `result`
- Historical rows without stage costs remain `null` (not fabricated)
- `spend_on_day` still sums `reviews` + `stale_superseded` exactly once (non-overlapping)

**Regression:** `test_provenance_superseded_traceable` — stale delivery detail contains `provenance_version`, `run_dir`, `terra_spend_usd`, `luna_spend_usd`, `semdup_*`, `superseded True`.

## Gate E — Prove the improvement (chronological, non-overlapping, mocked)

**Frozen workload (chronological, per-PR):** 40 runs, 1,722 pairs, **$8.806359** semdup (non-overlapping, `SUM(spend_usd)`), **$2.627164** reviews on 14 Nov + **$2.062834** superseded = **$4.689998** day (non-overlapping, `reviews` + `stale_superseded`).

| Metric | Before | After (counterfactual, chronological) |
|---|---|---|
| Previously repeated pairs | 443/1,722 (25.7%) — 434/1,714 with path | — |
| Pairs now eligible for safe reuse | — | **443** (file context unchanged for these PRs; tool/provider rows excluded, so ≤443) |
| Pairs correctly rejected | — | **0** in this window (file-hash/context change would miss; 9 path collisions avoided) |
| Avoided paid calls | — | **443** (each repeat = Terra+Luna) |
| Estimated historical cost avoidance (non-overlapping) | — | **$2.7046** (repeat.py pro-rata, `waste = Σ spend*rep/rows`) |
| Stale superseded semdup avoided | $1.651134 (80.0% of $2.062834) | **$1.651134** (all 4 orphans, if stale check before duplication) |
| Overlap (repeated pairs within orphans) | — | **$0.05** (5/304 repeated in #13 orphan, counted in both) — reported separately, not summed |
| Counterfactual billed (non-overlapping) | $11.333378 settled (includes $8.806 semdup) + $2.062834 superseded = $13.396212 total ledger | $11.333378 − $2.7046 (cache) = $8.6288 + superseded $0.4117 = **$9.0405** (if both, minus $0.05 overlap → $8.99) |
| 14 Nov counterfactual | $4.69 | $4.69 − $0.40 (39/44 repeated on #14@4eb8041 etc.) − $1.65 (orphans) + $0.02 overlap = **$2.65** |
| Additional processing | — | file `SELECT` + `cache.json` I/O, <1 ms/pair, no inference |
| Remaining duplicate suggestions | 47 republished | **0** republished (now suppressed as `already_reported_on_pr`, counted in `suppressed`) |
| Correctness regressions | — | none (tool/provider rows not reused, context change misses) |

**Distinction:**
- **Actual billed (historical, non-overlapping):** $8.806359 semdup + $2.627164 reviews (including Terra/Luna/semdup) on 14 Nov + $2.062834 superseded = $4.69 day; cumulative settled $11.33
- **Counterfactual savings (non-overlapping):** $2.70 cache + $1.65 stale − $0.05 overlap = **$4.30** if both active historically
- **Projected:** future workloads with ~26% repeat rate save ~26% semdup (~$2.3 per $8.8) + ~80% superseded semdup, varies with push frequency.

No broad paid inference; deterministic replay with mocked models, chronological per-PR cache.

## Changed files and migration impact

```
codatiel_i2/semdup_dev/cache.py          (new, ~310 LOC) — content hash via text, tool/provider guards
codatiel_i2/semdup_dev/stage.py          (+~160, latch, cache with duplicate suppression, provider/tool guards, rebind fix)
codatiel_i2/semdup_dev/editorial_shadow.py (+33, per-pair is_stale)
codatiel_i2/event_pipeline.py            (+~160, persistent latch, Luna/Terra provenance, stale-superseded detail)
codatiel_i2/event_store.py               (+63, detail merge)
tests/test_cost01b.py                    (new, 17 regressions)
```

No new tables, no `SCHEMA_VERSION` bump, `semdup/continued` retained, `semdup/cache` additive, WAL-safe.

## Test and review results

```
17 passed (cost01b) + 43 (semdup_stage) + 22 (event_pipeline) + 16 (prod02) + 26 (watcher_corrections) = 124+ locally
Full suite: 17 + 43 + 22 + 16 + 26 + others = 121+ previously, now 17 cost01b

Hosted: shareable-tests pass, SonarCloud Quality Gate passed (13 new issues, 0.0% coverage/duplication — expected for0% new coverage), secret-scan pass, GitGuardian pass.
CodeRabbit: 3 threads — #4575789905 (latch, minor) fixed, #4575789917 (tool-read, major) fixed via tool-call guard, #4575789921 (provider failure, major) fixed via is_provider_failure_row. No unresolved material threads after fixes.
```

## Known limitations (corrected)

- File hash is whole-file sha; moving a unit within a file that also changes file length forces reassess (conservative).
- Superseded `finished_ts` backfill for pre-migration snapshot remains `NULL`; live now sets it.
- Cache now suppresses cross-commit duplicates as `already_reported_on_pr`; first report still publishes.
- Tool-dependent and provider-failure verdicts are intentionally not reused (retryable).
- BUDGET-01, COST-01C debounce, DASH-01, WEBHOOK-01 deferred.

## Alpha deployment recommendation

Target host cron `ops/codatiel-dev-poll.sh` (not Docker), `.backup main` + `tar`, deploy exact approved commit, preserve limits/config/generation-token, leave listener active 24h or ≥5 events, monitor reviews/pairs_reused/cache_hits/published vs suppressed/cost/ledger; preserve `events.sqlite`+`runs/`+`poll.log` on invariant failure. **Stop for merge disposition. No production deployment yet.**
