"""GitHub publish gate (report-only in Phase A.5; closed until precision gates pass).

Full trust context (ledgers + checkouts + changed set + run SHAs) routes
through the central validator in verify/trust.py. Without it, NOTHING is
listed as publishable: syntactic-only rows are returned separately as
`candidates_preview` so no caller can mistake them for a publish list.
"""

from __future__ import annotations

from codatiel_i2.core.findings import validate_finding
from codatiel_i2.verify.trust import validate_verified_finding

PUBLISH_CAP_TOTAL = 5


def publish_gate(findings: list[dict], *, precision_gate_passed: bool = False,
                 evidence_by_id: dict | None = None,
                 verification_by_candidate: dict | None = None,
                 base_checkout=None, head_checkout=None,
                 changed_paths: set[str] | None = None,
                 expected_base_sha: str | None = None,
                 expected_head_sha: str | None = None) -> dict:
    if not precision_gate_passed:
        return {"mode": "report_only", "publishable": [], "reason": "precision gates not yet passed"}
    full_context = (evidence_by_id is not None and base_checkout is not None
                    and head_checkout is not None and changed_paths is not None
                    and expected_base_sha and expected_head_sha)
    if not full_context:
        # Fail closed: without artifact context there is no publish list.
        syntactic = [f for f in findings if f.get("publish_eligible") and not validate_finding(f)]
        return {"mode": "report_only", "publishable": [],
                "syntactic_candidates": syntactic[:PUBLISH_CAP_TOTAL],
                "validation": "incomplete_context",
                "reason": "trust context missing; nothing is publishable"}
    eligible = []
    context_ok = True
    for f in findings:
        if not f.get("publish_eligible"):
            continue
        errors = validate_verified_finding(
            f, evidence_by_id=evidence_by_id,
            verification_by_candidate=verification_by_candidate,
            base_checkout=base_checkout, head_checkout=head_checkout,
            changed_paths=changed_paths,
            expected_base_sha=expected_base_sha, expected_head_sha=expected_head_sha)
        if any(e.startswith("trust validation requires") for e in errors):
            context_ok = False
        if not errors:
            eligible.append(f)
    return {"mode": "report_only", "publishable": eligible[:PUBLISH_CAP_TOTAL],
            "truncated": len(eligible) > PUBLISH_CAP_TOTAL,
            "validation": "artifact_backed" if context_ok else "incomplete_context",
            "reason": "Phase A.5: preview only, no GitHub mutation"}
