# COST-01B — Eliminate Repeat and Superseded Review Spend — Report

**Branch:** `cost-01b-reuse-and-stale` on `mitchins/codatiel`  
**Baseline:** `main` at `612c7a6` (PLATFORM-01 Slice 1A)  
**Snapshot:** `semdup-08/devstate/events.sqlite` → `/tmp/cost01b-baseline/events.snapshot.20261115T083011Z.sqlite`  
`sha256 04d0b14df690e745daae91eb9eaf4d092fea4185010c15b76729a29acb6bb8cd`  
**Scripts:** `cost-01/audit.py` (27 LOC) + `cost-01/repeat.py` (27 LOC) — frozen, no inference  
**PR:** to be opened against `main` (one bounded PR)

## Gate A — Preserve and verify the baseline

Reproduced against a SQLite-consistent `.backup` copy (never the live file, WAL-safe):

```
audit.py  (14 Nov 9 reviews $2.6272 + superseded $2.0628 = $4.6900)
  id pr kind  total semdup sd% sd_wall tgt pairs reuse pub  mainIn cache%  sdIn sdCache%
  33 15 full  0.407 0.279 68% 498.9  24   43    0   12 330484 78% 251956 25%
  34 14 incre 0.153 0.044 29%  82.2   8   12    0    0 104741 53%  29459 31%
  35 15 incre 0.244 0.133 54% 236.4  12   18    0    8 212242 69% 144212 24%
  36 15 incre 0.396 0.316 80% 334.0  15   28    0    7 121968 60% 293617 15%
  ... pairs_reused = 0 on every run (confirmed)

repeat.py (40 semdup runs, 1,722 pairs)
  443 of 1,722 (26%) re-judged a target+counterpart content-hash pair already judged on same PR
  47 already-reported suggestions published again
  Pro-rata waste $2.7046 of $8.8064 semdup spend (since 2026-11-13)
  Orphan superseded runs: 4 (81b6c47..a39a7ba, ac22952..3974fc5, 98e3ee4..28a71b2, 98e3ee4..173c6c2) semdup $0.917 + $0.242 + $0.189 + $0.303 = $1.651 of $2.063 total superseded
```

**Discrepancy documented:** Superseded `detail` was `{}` and `finished_ts` NULL pre-migration; after `612c7a6` migration it is `finished_ts` set and `detail` now holds `provenance_version` etc. No historical stage costs fabricated.

**Paid-call sites identified (no production changes in Phase A):**

- `service.review_local` (Terra primary, `canonical-service-0.1`, `runtime_sdk.run_review`, bounded `max_turns`)
- `additive.run_additive` (Luna blind sweep, `sub_usd`, deterministic merge)
- `semdup_dev/oai_dupe.assess_pair` (Terra per pair, `TERRA_INSTRUCTIONS_ID`)
- `semdup_dev/oai_dupe.challenge_pair` / `editor.edit_pair` (Luna per Terra-eligible pair, `LUNA/EDITOR_INSTRUCTIONS_ID`)
- `SpendLedger` per model (`spend_semdup_terra.jsonl`, `spend_semdup_luna.jsonl`, `spend_ledger.jsonl`)

**Interruption/superseded availability:** `metrics.json` + `semdup.json` remain on the run dir; delivery row holds `cost_usd` (or `NULL` → unknown hold). Previously superseded deliveries lacked `run_dir` linkage — now retained.

## Gate B — Stop spending on stale reviews

Live-head checks at economically useful boundaries, no unsafe cancellation:

- **Before starting semantic duplication:** `stage._run` checks `is_stale()` (closure over `get_live_head(repo,pr) != head`). If stale, returns `status="stale_skipped"` with `spend_usd 0`, no inference, `stale_skipped` flag.
- **Before additional paid assessments:** `editorial_shadow.review_candidate_pairs` checks `is_stale()` before each pair's `_review_pair`; remaining pairs become `skipped: stale_superseded` (no inference, ledger preserved).
- **Before any subsequent paid stage:** `_maybe_semdup_stage` checks `is_stale()` before calling `run_stage`; `stale_skipped` with reason.

When stale:

- Stop further paid calls at next safe boundary
- Preserve and settle all costs already incurred (`SpendLedger.confirmed_usd()` + `store.settle`)
- Preserve unknown-cost holds
- Do not publish stale result (post-inference `owned` + `live_after` still guards; semdup `published` not merged if stale)
- Preserve generation-token `in_progress_run` atomic completion
- Latest head proceeds via existing `queued` drain

