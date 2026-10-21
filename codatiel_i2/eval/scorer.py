"""Scorer: per-detector precision/recall + CI gates (§16)."""

from __future__ import annotations

from codatiel_i2.core.findings import is_verified_proof_kind


def score(expected: list[dict], actual: list[dict]) -> dict:
    exp_ids = {e.get("finding_id") or e.get("invariant") for e in expected}
    act_ids = {a.get("finding_id") or a.get("invariant") for a in actual}
    tp = len(exp_ids & act_ids)
    fp = len(act_ids - exp_ids)
    fn = len(exp_ids - act_ids)
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall}


def ci_gates(findings: list[dict], anchors_valid: bool = True,
             evidence_ids_known: set[str] | None = None,
             anchor_errors: list[str] | None = None,
             trust_context: dict | None = None) -> dict:
    """Syntactic CI gates. NOT the trust gate: pass trust_context
    (evidence_by_id, verification_by_candidate, base/head checkouts,
    changed_paths, expected SHAs) to also run the central artifact-backed
    validator per finding; without it, trust_errors is reported as unknown.
    """
    invalid_anchors = sum(1 for f in findings if not f.get("changed_anchor", {}).get("path"))
    if anchor_errors:
        invalid_anchors += len(anchor_errors)
    if not anchors_valid:
        # Caller-asserted anchor invalidity fails every publishable finding.
        invalid_anchors += sum(1 for f in findings if f.get("publish_eligible"))
    missing_evidence = sum(1 for f in findings if f.get("publish_eligible") and not f.get("evidence_ids"))
    unknown_evidence = 0
    if evidence_ids_known is not None:
        for f in findings:
            if f.get("publish_eligible"):
                unknown_evidence += sum(1 for eid in (f.get("evidence_ids") or []) if eid not in evidence_ids_known)
    missing_trigger = sum(1 for f in findings if f.get("publish_eligible") and not (f.get("trigger") and f.get("bad_outcome")))
    unverified_publishable = sum(
        1 for f in findings
        if f.get("publish_eligible") and (
            (f.get("proof") or {}).get("level") != "verified"
            or not is_verified_proof_kind((f.get("proof") or {}).get("kind"))
            or (f.get("verification") or {}).get("status") != "survives"
        )
    )
    trust_errors: dict[str, int] = {}
    trust_unknown = trust_context is None
    if trust_context is not None:
        from codatiel_i2.verify.trust import validate_verified_finding

        for f in findings:
            if not f.get("publish_eligible"):
                continue
            errs = validate_verified_finding(f, **trust_context)
            if errs:
                trust_errors[str(f.get("finding_id") or "finding")] = len(errs)
    gates = {"invalid_anchor_count": invalid_anchors,
             "missing_evidence_on_publishable": missing_evidence,
             "unknown_evidence_on_publishable": unknown_evidence,
             "missing_trigger_or_outcome": missing_trigger,
             "unverified_publishable": unverified_publishable,
             "trust_errors": trust_errors,
             "trust_unknown": trust_unknown}
    gates["pass"] = (invalid_anchors == 0 and missing_evidence == 0 and unknown_evidence == 0
                     and missing_trigger == 0 and unverified_publishable == 0
                     and not trust_errors and not trust_unknown)
    return gates
