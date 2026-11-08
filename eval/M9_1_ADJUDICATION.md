# M9.1 Phase A1 — Independent Adjudication of M9 Findings

**Method:** each claim re-checked against the pinned work-tree source at the
recorded `run.json` `base_sha`/`head_sha`, plus the run's own `evidence.jsonl`
excerpts and `model_findings_raw.jsonl`. No model self-grading: classifications
below are `CONFIRMED` / `PARTIALLY_CORRECT` / `REFUTED`, with materiality,
actionability, and evidence quality stated separately. `ATTESTED` = evidence
authenticity only (replays, binds to SHA), never correctness.

## FW3 challenger — `reboots_sent` counts reservations, not dispatches

- **Claim:** `store.py` report increments `reboots_sent` for every row in
  `attempts`; rows are created by `reserve_attempt()` (INSERT) before
  `client.reboot()` is awaited (`service.py`: reserve ≈L365, reboot ≈L380).
  Crash/delivery-unknown window → report claims a send that never happened.
- **Source check:** `store.py:834-836` does `row["reboots_sent"] += 1`
  unconditionally per `attempts` row with `label = outcome or "UNRECORDED"`.
  `reserve_attempt()` INSERTs before reboot; `record_outcome()` is separate.
  Even without a crash, any `NULL`-outcome attempt is counted as sent.
- **Correctness: CONFIRMED.** Stronger than the stated crash window: the
  `UNRECORDED` path alone mislabels.
- **Materiality: moderate-high.** Effectiveness metric (`reboots_sent`) is
  unreliable; operators cannot distinguish sent vs reserved.
- **Actionability: yes.** Count only confirmed dispatches, or label
  reservations/`UNRECORDED` separately.
- **Evidence quality: good.** 4 `ev_*`, all bind to head `d9b61df6`, excerpts
  replay. Triggering condition concrete: reserve-then-crash, or any NULL outcome.

## FW3 challenger — even-size median

- **Claim:** `durations[len(durations)//2]` returns upper-middle for even n
  instead of the average of the two middle values (60s+120s → 120, not 90).
- **Source check:** `store.py:846` exactly as cited.
- **Correctness: CONFIRMED.** Direct, single-line.
- **Materiality: minor.** Overstates typical outage by up to half the gap.
- **Actionability: yes** (average middle pair).
- **Evidence quality: good** (2 `ev_*`, head-pinned).
- Note: direct defect, not cross-file; counts toward "previously missed" (M8
  confirmed-useful, missed by M9 baseline in the paired run) but not toward
  the nontrivial cross-file bar.

## FW3 baseline — outage attribution lost on restart

- **Claim:** `outage_started_utc` / `outage_rebooted` are runtime-only
  (`CameraRuntime`); a fresh `DecisionEngine` on restart loses them, so the
  next `frames_restored` reports post-restart duration and possibly
  `SELF_RECOVERED` despite a requested reboot.
- **Source check:** `policy.py:187-234` runtime construction; `:523-536`
  attribution logic as cited. Plausible and consistent with in-memory design.
- **Correctness: CONFIRMED (provisional).** No contradicting persistence found
  in the sampled span; full-store restore path not exhaustively audited.
- **Materiality: moderate.** Cross-restart effectiveness reporting.
- Both FW3 arms can be simultaneously true (different defects, same area).

## L08 challenger — 403 on `/cart?remove_item=&_wpnonce=`

- **Claim:** rule at `scs-edge-01.haproxy.cfg:129` returns 403 for WordPress
  hosts when path is `/cart` and query contains both `remove_item=` and
  `_wpnonce=`, matching ordinary WooCommerce cart-item removal; route log
  shows such requests previously reaching the app with 200.
- **Source check:** ACLs `:74-76` + 403 `:129` confirmed as cited. Comment
  claims "catches a direct fragment call forged with that same referrer", but
  the rule as written checks **no referrer/scanner discriminator** — unlike
  the adjacent fragment rule which requires `has_bogus_product_added_ref`.
  Overbreadth intent CONFIRMED.
- **Caveat (path exactness):** ACL is `path -i /cart` (exact); the cited 200
  log line shows path `.../cart/` (trailing slash), which exact-match would
  **not** hit. `/cart?remove_item=...` without slash does hit. Blast radius
  may be narrower than "all WooCommerce removals", but the rule is still a
  misconfigured WAF (blocks a legitimate parameter combination on one path
  form, with no scanner signature).
- **Correctness: PARTIALLY_CORRECT → CONFIRMED overbroad, with narrowed blast
  radius.** Downgraded from "blocks all cart removals" to "blocks a
  legitimate parameter combination with no scanner discriminator".
