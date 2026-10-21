# Codatiel Iteration 2 — product and measurement contract (frozen)

Source: `iteration-2-instructions.md` (Iteration 1 repo, HEAD `03d2ddf`).

## Product boundary

Five detectors only: contracts, authorization, docs_code,
operating_constraints, semantic_twins. Everything in §0 out-of-scope list
stays out.

## Pipeline (§2)

PR diff → classification → detector candidates → index/breadth retrieval →
exact evidence → specialist verification → witness → typed validation →
dashboard → publish gate. No general reviewer in the critical path.

## Finding schema (§8)

`finding_id, detector, subclass, changed_anchor{path,line_start,line_end},
counterpart_anchors, invariant, base_state, head_state, trigger, bad_outcome,
evidence_ids, proof{level,kind}, verification{status}, confidence,
publish_eligible`. No trigger + bad outcome = no publishable finding.

## Proof levels (§9)

candidate (dashboard only) → corroborated (dashboard; publishable only after
proven precision) → verified (registered proof validator replays the
finding's own proof contract; eligible for publication).

No proof kinds validate yet (VERIFIED_KINDS is empty until Detector 1
ships `docs_code.*.v1` proof validators). Evidence proves source material
is real; a proof validator proves the finding follows from it.

## Coverage (§15)

Detector-specific only. Never a generic clean verdict.

## Measurement (§16)

Every detector ships positive mutations + negative controls + historical
replay + frozen holdout. CI gates always: invalid anchors = 0, missing
evidence on publishable = 0, missing trigger/bad outcome = 0.
Dashboard → publish requires frozen-holdout precision + mutation recall with
no baseline regression.

## Definition of done (§25)

Five independent measurable detectors; first three publish-quality;
index improves recall/cost; breadth usage justified by measurement; never
claims unsupported categories; historic-PR overlap; seeded suite green;
ablation measurable; dashboard answers what/checked/proof/gaps/accuracy.
