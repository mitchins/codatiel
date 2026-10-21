# Codatiel I2 — Agent Development Rules

## Product identity — mandatory

**Codatiel I2 is the agentic code-review harness.**

The product is not a language model reviewing a preassembled diff or a static evidence packet.

A genuine Codatiel I2 review runs through the existing agentic review harness, with its
repository investigation capabilities, revision-bound evidence retrieval, model-directed
tool use, review reasoning, and evidence/claim validation.

A model must be able to investigate the repository, request additional relevant code and
evidence, inspect BASE and HEAD, and continue reasoning from tool results within bounded
resources.

**Giving Luna, Gemma, Haiku or any other model a preassembled Git diff and asking for a
review does not constitute running Codatiel I2.**

### Forbidden substitutions

Agents MUST NOT:

- Replace the Codatiel I2 harness with a single-turn model call and describe the result as
  Codatiel performance.
- Substitute static packets for repository investigation without explicit authorization for
  a separately identified baseline experiment.
- Remove or bypass the product's source tools, investigation loop or evidence validation to
  simplify a test.
- Silently fall back to a static reviewer when the agentic harness fails or a model cannot
  use its tools.
- Claim an experiment tested Codatiel merely because it reused Codatiel prompts, packets,
  validators or models.
- Attribute findings, misses, precision, recall or abstentions from another reviewer
  implementation to Codatiel I2.

Static/single-turn reviewers may be used as **explicitly labelled comparative baselines**,
never as substitutes for product testing.

### Model integration

Luna, Claude Haiku and local Gemma are alternative reasoning backends for the SAME Codatiel
I2 harness.

Where provider APIs differ, adapt the provider interface to support the harness.

Do not change the underlying review architecture or silently degrade the capabilities
available to a model.

Any capability incompatibility must be reported explicitly.

### Qualification and independent review

Before accepting any experiment as Codatiel I2 evidence, the independent reviewer MUST
verify:

1. Which executable implementation actually ran.
2. That it was the existing Codatiel I2 agentic harness.
3. That repository investigation and relevant source tools were genuinely available.
4. That model-directed investigation could proceed beyond the initial evidence.
5. That the execution trace demonstrates the reported investigation and result.
6. That no static-packet substitute, mock reviewer or silent fallback was used.

Not every review needs to invoke every tool. Tool availability and correct operation are
required; unnecessary tool calls are not.

**If the product was not exercised, the experiment is invalid as Codatiel I2
qualification — regardless of model quality, passing tests or reviewer approval.**

A reviewer who fails to check the system under test has not completed the review.

### Evidence and observability

Preserve enough execution evidence to establish:

- Model, prompt and harness version.
- Repository and BASE/HEAD revisions.
- Actual model-visible evidence.
- Available and invoked tools.
- Tool arguments and returned evidence.
- Candidate findings and their disposition, when explicitly recorded.
- Provider-returned reasoning summaries, where supported.
- Final finding or silence, including evidence validation.

Do not claim access to hidden reasoning that the provider does not expose.

Preserve existing privacy, spending and retention controls.

### Development discipline

- Inspect and reuse existing Codatiel I2 components before implementing replacements.
- Use independent reviewers for material changes.
- Test actual behaviour, not just the existence of interfaces.
- Treat failed or incomplete tests as such.
- Preserve historical evidence; correct incorrect classifications without rewriting
  original results.
- Do not expand the architecture merely to satisfy a test.
- Prefer bounded fixes with demonstrated product benefit.

**The goal is a useful agentic code reviewer, not a benchmark of how well a model can
reason from a truncated Git diff.**

---

## Canonical runtime (REAL-REVIEW-01 integration decision, binding)

### One product, one agent runtime

- Codatiel I2 is one product. Its canonical agent execution runtime is the **OpenAI Agents
  SDK** (`Agent` + `Runner.run`, bounded `max_turns`), driving Codatiel I2's
  domain-specific tools and product contracts.
- The SDK replaces redundant agent-loop orchestration only. It does not replace Codatiel's
  review functionality: revision-bound source tools (`codatiel_i2/core`, `codatiel_i2/tools`
  semantics: BASE/HEAD SHA binding, replay validation via `core/evidence.py`), finding
  validation and disposition (`core/findings.py`, `verify/trust.py`), and the
  `REVIEWER_OUTPUT_CONTRACT` / `REVIEWER_PUBLICATION_POLICY` gates remain authoritative.
- No second agent orchestration framework. No new agent framework. No unrelated redesign.

