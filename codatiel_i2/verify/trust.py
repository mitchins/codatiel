"""Central trust validator (Phase A.5): one authoritative gate.

Every publish path — publish_preview, GitHub publish gate, evaluation
mechanical gates — must call validate_verified_finding(). No independent
approximations. Schema validation alone is not trust: this validator proves
each citation resolves to real bytes in the exact revision checkout.
"""

from __future__ import annotations

from pathlib import Path

from codatiel_i2.core.evidence import checkout_holds_sha, replay_source_evidence, validate_envelope
from codatiel_i2.core.findings import is_verified_proof_kind, validate_finding
from codatiel_i2.verify.anchor import validate_anchor


def validate_verified_finding(
    finding: dict,
    *,
    evidence_by_id: dict | None,
    verification_by_candidate: dict | None = None,
    proof_by_candidate: dict | None = None,
    fact_by_id: dict | None = None,
    base_checkout: Path | str | None = None,
    head_checkout: Path | str | None = None,
    changed_paths: set[str] | None = None,
    expected_base_sha: str | None = None,
    expected_head_sha: str | None = None,
) -> list[str]:
    errors: list[str] = []
    errors.extend(validate_finding({**finding, "publish_eligible": True}))
    if head_checkout is None or base_checkout is None:
        errors.append("trust validation requires base and head checkouts")
        return errors
    if changed_paths is None:
        errors.append("trust validation requires the changed path set")
        return errors
    if evidence_by_id is None:
        errors.append("trust validation requires the evidence ledger")
        return errors
    if verification_by_candidate is None:
        errors.append("trust validation requires the verification ledger (no silent survives)")
        return errors
    if not expected_base_sha or not expected_head_sha:
        errors.append("trust validation requires the run's expected base/head SHAs")
        return errors

    head_checkout = Path(head_checkout)
    base_checkout = Path(base_checkout)

    anchor = finding.get("changed_anchor") or {}
    errors.extend(f"changed_anchor: {e}" for e in validate_anchor(head_checkout, anchor, changed_paths))

    for eid in finding.get("evidence_ids") or []:
        ev = evidence_by_id.get(eid)
        if ev is None:
            errors.append(f"evidence_id {eid!r} does not resolve in this run's ledger")
            continue
        # NOTE for future proof validators: evidence_ids are provenance only.
        # The finding's semantic claims (invariant/base_state/head_state) are
        # NEVER compared to bytes here — the per-detector proof validator
        # (e.g. docs_code.literal_value_mismatch.v1) owns that proof.
        if ev.get("revision") in ("base", "head", "diff"):
            if ev.get("base_sha") != expected_base_sha or ev.get("head_sha") != expected_head_sha:
                errors.append(f"evidence {eid}: belongs to a different run (SHA binding mismatch)")
                continue
        elif not ev.get("revision_sha"):
            errors.append(f"evidence {eid}: external/generated evidence needs a revision_sha binding")
            continue
        errors.extend(f"evidence {eid}: {e}" for e in validate_envelope(ev))
        errors.extend(
            f"evidence {eid}: {e}"
            for e in replay_source_evidence(
                ev, base_checkout=base_checkout, head_checkout=head_checkout,
                changed_paths=changed_paths,
            )
        )

    for i, counterpart in enumerate(finding.get("counterpart_anchors") or []):
        if not isinstance(counterpart, dict):
            errors.append(f"counterpart_anchors[{i}] is not an object")
            continue
        if counterpart.get("revision") not in ("base", "head"):
            errors.append(f"counterpart_anchors[{i}]: explicit revision 'base'|'head' required")
            continue
        revision = counterpart["revision"]
        checkout = base_checkout if revision == "base" else head_checkout
        if not checkout_holds_sha(checkout, counterpart.get("revision_sha") or ""):
            errors.append(f"counterpart_anchors[{i}]: checkout does not hold its revision_sha")
            continue
        errors.extend(
            f"counterpart_anchors[{i}]: {e}" for e in validate_anchor(checkout, counterpart, None)
        )

    proof = finding.get("proof") or {}
    if not is_verified_proof_kind(proof.get("kind")):
        # Schema validation already reports this; don't double-count it.
        if not any("no registered mechanical validator" in e for e in errors):
            errors.append(
                f"proof.kind {proof.get('kind')!r} has no registered mechanical validator"
            )
    else:
        if not str(proof.get("detail") or "").strip():
            errors.append("verified proof requires a non-blank detail pointer")
        artifact_ids = proof.get("artifact_evidence_ids") or []
        if not artifact_ids:
            errors.append("verified proof requires artifact evidence (no bare assertions)")
        for eid in artifact_ids:
            ev = evidence_by_id.get(eid)
            if ev is None:
                errors.append(f"proof artifact evidence {eid!r} does not resolve in this run's ledger")
                continue
            if ev.get("kind") not in ("source_excerpt", "diff_hunk", "file_state"):
                # External/witness/analyzer artifacts have no replay validator
                # yet (contract-detector phase); they cannot back verified proof.
                errors.append(
                    f"proof artifact {eid}: kind {ev.get('kind')!r} is not repository-bound "
                    "and has no artifact replay validator"
                )
                continue
            errors.extend(f"proof artifact {eid}: {e}" for e in validate_envelope(ev))
            errors.extend(
                f"proof artifact {eid}: {e}"
                for e in replay_source_evidence(
                    ev, base_checkout=base_checkout, head_checkout=head_checkout,
                    changed_paths=changed_paths,
                )
            )

    candidate_id = finding.get("candidate_id", "")
    record = verification_by_candidate.get(candidate_id) if candidate_id else None
    if record is None:
        errors.append("no verification record for this finding's candidate in this run's ledger")
    elif record.get("status") != "survives":
        errors.append(f"verification record status is {record.get('status')!r}, need 'survives'")
    # A survives ledger entry is NEVER proof authority by itself: registered
    # proof kinds must replay through their detector validator, and the
    # replayed verdict must match the finding. Refuted replay rejects even
    # when the ledger says survives.
    proof = finding.get("proof") or {}
    if is_verified_proof_kind(proof.get("kind")):
        errors.extend(_replay_registered_proof(
            finding, proof_by_candidate, fact_by_id, evidence_by_id,
            base_checkout, head_checkout))
    return errors


