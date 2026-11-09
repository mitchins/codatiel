# Stage C — final arbitration (product prompt)

This code has just been added or changed. Decide whether it
independently recreates an existing maintainable responsibility.

You receive: the target source, compact evidence cards from
per-candidate investigations, and exact source ONLY for the
strongest finalists. Cards are attention maps, not evidence — verify
every family member against finalist source before including it.
Discarded candidates' source is intentionally absent.

Apply the boundary rules:
- Callers, wrappers, formatters, loggers, delegating callers are
  uses, not independent implementations. Never consolidate a use
  into its implementation. A card role of wrapper/delegator is a
  stop sign for equivalence.
- Generated artifacts are never consolidation targets.
- Sub-step overlap is partial at most; composing an already-delegated
  helper is not a finding.
- Prefer reuse of an existing appropriate implementation over inventing
  a new shared helper.
- Arithmetic or policy drift on any input is divergence: unresolved
  drift belongs in investigate with the concrete difference stated.
- Relationship and boundary are separate: strong relationship never
  implies whole-unit reuse. shared_part needs a concrete inner
  boundary.
- Every family member justified independently from its own source.

Decide: family, relationship, smallest valid shared boundary, action
(reuse_existing | extract_shared_part | share_contract | investigate |
keep_separate), and unresolved ambiguity.
Actionable verdicts require: non-empty family of path::symbol ids,
a concrete boundary, no unresolved material behavioral mismatch.
Otherwise investigate (likely duplication, unresolved boundary) or
keep_separate (independent maintenance appropriate).

Reply with exactly one JSON object, no prose, with EXACTLY these keys
(no renames, no extras):
{"target_responsibility": str, "inspected": [str],
 "related_implementations": [{"implementation_id": str,
   "relationship": "equivalent|shared_part|diverged|related_only|unrelated|unknown", "why": str}],
 "best_existing_implementation": str|null, "recommended_family": [str],
 "action": "reuse_existing|extract_shared_part|share_contract|keep_separate|investigate",
 "proposed_boundary": str|null, "must_remain_separate": [str],
 "uncertainty": "low|medium|high"}
Rules: keep_separate/investigate requires recommended_family=[] and
proposed_boundary=null; actionable requires non-empty
recommended_family and non-null proposed_boundary.

Card interpretation: cards are independent hypotheses, not votes.
Three weak negative cards do not numerically outweigh one strongly
source-backed positive card — weigh evidence quality (spans read,
confidence, decisive differences), not card count. An
equivalent/high-confidence span-backed card is a strong actionability
hypothesis: keep_separate over it requires naming the source-backed
refutation in must_remain_separate; if you cannot refute it from
finalist source but cannot verify the boundary either, choose
investigate with the concrete uncertainty stated.
