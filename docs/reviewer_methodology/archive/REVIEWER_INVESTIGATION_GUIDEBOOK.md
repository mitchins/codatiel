# REVIEWER_INVESTIGATION_GUIDEBOOK.md

## Codatiel Reviewer — Investigation Guidebook

Version: 1.0-draft. Companion to `REVIEWER_RULES_OF_ENGAGEMENT.md`.

This guidebook teaches investigation techniques. It is instructional,
not a mandatory checklist. Choose the technique that fits the evidence.

---

## 1. Orient

Start from the changed code and its immediate responsibilities.

- Read the changed hunks. What function, class, or module changed?
- Read enough surrounding code to understand the unit's purpose.
- Check whether the change adds, removes, moves, or modifies a decision
  point (a guard, a permission check, a role assignment, a scope
  restriction, a data filter, a validation, a contract clause, a
  dependency pin).
- Look for non-code changes that carry authority: configuration, policy
  documents, dependency manifests, API contracts.

**Stopping condition**: you can state what the change does in one
sentence without speculating about unseen code.

## 2. Identify the material behavioural difference

Compare BASE and HEAD under equivalent inputs.

Techniques (choose what fits):

- **Set-membership change**: derive the added/removed values. Evaluate
  the controlling predicate with a value that crosses the boundary.
  (FJ02: `ctx.role='member'` against `OWNER_ONLY` expansion.)
- **Checked-vs-used divergence**: follow the identity of the checked
  object and the operated-on object. If they diverge, the check may not
  govern the effect. (051/broken: checked `rec`, dropped `target`.)
- **Ordering inversion**: trace when the effect occurs relative to
  enforcement. Guard-after-sink means the effect precedes the check.
  (036/broken: `drop_rec` before `require_owner`.)
- **Delegation or wrapper**: follow the delegation to the actual
  enforcement. If the delegate enforces the same constraint, the
  relocation is benign. (003/safe: `routeGuards → OwnerGuard →
  RequireOwner`.)
- **Constant/policy change**: evaluate the predicate with values from
  both the old and new policy. Determine who gains and who loses access.
  (005/broken: OWNER_ONLY expansion.)
- **Transactional staging**: determine whether the effect is observable
  outside the transaction. Staged-but-rolled-back is not committed.
  (033/034-safe: `tx.staged.push` is inert without `tx.commit`.)
- **Dependency/config change**: read the old and new values, determine
  what behavioural difference the new value produces, check for
  migration or compatibility notes.

**Stopping condition**: you can state the specific input or condition
under which BASE and HEAD produce different observable outcomes, or you
can state that no such input exists within the visible code.

## 3. Find related implementations and callers

Look for code that shares the changed responsibility:

- Callers of the changed function: do they depend on the old behaviour?
- Alternative implementations: does another path already handle the
  concern?
- Policy/contract documents: do they authorize or prohibit the new
  behaviour?
- Consumers: does downstream code assume the old contract?

Use the available tools (find-references, find-definitions, grep) to
build a minimal relationship map. Do not search exhaustively — stop
when you have either confirmed or excluded the concern.

## 4. Formulate a discriminating counterfactual

From the material difference, construct a concrete scenario:

```
Given: [specific input/state/principal]
BASE:  [what happens — cite file:line]
HEAD:  [what happens — cite file:line]
Difference: [what changed in outcome]
```

Example (set-widening):
```
Given: ctx.role='member', perm='mutate'
BASE:  check → 'member' not in {'owner'} → raise Denied
HEAD:  check → 'member' in {'owner','member',...} → return True
Difference: 'member' can now mutate
```

Example (checked-vs-used):
```
Given: authenticated user owns record A; record B exists in same tenant
BASE:  require_owner(rec); drop(rec)         ← checked and dropped: same
HEAD:  require_owner(rec); drop(Resolve(request_body.id))
                                                 ← checked rec, dropped target
Difference: owner of A can drop B
```

If no discriminating input exists within the visible code, the
difference may be purely structural. Record it as an observation, not
a concern.

## 5. Evaluate BASE versus HEAD

Apply the counterfactual to both revisions:

1. Trace the code path on BASE. What is the outcome?
2. Trace the same path on HEAD. What is the outcome?
3. Is the difference in the outcome, or only in the implementation?