def _replay_registered_proof(finding: dict, proof_by_candidate: dict | None,
                             fact_by_id: dict | None, evidence_by_id: dict | None,
                             base_checkout: Path, head_checkout: Path) -> list[str]:
    from codatiel_i2.detectors.docs_code import facts as facts_mod
    from codatiel_i2.detectors.docs_code import validators as validators_mod

    errors: list[str] = []
    candidate_id = finding.get("candidate_id", "")
    proof = finding.get("proof") or {}
    kind = proof.get("kind", "")
    if kind.startswith("contract."):
        from codatiel_i2.detectors.contract import facts as facts_mod
        from codatiel_i2.detectors.contract import validators as validators_mod
    if proof_by_candidate is None or fact_by_id is None or evidence_by_id is None:
        return ["trust validation requires the proof, fact and evidence ledgers for registered proof kinds"]
    record = proof_by_candidate.get(candidate_id) if candidate_id else None
    if record is None:
        return ["no proof record for this finding's candidate in this run's ledger"]
    if record.get("validator_id") != kind:
        return [f"proof record validator {record.get('validator_id')!r} != finding proof kind {kind!r}"]
    # — authoritative fact replay BEFORE validator —
    # Every input fact must resolve and be reproducible from its cited bytes.
    for role, fid in (record.get("input_fact_ids") or {}).items():
        fact = fact_by_id.get(fid)
        if fact is None:
            return [f"proof replay refutes: input fact {role} ({fid}) does not resolve"]
        res = facts_mod.replay_fact(fact, evidence_by_id,
                                    base_checkout=base_checkout,
                                    head_checkout=head_checkout)
        if not res.get("ok"):
            return [f"proof replay refutes: fact {role} ({fid}) failed replay: {'; '.join(res.get('errors', []))}"]
    try:
        replayed = validators_mod.replay_proof(
            record, fact_by_id,
            base_checkout=base_checkout, head_checkout=head_checkout)
    except Exception as exc:  # fail closed on replay crashes
        return [f"proof replay crashed: {type(exc).__name__}: {exc}"]
    if not replayed.get("survives"):
        return [f"proof replay refutes: {replayed.get('reason', 'no reason')}"]
    for key in ("detail", "base_state", "head_state", "trigger", "bad_outcome"):
        if replayed.get(key) != (finding.get("proof", {}).get(key) if key == "detail"
                                  else finding.get(key)):
            return [f"replayed proof {key} does not match the finding artifact"]
    return errors
