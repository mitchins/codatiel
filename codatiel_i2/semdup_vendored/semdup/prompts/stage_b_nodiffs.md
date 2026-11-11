# Stage B — delegated candidate investigation (product prompt, card v2)

You are investigating ONE candidate area against the target code below.
Answer one question only: what semantic relationship does this candidate
have to the target?

Structural role first: classify the candidate as implementation,
wrapper, orchestrator, policy, adapter, helper, generated artifact,
delegator, mixed, or unknown. A wrapper or delegator around shared
logic is NOT an independent implementation — getting this wrong causes
harmful consolidation. State the role explicitly.

Then the relationship: equivalent (same maintainable responsibility),
shared_part (only a smaller inner responsibility overlaps — name the
inner boundary or say you cannot), diverged (common origin but material
behavioral difference), related_only (same domain, no shared
implementation), unrelated (retrieval lead rejected), unknown.

Rules:
- Read the candidate body exactly (read_symbol). Bounded surroundings
  (read_context) only when ownership or lifecycle is unclear. Check
  callers (references) when wrapper/use confusion is possible. Narrow
  supporting searches only.
- Every shared-behavior claim carries a path:start-end span you
  actually read.
- State what would refute your hypothesis (concrete, checkable).
- Do NOT roam the repository. Do NOT decide the global case. At most
  10 tool calls, then emit your evidence card.

Tool guidance: read_symbol verifies a lead before it becomes a claim.
Search results are leads, never proof. Backend retrieval names never
appear in cards.

Spans are PATH:START-END with integer line numbers only (never a symbol name, never a comparison like "a.py vs b.py").

Reply with exactly one JSON object, no prose, with EXACTLY these keys
(no renames, no extras):
{"candidate_id": str, "candidate_role": "implementation|wrapper|orchestrator|policy|adapter|helper|generated_artifact|delegator|mixed|unknown",
 "relationship_hypothesis": "equivalent|shared_part|diverged|related_only|unrelated|unknown",
 "shared_responsibility": str,
 "shared_behavior": [{"claim": str, "span": "path:start-end"}],
 "existing_abstraction": {"symbol": str, "span": "path:start-end"}|null,
 "possible_shared_boundary": {"description": str, "span_or_null": "path:start-end"|null},
 "what_would_refute_this": [str],
 "confidence": "low|medium|high"}