If the outcome is the same, the change is a refactor or an equivalent
enforcement relocation. Record and move on.

If the outcome differs, evaluate whether the difference is material
(would a developer want to know?) and whether any alternative
explanation resolves it.

## 6. Test alternative explanations

Before concluding a regression:

- **Moved enforcement**: is the check now elsewhere on the same path?
- **Delegated enforcement**: does a callee or wrapper perform the check?
- **Intentional change**: do tests, comments, or policy documents
  indicate this is deliberate?
- **Compensating control**: does another mechanism prevent the alleged
  harm?
- **Dead code**: is the changed path actually reachable?
- **Naming confusion**: are you looking at the right function?

Actively search for the benign explanation. If you find one that
resolves the concern, the finding is reduced or eliminated.

## 7. Identify the actual impact

What operation, data, or guarantee is affected?

- Is it a protection boundary (auth, access control, input validation)?
- Is it a data contract (schema, API shape, serialization)?
- Is it a reliability guarantee (error handling, fallback, cleanup)?
- Is it a resource constraint (memory, connection, rate limit)?
- Is it a correctness issue (wrong result, lost update, race)?

If the impact is not identifiable from the visible code, record the
observation as incomplete.

## 8. Recognize diminishing value

Stop investigating when:

- The material difference is established and no alternative explanation
  resolves it.
- Further investigation would not change the conclusion or its
  communication.
- The remaining uncertainty is about policy or intent, not about
  code behaviour — and the finding is proportionate to that
  uncertainty.

Do not continue searching for confirming evidence after the concern is
established. Instead, actively search for the disconfirming evidence
(the strongest benign explanation).

## 9. Choose the output

| finding state | output |
|---|---|
| Concrete difference + material impact + no benign resolution | **Material concern** (advisory) |
| Concrete difference + plausible impact + some uncertainty | **Material concern** (qualified advisory) |
| Concrete difference + no identifiable impact | **Observed difference** (informational, not a public comment) |
| No concrete difference (refactor/rename) | **Silence** (log if useful for triage) |
| Difference exists but decisive evidence is missing | **Insufficient / no comment** (record the missing element) |
| Concern is real but depends on unknown policy/intent | **Unresolved question** (triage record, not a public comment) |

---

## Counterfactual examples

### Predicate changes but outcome does not

```
Change: `if role in OWNER_ONLY: allow` → `if role not in EXCLUDED: allow`
Where:  OWNER_ONLY = {owner}, EXCLUDED = {viewer, guest}
Result: for owner → allow (both); for editor → not in OWNER_ONLY = deny,
        not in EXCLUDED = allow (both). Outcome unchanged.
Conclusion: predicate changed but effective behaviour did not.
```

### Newly admitted role authorized by policy

```
Change: OWNER_ONLY {owner} → {owner, editor}
Policy: "editors may mutate published drafts"
Result: editor mutating a published draft is authorized by policy.
Conclusion: newly admitted role is authorized → boundary change, not
            regression.
```

### Changed guard is redundant with existing enforcement

```
Change: require_owner(ctx, rec) removed from handle_update
        but put_query applies `.where(owner_id=ctx.user_id)`
Result: the store-level filter enforces the same ownership constraint.
Conclusion: guard removed but enforcement remains → consistent.
```

### Checked object differs from used object

```
Change: require_owner(rec) + drop(target)
        where target = Resolve(request_body.id), rec ≠ target
Result: ownership of rec does not imply ownership of target.
Conclusion: checked-vs-used divergence → potential regression.
```

### Sanitized capability differs from full exposure

```
Change: previously denied (403), now returns a subset of fields
Result: the caller gains access to Id/TeamId/Type/DisplayName but not
        to Name/Header/Purpose/CreatorId. The capability is new but
        restricted.
Conclusion: boundary change (new capability, restricted exposure);
            whether this is a regression depends on policy that is
            not in evidence.
```

### No useful discriminating input available

```
Change: algorithm rewritten from bubble sort to quicksort
Result: both produce the same sorted output for the same input.
Conclusion: implementation detail change, no authorization or
            behavioural difference → silence.
```
