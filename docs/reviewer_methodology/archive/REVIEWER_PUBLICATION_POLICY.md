# REVIEWER_PUBLICATION_POLICY.md

## Codatiel Reviewer — Publication Policy

Version: 1.0-draft. The publication policy is deterministic post-
processing. The model proposes; this policy decides.

## Publication rule

A finding is published as a PR comment iff ALL conditions hold:

```
C0. claim_strength ∈ {material_concern, supported_regression}
    (observed_difference and insufficient are NEVER published —
     they are internal triage records)
C1. material_concern == true
C2. evidence is non-empty; every entry passes resolution:
    - file exists in the stated revision
    - lines are within the file's line count
    - excerpt matches the file content at the stated lines
      (deterministic substring verification by the harness)
C3. developer_comment is non-empty and ≥ 20 characters
C4. if claim_strength == supported_regression:
    counterfactual.scenario ≠ "" AND alternatives_checked ≠ []
C5. if claim_strength == material_concern:
    changed_behaviour names a specific behavioural scenario
    (not a generic risk narrative)
```

Findings that fail are retained as TRIAGE RECORDS with the failure
reason. They are not visible on the PR.

## Evidence resolution (harness-enforced, not model-controlled)

Before C0–C5, the harness verifies:

```
R1. Every evidence entry's file exists in the stated revision tree.
R2. The excerpt text is a substring of the file's content at the
    stated lines (deterministic check against revision-bound source).
R3. The finding's claim_strength does not exceed what the evidence
    supports:
    - observed_difference requires only BASE/HEAD bytes
    - material_concern requires BASE/HEAD bytes + a discriminating
      scenario (the counterfactual field)
    - supported_regression requires material_concern requirements +
      evidence that the checked object differs from the operated-on
      object (or equivalent invariant-violation evidence)
```

R1/R2 are deterministic byte checks. R3 is a structural check (does
the evidence have the right fields populated for this claim level);
it cannot independently verify semantic materiality. Human review of
proposed comments remains the qualification authority.

## Demotion rules (applied in order before C1–C5)

```
D1. supported_regression without counterfactual.scenario
    → demote to material_concern
D2. supported_regression without alternatives_checked
    → demote to material_concern
D3. material_concern without evidence
    → demote to insufficient
D4. REMOVED (R3b-2 adversarial finding: deterministic auto-promotion
    from observed_difference to material_concern is a model-controlled FP
    channel; promotion now requires the model to set material_concern=true
    AND the evidence to pass C3, which is already checked)
```

## Comment format

```
[<claim_strength_label>] <changed_behaviour>

Evidence:
- <rev> <file>:<lines>

<counterfactual if present>
Alternative explanations checked: <list>

Close-out: <recommended_action>
```

No severity levels. No merge-blocking language. No confidence scores.
The claim-strength label is the first word: `[Material concern]` or
`[Supported regression]`.

## Silence format (not published; internal triage record)

```json
{
  "investigation_status": "incomplete|complete_no_finding",
  "n_pairs_checked": 0,
  "n_gate_kills": 0,
  "n_abstentions": 0,
  "unresolved": ["missing element descriptions"]
}
```

## Non-goals

- No merge-blocking.
- No severity theatre.
- No confidence scores (model self-reports are not calibrated).
- No model-controlled publication.
- No automatic fix generation.
- No cross-PR aggregation (one finding per PR, deduped by underlying problem).
