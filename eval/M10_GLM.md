# M10-GLM — GLM 5.3 Flash challenger: pilot verdict STOP (recall signal, reliability fail)

## Configuration (recorded 2026-10-23, before any GLM evaluation run)

- Model `glm-5.3-flash` (served id confirmed by raw probe), Coding Plan
  endpoint `https://api.z.ai/api/coding/paas/v4` (the general endpoint
  does not consume plan quota; first probe round failed there on balance).
- Thinking FORCED-ON (provider cannot disable for flash) +
  `clear_thinking: false` (Preserved Thinking, provider recommendation
  for coding/agent use), `reasoning_effort` unset (provider default).
  SDK reasoning-replay hook enabled for glm models (required to return
  complete unmodified reasoning_content with tool results).
- Bounds identical to controls: `max_turns=12`, `tool_budget=40`,
  `max_tokens=8192`/request, canonical minimal prompt, file-list
  inventory, frozen SHAs. Quota billing: tokens recorded confirmed,
  USD unconfirmed (no token×rate math on subscription quota);
  preflight carve-out via `quota_` rate_status prefix.
- Data scope: frozen M9.2 cases only (3 public + 3 private repos sent
  under this instruction's authorization; same material class already
  sent to OpenAI/Anthropic). Provider training posture unverified;
  profile records this rather than asserting.
- Initial block: stale key quoted pay-as-you-go balance (code 1113) on
  both endpoints; resumed after the owner regenerated a Coding Plan key.
  No inference occurred under the blocked key ($0, excluded).

## Integration: PASS

Raw probe: thinking works (`reasoning_content` returned), usage carries
`prompt_tokens_details.cached_tokens` + `completion_tokens_details.reasoning_tokens`
→ ledger details are provider-reported, verified. WT4 smoke: complete
correct silence, 11 tools across 5 types (first backend to use
`lookup_docs`/`related_files`), pinned SHAs both sides, 37k in / 3.5k
out, 28k cached + 2.5k reasoning, boundary match, $0 marginal.
Budget-exhaustion path genuine (3-call cap → block notices → finalize).
2 new unit tests; full suite green (164 passed).

## Controlled pilot (1 rep × 6 cases): STOP — no reps 2–3

| Case | Status | Tools | Findings | Cost |
|---|---|---|---|---|
| WT4 | complete (correct silence) | 11 | 0 | quota |
| FW3 | incomplete (turns exhausted) | 31 | 0 | quota |
| KL1 | incomplete (turns exhausted) | 30 | 0 | quota |
| L08 | **complete** | 24 | **4** | quota |
| L06 | incomplete (turns exhausted) | 30 | 0 | quota |
| L10 | incomplete (turns exhausted) | 21 | 0 | quota |

Agent adjudication (NON-BLIND, owner spot-check pending) of the 4 L08
findings, each verified against the pinned work tree:

1. **CONFIRMED (L08-lms, re-detection)**: lms-dev backend dials
   `:443` while its vhost binds `:9443`; all four sibling backends dial
   `:9443`. Luna-A 3/3, Haiku 3/6 already held this.
2. **CONFIRMED (L08-epcore, re-detection)**: epcore backend → `:9443`
   with no epcore vhost on any listener. Luna-A 2/3 already held this.
3. **CONFIRMED (novel:cloudflare-hdr, INCREMENTAL vs baseline)**:
   public `bind :443`, no Cloudflare source restriction; both new rate
   limits gated on client-supplied `CF-Connecting-IP`; backends
   overwrite `X-Real-IP` with it. Omit → limits off; forge → unlimited
   keys. Luna-A 0/3 and all Haiku 0/6 missed it; Luna-B/C held it.
4. **CONFIRMED-LOW (novel:varnish-candidate-stale)**: candidate VCL
   targets the same `127.0.0.1:8083` the new Apache proxies to
   directly; nothing wires a Varnish listener (commit retires Varnish).
   Real stale artifact, minor, intent uncertain.

GLM-A single-run recall on L08 (3 material + 1 low) beats a Luna-A
single (1.67 mean) and includes a Luna-A miss — the only challenger so
far to do so. But completion is 2/6 (33%) vs Luna-A 78% / Haiku-A 72%,
and the four failures share one structural mode (12 turns burned in
long-reasoning investigation, 21–31 tools, no final submitted) —
measurement noise does not explain it, and reruns would be spending to
chase disappointing outcomes, which the brief forbids.

**No reps 2–3.** The recall signal is real but cannot promote a model
that completes one review in three to any routine slot; more reps
would refine a recall estimate for a disqualified candidate. GLM joins
Haiku's verdict shape (recall without reliability) at lower
reliability. A turn-budget or non-preserved-thinking experiment is a
new proposal, not a follow-up I run unilaterally.

## Side observations (recorded, not acted on)

- GLM is the only backend to call `find_definition`, `find_callers`,
  `lookup_docs`, and `related_files` — the P1.2 "dead weight" verdict
  on symbol/docs tools is model-dependent, not absolute.
- GLM wall time is the highest measured (L08 383 s vs Luna ~52 s,
  Haiku ~166 s); quota cost is $0 marginal but latency is a
  practical-review cost too.
- Quota accounting worked as designed: incomplete runs UNKNOWN_USAGE
  with tokens preserved; complete runs CONFIRMED_USAGE with USD None.
  Ledger `spent_total` unchanged at $0.389 / $5.00.

## Records

- Digest: `/tmp/m10/digests/GLM-L08.md` (private). Adjudication above
  is agent-performed, non-blind; the 3 material claims await the same
  owner-rating channel as the F-batch (`m10_feedback.py`).
