You are investigating ONE candidate area against the target code below.
What semantic relationship does this candidate have to the target?

Read the candidate body exactly (read_symbol); bounded surroundings
(read_context) only when ownership is unclear; check callers
(references) when wrapper/use confusion is possible.

Reply with exactly one JSON object, no prose:
{"candidate_id": str, "relationship": "equivalent|shared_part|diverged|related_only|unrelated|unknown",
 "shared_responsibility": str, "decisive_evidence": ["path:line-range", ...],
 "important_differences": ["...", ...], "existing_abstraction": str|null,
 "material_objection": str|null, "confidence": "low|medium|high"}

Reply with exactly one JSON object, no prose, with EXACTLY these keys:
{"candidate_id": str, "relationship": "equivalent|shared_part|diverged|related_only|unrelated|unknown",
 "shared_responsibility": str, "decisive_evidence": ["path:start-end"],
 "important_differences": [str], "existing_abstraction": str|null,
 "material_objection": str|null, "confidence": "low|medium|high"}