- **Materiality: high if hit** (customers cannot remove cart items), moderate
  overall given path-form caveat.
- **Actionability: yes** (narrow to scanner signature / referrer).
- **Evidence quality: good** (config + 200 log with same query pattern).

## L08 baseline (LMS `:443` vs `:9443`) vs challenger (Epcore `:9443` no vhost)

- **Baseline claim:** LMS backend `172.31.1.60:443` vs vhost
  `172.31.1.60:9443` (`lms-dev...conf:1`). **CONFIRMED**, material (traffic
  misses vhost).
- **Challenger's second finding** is **not** the same LMS finding (ledger delta
  "retains the LMS port finding" is **wrong**): it reports Epcore
  `172.31.1.60:9443` with SNI `epcore...` against an Effective Policy FPM
  vhost listening only on `127.0.0.1:8083` named `effectivepolicy.com.au`.
  No `epcore` vhost exists on `:9443` in the sampled tree → **CONFIRMED** as a
  distinct misroute (default-vhost/wrong-app risk).
- **Net for L08:** challenger lost baseline's LMS finding, gained two distinct
  findings (403 + Epcore). Still net incremental, but with a traded-away
  recall — the M9 "no loss" criterion fails here too, not just KL1/L06.

## KL1 baseline — D1 binding UUID not verified (challenger silence = real loss)

- **Claim:** validator checks `d1.database_id` format (UUID regex, `:92`) and
  `created_resources` type+name (`:119-132`), but never compares the resource
  entry's id to `d1.database_id`; bindings (`:144`) then use the supplied
  UUID while migration targets the run-prefixed name.
- **Source check:** confirmed as cited; no id-comparison anywhere in the
  validator span.
- **Correctness: CONFIRMED. Materiality: high** (disposable-isolation
  boundary; probe traffic can point at a different account DB).
- **Actionability: yes** (compare ids).
- Challenger silence on KL1 is an unexplained loss of confirmed material.

## L06 baseline — trimmed catalogue IDs miss StoreKit join (challenger silence = real loss)

- **Claim:** `productIDs` trims whitespace for the StoreKit request
  (`SubscriptionPlanCatalogueEntry.swift:32`), but `SubscriptionPlanJoiner`
  looks up with original untrimmed `monthly`/`yearly` values
  (`SubscriptionPlanOption.swift:58-65`). Whitespace IDs → request succeeds,
  join silently leaves cadence unavailable, plan hidden.
- **Source check:** confirmed as cited.
- **Correctness: CONFIRMED. Materiality: low-moderate** (requires backend to
  emit whitespace IDs; defensive contract mismatch, silently hides plans).
- Challenger silence is a real (if lower-weight) loss.

## L10 — both arms weak, neither is confirmed material

- **Baseline arm** (`.integration` check `continue`s for `.pure`): source
  comment at `CaseLedger.swift:296-300` states this is **lax on purpose**,
  pinned by WIRE-01 rows. Flagging intended, documented behavior as a defect
  is a **false positive**. A second baseline repetition instead reported an
  unrelated STORE-07 coverage claim — single-run instability, not a stable
  defect.
- **Challenger arm** (non-optional `evidenceTier` breaks old-JSON decoding):
  technically true for synthesized `Codable` (no custom `init(from:)`, no
  `decodeIfPresent`; initializer default `.pure` does not apply to decoding).
  **Correctness: CONFIRMED narrowly; materiality: low** (version-migration
  nit, no consumer-breakage proof). Not a behavioral regression with a
  triggering condition.
- **Verdict:** L10 contributes no confirmed-useful material to either arm.
  Ledger's "both 1, no clear win" is accurate but generous — both should be
  scored as non-material for Phase B purposes.

## Summary table

| Finding | Correctness | Materiality | Evidence |
|---|---|---|---|
| FW3 reboots_sent (chall) | CONFIRMED | moderate-high | good, head-pinned |
| FW3 median (chall) | CONFIRMED | minor | good |
| FW3 restart attribution (base) | CONFIRMED (provisional) | moderate | good |
| L08 403 (chall) | PARTIALLY_CORRECT (overbroad confirmed, path caveat) | moderate-high | good |
| L08 LMS :443 (base) | CONFIRMED | moderate-high | good |
| L08 Epcore :9443 (chall) | CONFIRMED (distinct from LMS) | moderate-high | good |
| KL1 isolation (base) | CONFIRMED | high | good |
| L06 trim (base) | CONFIRMED | low-moderate | good |
| L10 tier (base) | REFUTED (intended) | none | n/a |
| L10 evidenceTier (chall) | CONFIRMED narrowly | low | moderate |
