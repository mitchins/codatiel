"""C4.7/C4.10 semantic provenance: model_adjudicated trust (not verified).

A semantic finding record binds: model profile, prompt hash, input
evidence/fact IDs, model call ledger IDs, raw output digests, parsed
decisions, repetition results, majority rule, challenge result if used.

validate_provenance() rejects: altered decision/reason, swapped
evidence/facts, changed prompt hash, fabricated call ID, post-hoc
majority edit, forged challenge. It proves faithful derivation from the
recorded model execution, NOT semantic truth. Findings carry status
"model_adjudicated", never "verified".
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

STATUS = "model_adjudicated"


def canonical(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True)


def build_record(*, model_profile: str, model_id: str, prompt_version: str,
                 prompt_hash: str, evidence_ids: list[str],
                 fact_ids: list[str], call_ids: list[str],
                 raw_digests: list[str], decisions: list[str | None],
                 majority_decision: str | None, majority_rule: str,
                 fact_payloads: dict | None = None,
                 voting_decisions: list | None = None,
                 excluded_protocol: int = 0,
                 call_statuses: list | None = None,
                 challenge: dict | None = None) -> dict:
    rec = {
        "status": STATUS,
        "model_profile": model_profile, "model_id": model_id,
        "prompt_version": prompt_version, "prompt_hash": prompt_hash,
        "evidence_ids": sorted(evidence_ids),
        "fact_ids": sorted(fact_ids),
        "call_ids": list(call_ids),
        "raw_digests": list(raw_digests),
        "decisions": list(decisions),
        "majority_decision": majority_decision,
        "majority_rule": majority_rule,
        "voting_decisions": list(voting_decisions or []),
        "excluded_protocol": excluded_protocol,
        "call_statuses": list(call_statuses or []),
        "challenge": challenge,
    }
    if fact_payloads is not None:
        rec["fact_digests"] = {
            fid: hashlib.sha256(canonical(fact_payloads[fid]).encode()
                                ).hexdigest()[:16]
            for fid in rec["fact_ids"] if fid in fact_payloads}
    rec["record_digest"] = hashlib.sha256(
        canonical({k: v for k, v in rec.items()
                   if k != "record_digest"}).encode()).hexdigest()[:16]
    return rec


def validate_provenance(record: dict, *, ledger_calls: dict,
                        expected_prompt_hash: str,
                        evidence_by_id: dict,
                        fact_by_id: dict) -> list[str]:
    """ledger_calls: call_id -> {digest, profile}. Returns error list."""
    errors: list[str] = []
    if record.get("status") != STATUS:
        errors.append("semantic record must carry model_adjudicated status")
    if record.get("prompt_hash") != expected_prompt_hash:
        errors.append("prompt hash mismatch")
    for eid in record.get("evidence_ids", []):
        if eid not in evidence_by_id:
            errors.append(f"evidence {eid!r} does not resolve")
    for fid in record.get("fact_ids", []):
        if fid not in fact_by_id:
            errors.append(f"fact {fid!r} does not resolve")
    for fid, want in (record.get("fact_digests") or {}).items():
        got = fact_by_id.get(fid)
        if got is None:
            errors.append(f"fact {fid!r} does not resolve")
        elif hashlib.sha256(canonical(got).encode()).hexdigest()[:16] != want:
            errors.append(f"fact {fid!r} payload does not match record binding")
    seen_digests = []
    for cid in record.get("call_ids", []):
        call = ledger_calls.get(cid)
        if call is None:
            errors.append(f"model call {cid!r} not in ledger (fabricated)")
            continue
        if call.get("profile") != record.get("model_profile"):
            errors.append(f"model call {cid!r} profile mismatch")
        seen_digests.append(call.get("digest"))
    if seen_digests != list(record.get("raw_digests", [])):
        errors.append("raw output digests do not match ledger calls")
    votes = [d for d in record.get("decisions", []) if d]
    counts = Counter(votes)
    top = max(counts.values()) if counts else 0
    winners = [v for v, c in counts.items() if c == top]
    rule = record.get("majority_rule")
    if rule == "validity_gated_plurality_tie_uncertain":
        expect = winners[0] if len(winners) == 1 and votes else (
            None if not votes else "uncertain")
    elif rule == "c4r_clean_plurality_min2":
        # Campaign rule: only clean reps vote; ≥2 clean votes required;
        # sole winner wins; ties and thin votes abstain; all-infra → None.
        clean = [d for d in record.get("voting_decisions", []) if d]
        statuses = record.get("call_statuses", [])
        cc = Counter(clean)
        ctop = max(cc.values()) if cc else 0
        cw = [v for v, c in cc.items() if c == ctop]
        if not statuses and not clean:
            expect = None
        elif statuses and all((s or "").startswith("infra")
                              for s in statuses):
            expect = None
        elif len(clean) >= 2 and len(cw) == 1:
            expect = cw[0]
        else:
            expect = "insufficient_evidence"
    else:
        errors.append(f"unknown majority rule {rule!r}")
        expect = record.get("majority_decision")
    if rule not in ("validity_gated_plurality_tie_uncertain",
                    "c4r_clean_plurality_min2"):
        pass
    elif record.get("majority_decision") != expect:
        errors.append("majority decision does not follow repetition votes")
    if record.get("challenge") is not None:
        ch = record["challenge"]
        if ch.get("verdict") not in ("uphold", "refute", "uncertain"):
            errors.append("bad challenge verdict")
        if not ch.get("call_id") or ch["call_id"] not in ledger_calls:
            errors.append("challenge call not in ledger")
    want = hashlib.sha256(canonical(
        {k: v for k, v in record.items()
         if k != "record_digest"}).encode()).hexdigest()[:16]
    if record.get("record_digest") != want:
        errors.append("record digest mismatch (post-hoc edit)")
    return errors
