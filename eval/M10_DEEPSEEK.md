# M10 P2 — DeepSeek Flash challenger: verdict STOP (no incremental value)

## Configuration (recorded 2026-10-23, before any evaluation run)

- Model `deepseek-flash` (served id confirmed by raw probe; provider docs
  claim backing `DeepSeek-V4.1-Flash-1017`), base `https://api.deepseek.com`.
- Thinking ENABLED explicitly (`extra_body={"thinking": {"type": "enabled"}}`,
  default effort). Rationale recorded in `runtime_sdk.deepseek_model`:
  provider default, SDK reasoning_content replay support (required with
  tools, else HTTP 400), M9.2 reasoning-side hypothesis, Haiku parity.
- Bounds identical to controls: `max_turns=12`, `tool_budget=40`,
  `max_tokens=8192`/request, canonical minimal prompt, file-list inventory,
  frozen SHAs. Per-review ceiling $1.00; peak split pricing
  (miss 0.30 / hit 0.006 / out 1.20 per MTok, official docs 2026-10-23).
- Data scope: frozen M9.2 cases only (3 public + 3 private repos —
  KL1/L06/L08 are private; sent under this brief's Phase 2 authorization,
  same material class already sent to OpenAI/Anthropic). Provider privacy
  posture unverified; profile records this rather than asserting.

## Integration: PASS

Raw probe: thinking works (`reasoning_content` returned), usage carries
`prompt_tokens_details` (cache hit/miss) + `completion_tokens_details`
(reasoning tokens) — ledger details are provider-reported, verified.
Smoke WT4 (silence control): 12 tools across 4 types, evidence SHAs ==
frozen base/head, model finalized with a correct, thorough silence,
CONFIRMED $0.0054 (34k cached + 710 reasoning tokens, split-priced),
boundary hash match, canonical instructions, $0 recording errors.
Budget-exhaustion path genuine (3-call cap → explicit block notices →
clean finalize, partial ledger preserved). No silent fallback, no
retries, no 400s. 5 unit tests added, full suite green (162 passed).

## Controlled pilot (1 rep × 6 cases): FAIL — stop

| Case | Status | Tools | Findings | Silence | Cost |
|---|---|---|---|---|---|
| WT4 | complete | 12 | 0 | correct silence | $0.0054 |
| FW3 | incomplete (turns exhausted) | 22 | 0 | none | $0.0499 |
| KL1 | incomplete (turns exhausted) | 23 | 0 | none | $0.0278 |
| L08 | incomplete (turns exhausted) | 33 | 0 | none | $0.0315 |
| L06 | incomplete (turns exhausted) | 23 | 0 | none | $0.0297 |
| L10 | incomplete (turns exhausted) | 21 | 0 | none | $0.0324 |

Zero material findings, zero silences on all five real cases; the model
investigated (21–33 tool calls) but never converged to a final within 12
turns. Mechanism: thinking-mode reasoning replay concatenates prior
reasoning into every request (400–800k input tokens/run, ~85% cache
hits — replay functioned, cost stayed ~$0.03/run); 7–10k output tokens
per run are overwhelmingly reasoning (e.g. FW3: 9,365 of 10,780). Long
deliberation, no verdict. Accounting stayed clean throughout
(0 recording errors, 0 unknown responses).

Per the brief, reps 2–3 require "plausible incremental value": 0
findings + 1/6 completion is the opposite. The stop condition
("no worthwhile incremental value after the pilot") applies: **no
further DeepSeek repetitions, no non-thinking switch** (that would be
tuning the mode against observed outcomes). Adjudication is moot —
there is nothing to blind (0 findings; incompletes carry no verdict).

## Disposition

- **DeepSeek is neither a better default nor a useful complement in the
  tested configuration.** It is disqualified on reliability (17%
  completion vs 78% Luna-A / 72% Haiku-A) before cost or recall even
  matter. A non-thinking variant might behave differently; that is a new
  experiment the owner may authorize, not a follow-up I run unilaterally.
- Integration plumbing (adapter, split pricing, detail capture) is kept:
  reviewed, tested, and reusable for any future provider. It changes no
  existing backend behavior (all suites green).
- Spend: $0.179 / $5.00. Runs preserved under `/tmp/m10` (to be archived
  with the rest of M10 at close).
