# M9.2 Freeze & Pre-registration (P3 — before any M9.2 inference)

## Frozen arms

| Arm | Instructions | `instructions_id` | Inventory | `inventory_strategy` (code hash) |
|---|---|---|---|---|
| A | current minimal prompt, byte-identical | `canonical-minimal-0.1@a93e5e56fa9a` | current `_render_inventory` | `file-list-0.1` (`615ef8bf9a29022a`) |
| B | ported guidebook (`guidebook_port.md`, verbatim) + minimal output schema | `guidebook-port-0.1@32dd8f811e95` | current `_render_inventory` | `file-list-0.1` (`615ef8bf9a29022a`) |
| C | ported guidebook (same bytes as B) | `guidebook-port-0.1@32dd8f811e95` | triage inventory | `triage-0.1` (`cdde181ceb30c388`) |

- `instructions_id`s are content-derived (`<name>@<sha256[:12]>` of the exact submitted bytes); inventory hashes are sha256[:16] of the rendering function's source.
- After this commit, the only permitted change is a mechanical defect fix (parse/transport), applied identically to all arms and logged. Any guidance-text change voids the phase.

## Development cases (P4) — SHA-verified, corrected from M9.1

| Case | Repo / PR | Base → Head (full SHA) | Role |
|---|---|---|---|
| FW3 | frigate-watchdog#3 | `956f7445fa13e93309dc5809395cd316ae4d7403` → `d9b61df6c889a28b0cb49547979897533b873ba4` | material (median, reboots, restart) |
| KL1 | kinglet-lms#1 | `dbf06e0004a44570dc253e88fffd7433d95997c8` → `694269d572bd77791f50dfd61d2ec52323da9c47` | material (isolation) + large + distractors |
| L08 | scs-infra#4 | `aa807b86fda364ef0723b536672e07cca4646c6e` → `aff2a0680940964ce0d7304a2699f0415893a4b4` | material (lms, epcore, 403) |
| L06 | my-day-my-way-ios#11 | `6509400ca318ba90d868e3cfa7fe7a5f2571bb86` → `4051ff05090a76165f1a533675086e5760b97cbd` | material (trim; roster head corrected) |
| L10 | needletail-harness#4 | `08847b4cd2691c127597218dfd31457bf246145f` → `80bf493d1cae6d9d40fe70894ee294be80292af2` | intended-behaviour trap + low-material control |
| WT4 | wall-tidbits#4 | `9333622385f7139ae2eddeb4a1efaec42c59f1aa` → `f053c2f89fd1f1acd509b0962e83ab211cbb4f71` | clean negative control |

Any case that cannot be SHA-verified at run time is dropped and the drop recorded. SHA gate enforced (M9.1).

## Run plan (paid)

- **Luna:** A, B, C × 6 cases × 3 reps = 54 runs. Order case-major, arms interleaved (A/B/C rotation, rotating start per case).
- **Haiku:** A, B × 6 cases × 3 reps = 36 runs. C on Haiku only if budget remains after Luna completes.
- Identical `max_turns=12`, `tool_budget=40`, model settings, revisions, SHA gate for every arm. No retries (one infra-only retry before the first model response, logged).
- Reserve rule: keep ≥ 1 maximum-observed run cost unspent before starting any run; re-project after the first case of each phase; stop and report if the projection exceeds the phase budget.
- Haiku invalid finals (`INCOMPLETE_INVALID_FINAL`: empty/unparseable) are never silence and never a correct negative; report rate per arm.

## Measures (per arm × model)

Material hit rate (distinct reference-set defects found per run + newly confirmed, adjudicated); consistency (reps/3 per defect); losses (found by A in ≥2/3 but arm in 0/3); false accusations per run (L10 trap separate); efficiency (tool calls, turns, $ per material hit; incompletes); taxonomy shift (P2 classes recomputed).

**An arm improves on A** for a model iff ALL hold: (1) hit rate ≥ 1.3× A's and ≥ +2 defect-reps absolute; (2) false accusations/run ≤ A's + 0.1; (3) no loss of KL1 isolation or any high-materiality item; (4) incomplete rate not worse by > 1 run in 18. Meets 1 but not 2–4 → `MIXED`; else `NO_IMPROVEMENT`. No p-values; per-case tables.

## Adjudication (blinded)

Strip arm/model labels → opaque IDs; every verdict carries base/head file:line proof; reference matches by mechanism; new findings get CONFIRMED/PARTIAL/REFUTED/UNVERIFIABLE + materiality. Commit record + sealed blind→arm map. Agent-performed adjudication is labelled development evidence; the owner spot-checks ≥ 5 verdicts before §7.

## Qualification (P5, only if an arm improves)

Winner vs A on F1–F6 (frozen, diffs unopened until §7.1; pre-run reference list committed first; post-hoc finds tagged `POST_HOC`). Verdicts: `METHODOLOGY_IMPROVES` / `METHODOLOGY_MIXED` / `NO_IMPROVEMENT` / `BLOCKED`.

## Budgets (hard ceiling $10.00)

P4 development ≤ $6.50; P5 qualification ≤ $3.50. Re-project after the first case of each phase.

## Escalation criterion for costlier models (recorded, not acted on)

> A costlier-model experiment is justified only if, for the best Luna/Haiku arm, `EXAMINED_NOT_CONNECTED` + `INTENDED_BEHAVIOUR_MISREAD` + `FOUND_WRONG` together exceed `NEVER_EXAMINED` + `BUDGET_EXHAUSTED_BEFORE` across material reference items, on both models.
>
> That is: the model reads the decisive code and still reasons wrongly, more often than it fails to reach the code at all. If attention failures dominate, the next investment is triage/inventory, not intelligence.

Retrospective P2 inputs: Luna 23 vs 19 (reasoning exceeds); Haiku 18 vs 25 (attention exceeds). Criterion not met retrospectively; decided on P4 traces.
