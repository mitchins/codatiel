"""Typed finding schema: every finding encodes a broken invariant."""

from __future__ import annotations

SCHEMA_VERSION = "codatiel_i2.finding.v1"

DETECTORS = ("contracts", "authorization", "docs_code", "operating_constraints", "semantic_twins")

PROOF_LEVELS = ("candidate", "corroborated", "verified")

# §14: level 5 (model-only agreement) is never sufficient alone for publication.
# A proof kind is listed here ONLY when a registered proof validator replays
# it (e.g. docs_code.literal_value_mismatch.v1 receiving structured facts +
# evidence IDs and itself proving value(A) != value(B)). The tuple is
# DELIBERATELY EMPTY until Detector 1 ships its validators: with no
# detectors, nothing may graduate to mechanically verified, and
# `deterministic_contradiction` must not act as a magic escape hatch where
# a detector asserts a contradiction while pointing at valid artifacts.
# Evidence proves the source material is real; a proof validator proves the
# finding follows from it. Those are separate responsibilities.
VERIFIED_KINDS: tuple[str, ...] = ()


def registered_proof_kinds() -> tuple[str, ...]:
    """Detector-specific proof validators (e.g. docs_code.*.v1).

    The central trust layer never infers semantic contradictions; it asks
    this registry whether a proof kind is authoritative. Registration
    happens only with a mechanical implementation (see
    detectors/docs_code/validators.py).
    """
    try:
        from codatiel_i2.detectors.docs_code import validators as _v

        names = tuple(sorted(_v.PROOF_VALIDATORS))
    except Exception:
        names = ()
    try:
        from codatiel_i2.detectors.contract import validators as _cv

        names = names + tuple(sorted(_cv.PROOF_VALIDATORS))
    except Exception:
        pass
    return names


def is_verified_proof_kind(kind: object) -> bool:
    return kind in VERIFIED_KINDS or kind in registered_proof_kinds()

REQUIRED_FIELDS = (
    "finding_id", "detector", "subclass", "changed_anchor", "counterpart_anchors",
    "invariant", "base_state", "head_state", "trigger", "bad_outcome",
    "evidence_ids", "proof", "verification", "confidence", "publish_eligible",
)


def validate_finding(row: dict) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_FIELDS:
        if key not in row:
            errors.append(f"missing field: {key}")
    if row.get("detector") not in DETECTORS:
        errors.append(f"unknown detector: {row.get('detector')!r}")
    anchor = row.get("changed_anchor") or {}
    if not anchor.get("path"):
        errors.append("changed_anchor.path is required")
    if not isinstance(anchor.get("line_start"), int) or not isinstance(anchor.get("line_end"), int):
        errors.append("changed_anchor line range must be ints")
    elif anchor["line_start"] < 1 or anchor["line_end"] < anchor["line_start"]:
        errors.append("changed_anchor has invalid line range")
    if not row.get("invariant"):
        errors.append("invariant is required")
    proof = row.get("proof") or {}
    if proof.get("level") not in PROOF_LEVELS:
        errors.append(f"proof.level must be one of {PROOF_LEVELS}")
    verification = row.get("verification") or {}
    if verification.get("status") not in ("survives", "refuted", "uncertain"):
        errors.append("verification.status must be survives|refuted|uncertain")
    if not row.get("evidence_ids"):
        errors.append("evidence_ids must be non-empty")
    for i, counterpart in enumerate(row.get("counterpart_anchors") or []):
        if not isinstance(counterpart, dict) or not counterpart.get("path"):
            errors.append(f"counterpart_anchors[{i}].path is required")
        elif not isinstance(counterpart.get("line_start"), int) or not isinstance(counterpart.get("line_end"), int):
            errors.append(f"counterpart_anchors[{i}] line range must be ints")
    if row.get("publish_eligible"):
        # CI gate: no trigger + bad outcome = no publishable finding
        if not row.get("trigger"):
            errors.append("publishable finding requires trigger")
        if not row.get("bad_outcome"):
            errors.append("publishable finding requires bad_outcome")
        if proof.get("level") != "verified":
            errors.append("publishable finding requires proof.level=verified")
        if not is_verified_proof_kind(proof.get("kind")):
            errors.append(
                f"proof.kind {proof.get('kind')!r} has no registered mechanical validator "
                "(no proof kinds validate until detector proof validators exist)"
            )
        if verification.get("status") != "survives":
            errors.append("publishable finding requires verification.status=survives")
    return errors


def is_publish_eligible(row: dict) -> bool:
    return not validate_finding({**row, "publish_eligible": True})