### What is canonical and what is not

- **Canonical:** an SDK `Runner.run` execution in which the model directs investigation
  through SDK-exposed I2 source tools against pinned BASE/HEAD checkouts, with evidence
  replay and finding disposition applied, and a full local transcript preserved.
  A run in which the model issues zero tool calls and reviews only initial evidence is a
  static review regardless of which runtime executed it, and is governed by the
  prohibitions above.
- **Not canonical (explicitly labelled baselines only):**
  - `eval/review_harness.py` `ReviewHarness.investigate` with the `eval/live_model.py`
    `LiveModel` custom JSON tool loop — prior evaluation scaffolding; may be cited as a
    comparative baseline, never as Codatiel I2 qualification evidence.
  - `eval/shadow_ops.py` `SingleTurnReviewer`, `eval/shadow_live_01.py`, capacity/frontier
    single-shot paths — static-packet baselines by definition.
  - Outer-repo `tools/codatiel_agents_sdk_spike.py` Phase A–D pipeline and its
    reimplemented surface tools / local verdict taxonomy — prototype scaffolding that
    duplicates orchestration and bypasses I2 evidence binding; superseded by the canonical
    runtime. Retained in history only.
- A mock/scripted model driving the canonical SDK runtime with real source tools is a
  **runtime smoke test**, not a review result and not qualification evidence.

### Model backends

- Luna (OpenAI, Code Review project, `STANDARD_API_NO_TRAINING`, `store=false`), Claude
  Haiku 5.5 (Anthropic Messages API via a dedicated provider adapter; thinking blocks are
  provider-private and only provider-exposed summaries may be recorded), and local Gemma
  (OpenAI-compatible endpoint via `OpenAIChatCompletionsModel`) are backends to the same
  runtime with the same tool surface. Provider adapters are required work: the existing
  `eval/haiku_provider.py` single-turn adapter is a baseline-only path, not a backend to
  this runtime.
- Provider-specific protocol adaptation is permitted. Per-model changes to the
  investigation mission, evidence rules, or scoring criteria are not.
- Incompatibility that removes a tool or degrades evidence for one model must be reported
  explicitly; silent fallback to static review is prohibited.
- Status: the canonical SDK runtime module is under construction in REAL-REVIEW-01. Until
  it lands with a passing mock-inference smoke test, no new runs may be claimed as
  Codatiel I2 evidence; new model outputs are baselines by default.

### Tracing, privacy, spending (preserved controls)

- SDK cloud tracing MUST be disabled (`set_tracing_disabled(True)` or equivalent);
  forensic traces stay local. No private repository traces to any additional service.
- Previously approved provider policies stand: OpenAI `STANDARD_API_NO_TRAINING`;
  Anthropic commercial no-training default with ~30-day retention. Extending either policy
  to new data classes requires an explicit operator decision.
- Paid-inference ceilings, per-run budgets, no-retry/no-escalation rules, and the SMTP2GO
  digest control from the governing experiment brief remain in force and are restated in
  that brief, not weakened here.

### Conflict resolution

Existing project instructions (`REVIEWER_RULES_OF_ENGAGEMENT.md`,
`REVIEWER_INVESTIGATION_GUIDEBOOK.md`, `REVIEWER_OUTPUT_CONTRACT.md`,
`REVIEWER_PUBLICATION_POLICY.md`, `REVIEWER_READINESS.md`, `SHADOW_*` / `REVIEWER_01*` /
`FRONTIER_*` decisions, forensics ledgers) are preserved as historical evidence and
baseline records. Where any prior text treats a static-packet or single-turn result as
Codatiel I2 qualification evidence, **this document supersedes it**: those results stand
unchanged as labelled baselines. Original traces and verdicts are never rewritten;
incorrect classifications are corrected by new appended records.

Known architecture conflict, resolved: `README.md` (Pipeline) and
`docs/PRODUCT_CONTRACT.md` (§2 Pipeline, "No general reviewer in the critical path")
describe an autonomous detector pipeline. That pipeline's stages — workspace
classification, breadth/exact retrieval, specialist verification, typed finding
validation, publish gate — are preserved as the domain machinery the canonical agent
runtime orchestrates and as the validation gates every finding must pass. What is
resolved: an autonomous run of that pipeline with no model-directed investigation is not
a Codatiel I2 review and cannot generate qualification evidence. "No general reviewer"
continues to restrict scope (five regression classes only); within that scope,
investigation is model-directed.