**Regressions (all mocked, no inference):**

- `test_stale_before_dup_no_inference` — A→B stale at C before dup: 0 calls, `stale_skipped`
- `test_stale_during_dup_no_further_pairs` — stale during dup: ≤1 call, remaining `stale_superseded`
- `test_partial_stale_accounted` — partial spend persisted in `semdup.json` + settled ledger
- `test_stale_cannot_overwrite_newer` — generation-token atomic seal (`claim_run` check)
- `test_next_valid_covers_required_range` — queued `A→C` (not `B→C`) preserved

## Gate C — Reuse semantic-duplication verdicts safely

New module `semdup_dev/cache.py` (`cost01b-v1`):

**Identity (hash of canonical JSON):**

- `target_sha256` (hash of unit text, not line), `counterpart_sha256`
- `target_file_sha256`, `counterpart_file_sha256` (surrounding context, file-level)
- `target_path`, `counterpart_path` (different files are distinct)
- `classification` (introduced_or_changed role)
- `terra_model`, `luna_model`
- `terra_instructions_id` (`cec20f6c368e`), `luna_instructions_id` (`5c6cf6fb1f53`), `stage_version` (`semdup-stage-0.3`)

Excluded: `base_rev`, `head_rev`, `start_line`. File hash captures surrounding context changes; moving a function to a different line keeps file hash stable only if surrounding file bytes unchanged — otherwise it correctly forces reassess (conservative).

**On hit:**

1. Verify current evidence matches (`validate_anchor` + text sha)
2. Rebind to current `start_line`/`end_line`/`sha256`
3. Validate suggestion still applicable (anchor exists and text matches)
4. Preserve publication dedup (existing `_dedup` + mirror collapse)
5. Skip paid call only when reuse conditions satisfied; misses follow existing path

Scoping: `state_dir/semdup/cache/<repo>__<pr>/<pr>.json` — per-repo/PR, no cross-PR collision. Budget-stop rows never cached (`is_budget_stop_row` + `_mislabeled_refusal`).

**Regressions:**

- Unchanged pair+context across commits → reused (0 new calls, `pairs_reused` ≥1, `pairs_cache_hits` ≥1)
- Same code moved to different line (different `start_line`, same sha/file hash) → same key, hit
- Same bodies, changed surrounding file (file sha differs) → different key, miss
- Either body changes (sha differs) → miss
- Prompt/rules/model changes (different `terra_model` or `terra_instructions_id`) → miss
- Cached source deleted (anchor `ok False`) → `rebind_row` returns `None`, reassess, no stale publication
- Different PR/repo identical identifiers → separate cache files, no hit
- Repeated successful incremental review → second delivery is `skipped:replay` (Store replay protection), not duplicate publication

## Gate D — Preserve cost provenance

No second ledger. Minimal backward-compatible change:

- `event_store.Store.delivery_update_decision(..., detail)` now merges JSON detail; new helper `delivery_update_detail`
- `event_pipeline._provenance_detail(repo,pr,base,head,result,kind,info,superseded)` builds provenance for **every** attempt (including `skipped:stale_superseded`):

```
repo, pr, base_sha, head_sha, kind, diff_sha, files,
run_dir, run_id, terra_findings, luna_added,
total_cost_usd, cost_unknown, terra_spend_usd, semdup_spend_usd, semdup_status,
pairs_examined, pairs_reused, pairs_cache_hits, pairs_total, semdup_wall_s,
superseded, provenance_version="cost01b-v1", finished_at
```

Stored in `deliveries.detail` (existing `TEXT` column, JSON merge). Historical rows without stage costs remain `null` (unavailable, not fabricated). `reviews` provenance remains via `run_dir` artifacts; `spend_on_day` still sums `reviews` + `stale_superseded` deliveries exactly once (no double-count).

Migration: Additive `ALTER`-style JSON merge, no schema version bump, no WAL break.

**Regression:** `test_provenance_superseded_traceable` — stale run's delivery detail contains `provenance_version`, `run_dir`, `total_cost_usd`, `superseded True`.

## Gate E — Prove the improvement (deterministic replay, mocked models)

Frozen workload: 40 semdup runs, 1,722 pairs, $8.81 semdup + $2.06 superseded = $10.87 billed.

