# REVIEW-DELEGATE-01 — Final Report: Does Subagent Delegation Help Terra?

Frozen 2026-11-09/10. Verdict: **MIXED**. Recommendation: **HOLD** (do not
integrate delegation; C-pattern shows promise for security cases only).

## 1. Implementation (minimal delegation layer)

New module `codatiel_i2/delegate.py` (~450 lines) on experimental branch
`exp/delegate-01` (commits `6f6ceee`, `6b3a893`, `69a94b1`):

- **Lead (Terra)** runs the unchanged canonical review (B+FINAL 39/11,
  bundle-0.1, same settings as the COMPLEMENT-02 control) plus one async
  delegation tool per subagent and a prompt addendum (cap, include genuine
  subagent finds, challenge-with-reason).
- **Subagents (Luna; Haiku in arm C)** are SDK `Agent`s with own model,
  instructions, read-only I2 tools, own `ReviewContext` (own tool budget),
  own `SpendLedger` (own spend ceiling: Luna $0.25, Haiku $0.50/run) and a
  file-backed `SQLiteSession` scoped to one (arm, case, rep, role).
- **Independent sweep first** (inventory only, lead has not run yet → blind
  by construction), then parent-directed follow-ups in the same session
  (cap 3 Luna / 2 Haiku). All outputs append to `ledger.jsonl`; the lead
  cannot edit it.
- **Single event loop per review.** The first build failed 11/18 early runs:
  nested `run_in_executor + inner asyncio.run` strands the SDK's
  process-global httpx pool on a dead loop (`Event loop is closed`), and
  naive pool resets poison pre-constructed models (`client has been
  closed`). Fix: pure-coroutine subagent calls sharing the lead's loop
  (`arun_review` core), plus loop-change re-resolution of string-origin
  models with stale-pool detach (`UsageRecordingModel._ensure_loop_fresh`,
  `_POOL_LOOP`). Verified with real models: sequential sync loops and
  gate-pattern shared-wrapper loops all pass.

## 2. Session verification (7 tests, `tests/test_delegation_session.py`)

Scripted models, real SDK/Runner/tools/sessions — no inference. Proves:
(1) investigation retained across questions (item growth, marker present);
(2) sweep-then-follow-up with `resumption_ok`; (3) separate tag starts
empty + tag re-init cleans (isolation/cleanup); (4) follow-up cap refuses
without inference; (5) subagent timeout surfaces as error; (6) task
cancellation leaves no partial ledger entry; (7) end-to-end scripted lead
calling `delegate_investigation` gets the retained-context answer, recorded
in `metrics.json` delegation block. All pass.

## 3. Haiku gate: PASS ($0.039)

Bounded Haiku sweep (3,898 chars, 17 tool calls) + follow-up with
`resumption_ok: true` on D01-Y23. One caveat: on the larger FW3 diff the
Haiku sweep hit `MaxTurnsExceeded` (8 turns insufficient), though its
follow-up succeeded — bounded investigations are tight for big diffs.

## 4. Campaign (68 complete runs; $11.48 + $0.04 gate = $11.52 / $12 cap)

- Arms × cases × reps: A (Terra) 15×2, B (Terra+Luna) 15×2, C
  (Terra+Luna+Haiku) 6×2 on preselected high-risk cases
  (FW3, KL1, Y15, Y22, Y23, Y24). Execution order randomized per case
  (seed 20261110). Blinded adjudication (seed 20261111, 179 findings,
  RC + severity rules of COMPLEMENT-02, 56 validated RCs).
- C stopped at 8/12 by the campaign-ceiling guard (spent $11.48; reserve
  $0.60): both Y23/Y24 reps unrun. C is therefore PARTIAL (4/6 cases).
- Completion 100% (all started runs complete, 0 inconclusive). FP load:
  A 9, B 10, C 4. UNRESOLVED: A 3, B 2, C 4.

## 5. A/B/C comparison (best-of-two validated-RC unions)

| Arm | RCs | P0/P1/P2 | P3 | Cost (parent+sub) | Median parent tools |
|---|---|---|---|---|---|
| A control | 41 | 20 | 21 | $3.75 ($0.125/run) | 23.5 |
| B +Luna | 35 | 19 | 16 | $4.01 ($0.134/run, +7%) | 20.5 |
| C +Luna+Haiku (4 cases) | 20 | 9 | 11 | $3.63 ($0.454/run, 3.6×) | 22.5 |

