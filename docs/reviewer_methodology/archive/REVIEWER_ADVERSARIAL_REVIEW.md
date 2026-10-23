# REVIEWER_ADVERSARIAL_REVIEW.md

Three independent reviewers challenged the reviewer-contract design.
All objections verified against source before disposition.

## R1 — Instruction Conflict: INTERNALLY-INCONSISTENT (one conflict)

- **C5 vs output contract examples — ACCEPTED.** C5 mandates "would"
  for hypotheticals; the output contract's own canonical example uses
  indicative tense ("now returns True on HEAD but raised Denied on
  BASE"). Fixed: the counterfactual field descriptions and claim-ladder
  examples now use derived/modal form ("derived outcome under the
  stated scenario: would raise… would return…").
- All other checks: no conflict found. `supported_regression` is
  reachable; S3/M3 are complementary; A2/Ladder harmonized via
  conflict-resolution rule 1; `material_concern` is independently
  publishable without `supported_regression` requirements.

## R2 — Over-accusation/Over-conservatism: BALANCED (3 structural defects)

**NOTE: The case analyses below are DESIGN WALKTHROUGHS (the reviewer
manual applied by a human analyst), NOT model accuracy results. No
model was run. They demonstrate that the guidebook's instructions,
when followed correctly, lead to the correct outcome — they do not
demonstrate that a live model will follow them correctly.**

- **005/broken → supported_regression, published: DESIGN-CORRECT.** The
  gate now admits the pair; the counterfactual resolves the predicate;
  no charter or test suppresses it.
- **033/safe → silence: CORRECT.** The transactional-staging bullet +
  A1 benign explanation + §5 outcome trace correctly resolves to
  silence. The ordering-inversion bullet is a residual FP trap,
  recorded.
- **A11 → material_concern (visible qualified advisory): CORRECT.**
  S5 overrides silence; conflict-resolution #4 forces visibility.
  BUT D-1: the informational tier has no publication channel in the
  output contract (only material_concern and supported_regression
  publish) — structurally forces the A11 advisory into burial.
  CORRECTED (R3b-2 corrective): informational/observed_difference
  findings are INTERNAL triage records, never published. Only
  material_concern and supported_regression may become proposed
  public comments. The A11 advisory is a DESIGN-WALKTHROUGH outcome,
  not a production capability.
- **052/broken → supported_regression, published: CORRECT.** E3
  enables the match (forces body verification, prevents importing
  051-safe's dissent).
- **041-043/safe → silence: CORRECT.** Charter lookup + kind-vs-flag
  discriminator visible in the hunk. Caveat: charter lookup is
  content-anchored, not structurally enforced.
- **D-2 (D4 auto-promotion is a deterministic FP channel): ACCEPTED.**
  D4 removed from the publication policy. `observed_difference` is no
  longer auto-promoted.
- **80% standard: NEEDS AMENDMENT.** "Real and material and useful"
  is conjunctive but "material and useful" is not operationalized.
  Amended: a published comment counts as TP only if real AND material
  AND useful (immaterial-but-real counts as FP). Per-PR noise cap
  added (dedup by underlying problem extended to triviality).

## R3 — Evidence/Tool/Product/Ask:

- **Injection through evidence/charter attachment: FAIL.** The
  constant-attachment mechanism and charter-attachment channel can
  inject attacker-positioned "policy" text. ACCEPTED as a recorded
  defect for R3b-2 implementation (the fix requires trusted-source
  verification, which is out of preflight scope).
- **Tool descriptions: NEEDS-WORK.** `compare_base_head` and
  `get_policy_documents` do not exist; `find_references` is callers-
  only; `get_changed_hunks` is `diff_window` under a different name.
  ACCEPTED: tool descriptions marked with real names and
  availability status.
- **80% materiality bar: NEEDS-WORK.** Technically-correct noise
  passes the bar as mechanized. ACCEPTED: materiality-anchored
  scoring (immaterial-but-real = FP) + per-PR noise cap.
- **Ask compliance: PASS.** The guidebook is genuinely different from
  AUTH3R: demotion replaces suppression, the claim ladder creates a
  publishable middle rung, the conjunctive abstention engine is gone.
  Caveat: all artifacts are untested paper designs.

## Dispositions applied to deliverables

1. Output contract: counterfactual field descriptions use modal form.
2. Output contract: informational publication tier added.
3. Publication policy: D4 auto-promotion removed.
4. Publication policy: per-PR noise cap + materiality-anchored TP definition.
5. Tool descriptions: real names and availability status marked.
6. Guidebook: ordering-inversion bullet carries an inline caveat.
7. 80% standard: materiality-anchored definition.
