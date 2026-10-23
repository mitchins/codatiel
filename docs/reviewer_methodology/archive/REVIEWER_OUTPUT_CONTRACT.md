# REVIEWER_OUTPUT_CONTRACT.md

## Codatiel Reviewer — Output Contract

Version: 1.0-draft. The output contract separates model reasoning from
deterministic publication. The model proposes; the publication policy
decides.

## Model output schema

```json
{
  "investigation_status": "complete | incomplete",
  "findings": [finding...],
  "no_finding_reason": "string (required when findings is empty)",
  "unresolved_investigations": [{"description": "...", "missing_evidence": "..."}]
}
```

## Finding schema

| field | type | required | validation |
|---|---|---|---|
| claim_strength | enum | yes | one of: observed_difference, material_concern, supported_regression, insufficient |
| changed_behaviour | string | yes | non-empty, ≤200 chars |
| evidence | list | yes for material_concern and supported_regression | each: {revision, file, lines, excerpt} |
| counterfactual | object | yes for supported_regression | {scenario, base_outcome, head_outcome} |
| alternatives_checked | list | yes for supported_regression | non-empty list of considered-and-resolved explanations |
| remaining_uncertainty | string | no | what is unknown |
| material_concern | boolean | yes | whether the developer would want to know |
| recommended_action | enum | no | confirm / fix / reconsider / none |
| developer_comment | string | conditional | required if material_concern=true |

## Publication policy (deterministic)

A finding is PUBLISHED as a PR comment iff ALL of:
1. `claim_strength` ∈ {material_concern, supported_regression}
2. `material_concern` is true
3. Evidence list is non-empty; every entry has file + lines + non-empty excerpt
4. `developer_comment` is non-empty and ≥ 20 characters
5. For `supported_regression`: counterfactual.scenario is non-empty AND alternatives_checked is non-empty

A finding that fails publication is retained as a TRIAGE RECORD (not
visible on the PR) with the failure reason logged.

## Claim ladder definitions

| level | requires | example |
|---|---|---|
| observed_difference | BASE/HEAD bytes differ on a specific span; outcome equivalence unknown. INTERNAL ONLY — never published as a PR comment. Retained as a triage record. | "The function `check` now references an expanded `OWNER_ONLY` set." |
| material_concern | observed_difference + a concrete scenario where outcomes differ + a plausible consequence the developer would want to know about | "With `ctx.role='member'`, `check` now returns True on HEAD but raised `Denied` on BASE." |
| supported_regression | material_concern + identity of the checked/used objects established + no alternative enforcement identified + counterfactual scenario stated | "`require_owner` governs `rec` but `drop` operates on `target` from `Resolve(request_body.id)`: derived outcome: owner-of-rec would be able to drop target." |
| insufficient | investigation performed but a decisive element is missing | "The `Resolve` body is not in evidence; its tenant check cannot be evaluated." |

## Anti-gaming rules

1. `claim_strength` is set by the model but VALIDATED by the publication
   policy. A `supported_regression` without counterfactual and
   alternatives is demoted to `material_concern` (not published as a
   regression).
2. `material_concern` without evidence is demoted to `insufficient`.
3. An empty `findings` array with a non-empty `no_finding_reason` is
   always valid — the reviewer is never forced to produce a finding.
4. The model cannot set `developer_comment` on a finding that fails
   `publish_eligible` — the harness strips it.
5. Model self-reported confidence is NOT used. The publication policy
   is deterministic, based on structural completeness.