Single-rep sets: A r1 33 / r2 32; B r1 22 / r2 29 (high intra-arm noise).

**B gains (missed by control), 6:** P2 RC-L10-CODABLE (2/2 reps, present in
both Luna sweeps — genuine subagent contribution), P3 SCORM/XUNIT/RECAP
(P15)/Y22-400/Y24-ANNOT (the last also in the Luna sweep).

**B losses (missed by challenger), 12:** P1 RC-L08-EPCORE (A-only 1
instance — fragile), **P2 RC-Y15-CLIIDENT (A 2/2 → B 0/2, robust)**,
P3 CRC/DUPKEY/ACL/SHIELD/VARNISH/VCL/CAPFLAG(P15)/CURLDOC/RECAP(Y15)/RELEASE.

**C gains on its 4 cases (missed by A+B), 9:** P2 LOGIN (2/2, session-DB
proves subagent engagement), P2 DENO, P2 Y22-404, P3 BIND/LATCHED-COUNT/
MEDIAN/ASRLEAK/COOKIE/DEFAULTMODEL. C also lost RECAP-Y15 (P3) and found
nothing on KL1 (missed UUID P2) — C is mixed too, on partial coverage.

**Deterioration mechanism (measured):** B parents used fewer own tools
(20.5 vs 23.5 median; Y23: 19/21 → 12/14) while subs did 15–20 calls.
Failure mode = diffusion of responsibility: on Y23 the Luna sub mentioned
`loader` 36–38× in-session yet dismissed the docstring guarantee, and the
parent never checked it — both reps silent on a case A solved 2/2.
Delegation displaced, rather than extended, lead coverage.

## 6. Verdict: MIXED — HOLD

- Not BETTER: a robust P2 gain (CODABLE) is cancelled by a robust P2 loss
  (CLIIDENT) plus a (fragile) P1 loss and −5 net P3s, at +7% cost.
- Not WORSE: completion/FP parity, genuine subagent-attributed gains, and
  the C-pattern found 3 P2s (incl. unthrottled login) that 4 control-class
  runs missed.
- Not INCONCLUSIVE: the material difference (displacement + P2 swap) is
  demonstrated across 30 matched pairs.
- **HOLD:** do not integrate delegation. A cost-capped, security-scoped
  C follow-up (Haiku sweep bounds raised for large diffs, Y23/Y24
  coverage) is the only direction with positive evidence; universal
  Luna-delegation is not recommended.

## 7. Reproduction

Branch `exp/delegate-01`. Unit proof (free):
`PYTHONPATH=repo .venv/bin/python -m pytest tests/test_delegation_session.py`.
Campaign (paid, capped): `zsh delegate-01/scripts/run_d01.sh <A|B|C>
<CASE> <REP>` with `D01_ROOT/D01_CEILING_USD/D01_RESERVE_USD` set; roster
and ceiling guards refuse reruns/overruns; `batch_d01.py [A B C]`
randomizes (seed 20261110). Adjudication:
`d01_build_blind.py → d01_adjudications.py → d01_apply_adj.py →
d01_compare.py → d01_build_deliverables.py`. Raw traces/session DBs stay
in `delegate-01/` (outside git, 700); sanitized `eval/DELEGATE_01_RUNS.json`
(68 runs) + `eval/DELEGATE_01_FINDINGS.json` (56 RCs) are committed.
No watcher/docker/GitHub/production changes; 170 repos stayed paused.

## 8. Important defects (selection)

- Gains: L10-CODABLE P2 (B; breaking Codable decode, no migration path);
  Y15-LOGIN P2 (C 2/2; no throttling/lockout, verified); Y15-DENO P2 (C;
  undigested executable fetch); Y22-404 P2 (C; unmatched section serves
  first section as 200, verified); FW3-MEDIAN P3 (C 2/2, verified
  upper-middle); Y15-DEFAULTMODEL P3 (C; probe/models[0] vs recorded
  empty model, verified).
- Losses: L08-EPCORE P1 (A-only, single instance); Y15-CLIIDENT P2
  (A 2/2 → B 0/2; CLI record lacks model/key-env, gate prunes);
  Y23-RELEASE P3 (A 2/2 → B 0/0; false no-masking docstring, verified).
- New-case notes: Y15⊆P15 defect correspondence (same auth/readiness code
  in both branches; Y15 branch fixed the record_qualified TOCTOU with
  StaleIdentityError, P15 branch still races — RC-P15-RECQUAL P3).