| Metric | Before | After (counterfactual) |
|---|---|---|
| Previously repeated pairs | 443 (26%) | — |
| Pairs now eligible for safe reuse | — | 443 (file context unchanged in this window) |
| Pairs correctly rejected (context/body/model change) | — | 0 in this window (file-hash changes would miss) |
| Avoided paid assessment calls | — | **443** (Terra+Luna per pair) |
| Estimated historical cost avoidance | — | **$2.28–$2.70** (repeat.py $2.70; replay $2.28 pro-rata) |
| Stale superseded semdup spend avoided | $1.65 (orphan) | **$1.65** saved if stale check before duplication had fired |
| Counterfactual billed | $10.87 | $6.94 (reviews $6.53 + superseded $0.41) |
| Additional processing | — | file hash lookups + cache JSON I/O, negligible |
| Remaining duplicate suggestions | 47 republished | 47 still published (same output, cheaper); suppressing already-reported would make 0 |
| Correctness regressions | — | none |

For the **14 Nov sample day** ($4.69: reviews $2.63 + superseded $2.06, semdup $1.39, superseded semdup $1.65):

- Reuse would save ~$0.40 (39 of 44 repeated on #14@4eb8041, etc.)
- Stale check before semdup would save ~$1.65 (all 4 orphans)
- Combined ~$2.05 of $4.69 day (~44%), or ~49% of semdup spend.

**Distinction:**

- **Actual billed:** $10.87 (historical)
- **Historical counterfactual savings:** $3.93 ($2.28 cache + $1.65 stale)
- **Projected savings:** future workloads with ~26% repeat rate save ~26% semdup + ~80% superseded semdup; varies with push frequency.

No broad paid inference experiments were conducted; a small capped live experiment (e.g., 5–10 incremental PRs with the new cache) could validate semantic equivalence but is not needed to close the mission.

## Changed files and migration impact

```
codatiel_i2/semdup_dev/cache.py          (new, 278 LOC) — content-addressed cache, no DB migration
codatiel_i2/semdup_dev/stage.py          (+120, stale check + cache lookup/store, rebind)
codatiel_i2/semdup_dev/editorial_shadow.py (+33, per-pair is_stale gate)
codatiel_i2/event_pipeline.py            (+125, is_stale threading, provenance detail, stale before semdup)
codatiel_i2/event_store.py               (+63, detail merge in delivery_update_decision + helper)
tests/test_cost01b.py                    (new, 14 regressions, all mocked)
```

No new tables, no `platform` activation, no dashboard, no webhook, no `SCHEMA_VERSION` bump. Existing `semdup/continued` (per-revision) retained; new `semdup/cache` (per-PR) additive. All writes are WAL-safe, `tmp`+`replace` atomic.

## Test and review results

```
121 passed (cost01b 14 + semdup_stage 43 + event_pipeline 22 + prod02 16 + watcher_corrections 26)
  — all with scripted models, real git repos, real index builds, zero network
```

CI: local `pytest` passes; hosted CI and CodeRabbit to be run on PR.

## Known limitations

- File hash is whole-file sha; moving a unit within a file that also changes file length will change file hash and force reassess even if immediate surrounding window is unchanged (conservative; correct per spec but misses some moves).
- Superseded `finished_ts` backfill for pre-migration rows remains `NULL` in frozen snapshot; live DB now sets it via `delivery_update_decision`.
- Cache does not yet deduplicate already-reported findings into suppressed count; reused published verdicts are still published (same output, cheaper). Switching to suppression is a one-line policy change.
- No calendar monthly cap fix (BUDGET-01), no debounce (COST-01C), no dashboard (DASH-01) — explicitly deferred.

## Alpha deployment recommendation

- Target is host cron `ops/codatiel-dev-poll.sh` (not legacy Docker watcher)
- Back up `semdup-08/devstate/events.sqlite` + `operator.json` via `.backup main` + `tar`
- Deploy exact approved commit (no limit/config widening, no platform activation)
- Leave listener active; observe 24h or until ≥5 real PR events:
  - Reviews attempted/completed/superseded/skipped, pairs assessed vs reused (`pairs_cache_hits`), cost per completed review, superseded cost, publications/duplicates, ledger consistency, exceptions
- Preserve `events.sqlite` + `runs/` + `poll.log` on any accounting/publication/stale invariant failure; do not reset spend or replay PRs to manufacture activity.
