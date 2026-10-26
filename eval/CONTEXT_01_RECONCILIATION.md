# CONTEXT-01 reconciliation — reference arithmetic, P15 ground truth, POST_HOC

No inference. Persisted 63-run evidence at `eval/CONTEXT_01_63RUN_EVIDENCE.json`
(63 runs, $0.946, sanitized run IDs, wall, bundle, spend). Raw ledger
archived ephemerally at `/tmp/qual/ledger.json`; the eval copy is the
durable, auditable record. Completed-clean vs incomplete is preserved
via `status` (`complete` vs `incomplete_budget_exhausted`) and
`stage` (`budget_exhausted` is turn-budget, 12 turns, not spend).

## Reference arithmetic

Total distinct reference concerns = **12**, not 11:

- FW3 2 (reboots, median)
- KL1 1 (readme-totals; isolation is MISS per M9.2 adjudication, not counted)
- L08 3 (lms, epcore, 403)
- L06 1 (trim; subscription staleness is POST_HOC)
- L10 1 (evidenceTier; tier-trap is correctly avoided 9/9, not counted)
- WT4 0 (silence; correct)
- P15 4 (ASR readiness, vision preflight, image-judge, portainer password)

12 = 2+1+3+1+1+0+4. The pilot/decision reports that listed 11 omitted one
P15 duplicate collapse (P15-2b/3b are same doc lines) but counted 4 distinct
— 12 is correct and was used for per-review recall denominators
(0.52 A, 0.52 B, 0.57 C). No decision threshold changes (1.3×/+2).

All per-case, per-issue counts in the decision report remain correct
under 12; the 11 was a summary typo.

## P15 configuration / ground truth at frozen head 58c26e2

P15 @ `58c26e20` is the **final** head (H5) of 5 successive PR #15
heads (H1 `806cc393` → H5 `58c26e20`). The 5 historical runs (watcher)
found 7 raw findings collapsed to 4 distinct concerns across H1–H5, but
only **1** of those 4 is still present at H5:

- **Still present at H5:** P15-1 vision preflight (app.py 370–377,
  `submission preflight can queue jobs without a qualified vision
  backend`) — confirmed in H5 run `760c125d` and in pilot A/B at H5
  (1/3 each).
- **Fixed by H5:** P15-2a/3a/5 ASR readiness (inference.py now gates on
  `qualified_for_asr` via `_identity` and `record_qualified`; `provider_status`
  shows `qualified_for_asr`); default-compose ASR disabled is now
  correctly reported via `provider_status`; portainer password is now
  documented via `env_file` vs stack variables (deploy.md 167/194) and
  the standalone Portainer file correctly notes no Whisper service.

The frozen H5 bundle (87–90K chars, 34 files) confirms: Dockerfile,
compose, deploy.md, inference.py, auth.js, etc. are present, but the
historical ASR-URL-alone and default-compose findings no longer replay
as ATTESTED at H5's lines.

Pilot POST_HOC at H5 (not in original 4, tagged separately):

- `readiness rejection leaves uploaded media` (app.py)
- `failed guided endpoint test still saves` / `replaces` (inference.py `save_one`)
- `tls-terminating proxy can issue session` (app.py/compose)
- `login accepts unlimited password guesses` (auth)

These 4 were found in 2/3 A/B/C reps at H5 and are verifiably present
in the H5 checkout (byte-replay), but were not part of the frozen
reference. Per `CONTEXT_01_REFERENCE.md`, they are `POST_HOC` — not
counted towards the 4-concern recall, not retrofitted, and require
separate adjudication. They explain why pilot/decision P15 recall
appeared 1/4 — the denominator should be 1 for H5, not 4, when
evaluating H5 alone. Across the PR's evolution, the denominator is 4,
but at frozen H5 the achievable distinct concerns is 1 + 4 POST_HOC.

No silent omission: tests/generated remain listed (e.g., 8 test files
in P15 bundle) and never quoted.

## POST_HOC handling

All non-reference advisories in pilot/decision (FW3 `other:`, KL1
`pair validation`/`development plan`, P15 above) are tagged `POST_HOC`
in this reconciliation and were excluded from recall/precision
calculations. False positives remain 0.24/run (WT4 0/9, others ≤1/run,
≤A+0.1). No arm's precision is materially worse than A under
registered criteria.

## Persisted evidence

- `eval/CONTEXT_01_63RUN_EVIDENCE.json` — 63 sanitized run records
  (case, arm, rep, status, stage, tool_calls, usd, usage, wall,
  bundle, spend_summary, sanitized_run_id). Deterministic, no
  absolute paths, no credentials.
- The ledger remains spend-capped ($0.946 <$8) with all
  `CONFIRMED_USAGE`, zero `PRICING_PARTIAL`, correct `short` tiers
  and 83–85% cache hit-rate from request 2 — as reported in pilot and
  decision reports.

No inference, no budget change, no enablement.
