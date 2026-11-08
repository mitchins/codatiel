# REVIEW-CONTRACT-01B — Decision Report

Decision: **GUIDED_REVIEW_EXECUTABLE_PARTIAL**

The deterministic agent loop works end to end; live-model execution
remains incomplete (no local endpoint available). Per the mission,
this is an execution-readiness decision, not a substantive-precision
qualification.

## What was built

- Corrected harness (`eval/review_harness.py`): all six D1–D6 defects
  fixed and regression-tested; plus nine further defects found by the
  correctness reviewer and repaired (deletion-blind `get_changed_files`,
  malformed-range divergence between `record_evidence`/`publish_eligible`,
  discarded evidence gaps, mislabelled step-cap, conflated tool log,
  un-bounded `validate_grounding`, dead code, unintegrated dedupe,
  redundant NUL guard).
- `investigate()` implements Orient → Investigate → Counterfactual →
  Challenge → Decide with a model-agnostic `act()` protocol, tool
  budget, and explicit terminal states (`voluntary_stop`,
  `budget_exhausted`, `step_cap_reached`, `error`). All terminal
  branches preserve evidence gaps.
- Contract contradiction resolved: S5 is internal-only; conflict rule 4
  no longer overrides silence; prompt README §§D/E/F list working tools
  only (lexical lookup honestly labelled), carry the strengthened
  `publish_eligible` pseudocode, and require `comment_location`.
- Evidence boundary operational: untrusted envelopes with provenance,
  revision-validated confinement (absolute/traversal/symlink rejected),
  injection telemetry attached at both `record_evidence` and every
  string tool output. Scanner is documented as telemetry, not
  prevention.
- Structural checks hardened (§5): valid revision, exact non-empty
  source-range excerpt, permitted strength, counterfactual required for
  any material behavioural concern, full `supported_regression` field
  set, valid HEAD comment location, batch dedupe via
  `render_proposals`. Documented as shape/provenance only; human
  review remains the qualification authority.
- Smoke: 6/6 development cases behave as designed (4 proposals, 1
  silence, 1 internal-only). Traces capture exact prompts, tool
  request/response pairs, counterfactuals, evidence, alternatives,
  proposals, cost ($0.00), approximate tokens, latency, and
  bounds-checked grounding validation.
- Endpoint limitation concretely recorded: 127.0.0.1:11434 and :1234
  both connection-refused at smoke time. FakeModel performs no
  reasoning; traces validate plumbing, not model competence.

## Independent review (all deepseek-reviewer, read-only)

- Correctness: PASS WITH NOTES → all nine notes repaired and
  re-tested (6 new regression tests); smoke re-run clean.
- Product: PASS → 4/5 HELPFUL-or-correct-silence, A11-sanitized
  NEUTRAL-by-design. Watch-item: internal records are only safe if a
  human triage consumer exists (`render_proposals.retained_triage` is
  the sink; no consumer wired yet — first item for the live pilot).
- Compliance: COMPLIANT WITH NOTES → note (prompt injected only
  README) repaired: all three artifacts now govern the prompt.

## Compliance statement

No AUTH3R adjudicator reused (imports: difflib/re/time/pathlib only).
No paid APIs, no publication path, no new retrieval/proof system, no
model-controlled publication. No contract redesign beyond the mandated
S5 correction. No frozen dispositions reopened.

## Remaining before live qualification

1. Live-model execution against a real endpoint (guidebook-length
   prompt); FakeModel traces prove nothing about reasoning.
2. A human triage consumer for `retained_triage` records.
3. `find_definition`/`find_callers` remain approximate lexical lookup
   (documented; no index built per mission scope).

## Tests

464 passed, 1 skipped (full suite). New: 17 defect-regression tests
(01B), 9 boundary tests (01B), 11 harness tests (01A).
