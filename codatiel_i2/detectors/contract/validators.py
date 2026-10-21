"""Contract mechanical validators V1/V2/V3 + proof replay (C3.3/C3.6).

Validators consume replayed facts only. They never inspect family IDs,
labels, counterfactual prose, or fixture names; they use no LLM and no
embedding similarity. Retrieval proposes the evidence pair (carried in
proof_inputs); facts + validators prove the contradiction. Subject-file
binding defeats spliced cross-contract facts.

Each validator proves the full counterfactual itself: BASE compatible AND
HEAD contradictory, from base+head facts. Detail/base_state/head_state are
recomputed on every replay, so the central trust comparison is honest.
"""

from __future__ import annotations

from codatiel_i2.common.jsonio import canonical_json

VALIDATOR_VERSION = "v1"

TRIGGERS = {
    "contract.required_name_missing.v1": "one contract side changed",
    "contract.required_parameter_missing.v1": "callable signature changed",
    "contract.removed_column_still_required.v1": "migration removed a column",
}
OUTCOMES = {
    "contract.required_name_missing.v1": "required name unavailable to counterpart",
    "contract.required_parameter_missing.v1": "required argument unsatisfied at call site",
    "contract.removed_column_still_required.v1": "removed column still read at runtime",
}

V1 = "contract.required_name_missing.v1"
V2 = "contract.required_parameter_missing.v1"
V3 = "contract.removed_column_still_required.v1"

REQUIRED_ROLES = {
    V1: ("provided", "required", "provided_base", "required_base"),
    V2: ("signature", "callsite", "signature_base", "callsite_base"),
    V3: ("migration", "requirement", "migration_base", "requirement_base"),
}


def _pair(record: dict) -> tuple[str, str]:
    extra = record.get("proof_inputs") or {}
    return extra.get("producer_path", ""), extra.get("consumer_path", "")


def _names(fact: dict) -> list[str]:
    return sorted(fact.get("names", []))


def _rev_ok(fact: dict, want: str) -> bool:
    return fact.get("revision") == want


def validate_required_name_missing(provided: dict, required: dict,
                                   provided_base: dict, required_base: dict,
                                   producer_path: str,
                                   consumer_path: str) -> dict:
    base = {"validator": V1, "version": VALIDATOR_VERSION}
    quads = ((provided, "provided_names"), (required, "required_names"),
             (provided_base, "provided_names"),
             (required_base, "required_names"))
    if any(f.get("fact_kind") != k for f, k in quads):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "reason": "wrong fact kinds"}
    ns = provided.get("namespace")
    if not ns or any(f.get("namespace") != ns
                     for f in (required, provided_base, required_base)):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "namespace mismatch: not one contract"}
    sides = ((provided, producer_path), (required, consumer_path),
             (provided_base, producer_path), (required_base, consumer_path))
    if any(f["subject"]["file"] != p for f, p in sides):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "fact subjects do not match the evidence pair"}
    revs = ((provided, "head"), (required, "head"),
            (provided_base, "base"), (required_base, "base"))
    if any(not _rev_ok(f, r) for f, r in revs):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "base/head revision binding violated"}
    missing = sorted(set(_names(required)) - set(_names(provided)))
    base_missing = sorted(set(_names(required_base))
                          - set(_names(provided_base)))
    detail = (f"{V1}|{producer_path}|{consumer_path}|{ns}|"
              f"missing={','.join(missing)}")
    base_state = (f"base compatible: requires "
                  f"{','.join(_names(required_base)) or '{}'} against provides "
                  f"{','.join(_names(provided_base)) or '{}'}")
    head_state = (f"head contradictory: requires "
                  f"{','.join(_names(required)) or '{}'} against provides "
                  f"{','.join(_names(provided)) or '{}'}; "
                  f"missing {','.join(missing) or '{}'}")
    if base_missing:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "detail": detail,
                "base_state": base_state, "head_state": head_state,
                "reason": "BASE already incompatible: not a regression"}
    if not missing:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "detail": detail,
                "base_state": base_state, "head_state": head_state,
                "reason": "required ⊆ provided: compatible"}
    return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": True, "missing": missing, "namespace": ns,
            "detail": detail, "base_state": base_state,
            "head_state": head_state}


def _unsatisfied(signature: dict, callsite: dict) -> list[str]:
    required = signature.get("required", [])
    return [r for i, r in enumerate(required)
            if i >= callsite.get("positional", 0)
            and r not in (callsite.get("keywords") or [])]


def validate_required_parameter_missing(signature: dict, callsite: dict,
                                        signature_base: dict,
                                        callsite_base: dict,
                                        producer_path: str,
                                        consumer_path: str) -> dict:
    base = {"validator": V2, "version": VALIDATOR_VERSION}
    quads = ((signature, "callable_signature"), (callsite, "callsite_args"),
             (signature_base, "callable_signature"),
             (callsite_base, "callsite_args"))
    if any(f.get("fact_kind") != k for f, k in quads):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "reason": "wrong fact kinds"}
    sides = ((signature, producer_path), (callsite, consumer_path),
             (signature_base, producer_path), (callsite_base, consumer_path))
    if any(f["subject"]["file"] != p for f, p in sides):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "fact subjects do not match the evidence pair"}
    revs = ((signature, "head"), (callsite, "head"),
            (signature_base, "base"), (callsite_base, "base"))
    if any(not _rev_ok(f, r) for f, r in revs):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "base/head revision binding violated"}
    pairs = ((signature, callsite), (signature_base, callsite_base))
    if any(c.get("callee") != s["subject"]["symbol"] for s, c in pairs):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "callsite callee does not resolve to the signature"}
    missing = _unsatisfied(signature, callsite)
    base_missing = _unsatisfied(signature_base, callsite_base)
    detail = (f"{V2}|{producer_path}|{consumer_path}|"
              f"{signature['subject']['symbol']}|missing={','.join(missing)}")
    base_state = f"base compatible: call satisfies {callsite_base.get('callee')}"
    head_state = (f"head contradictory: call to {callsite.get('callee')} "
                  f"leaves required {','.join(missing) or '{}'} unsatisfied")
    if base_missing:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "detail": detail,
                "base_state": base_state, "head_state": head_state,
                "reason": "BASE already incompatible: not a regression"}
    if not missing:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "detail": detail,
                "base_state": base_state, "head_state": head_state,
                "reason": "all required parameters satisfied"}
    return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": True, "missing": missing, "detail": detail,
            "base_state": base_state, "head_state": head_state}


def validate_removed_column_still_required(migration: dict, requirement: dict,
                                           migration_base: dict,
                                           requirement_base: dict,
                                           migration_path: str,
                                           consumer_path: str) -> dict:
    base = {"validator": V3, "version": VALIDATOR_VERSION}
    if migration.get("fact_kind") != "migration_effect" or \
            requirement.get("fact_kind") != "required_names":
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "reason": "wrong fact kinds"}
    if requirement.get("namespace") != "schema_column":
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "requirement is not a schema-column read"}
    if migration["subject"]["file"] != migration_path or \
            requirement["subject"]["file"] != consumer_path:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "fact subjects do not match the evidence pair"}
    revs = ((migration, "head"), (requirement, "head"))
    if any(not _rev_ok(f, r) for f, r in revs):
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": "base/head revision binding violated"}
    base_removed = set((migration_base or {}).get("removed_columns", []))
    base_names = set(_names(requirement_base)) if requirement_base else set()
    hit = sorted(set(migration.get("removed_columns", []))
                 & set(_names(requirement)))
    base_hit = sorted(base_removed & base_names)
    detail = (f"{V3}|{migration_path}|{consumer_path}|"
              f"still_read={','.join(hit)}")
    base_state = (f"base compatible: removed "
                  f"{','.join(sorted(base_removed)) or '{}'} vs reads "
                  f"{','.join(sorted(base_names)) or '{}'}")
    head_state = (f"head contradictory: removed "
                  f"{','.join(sorted(set(migration.get('removed_columns', [])))) or '{}'} "
                  f"still read {','.join(hit) or '{}'}")
    if base_hit:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "detail": detail,
                "base_state": base_state, "head_state": head_state,
                "reason": "BASE already incompatible: not a regression"}
    if not hit:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "detail": detail,
                "base_state": base_state, "head_state": head_state,
                "reason": "no removed column is still read"}
    return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": True, "missing": hit, "detail": detail,
            "base_state": base_state, "head_state": head_state}


PROOF_VALIDATORS = {
    V1: validate_required_name_missing,
    V2: validate_required_parameter_missing,
    V3: validate_removed_column_still_required,
}


def replay_proof(record: dict, fact_by_id: dict, *,
                 base_checkout=None, head_checkout=None) -> dict:
    """Re-run the registered validator from ledger facts."""
    validator_id = record.get("validator_id", "")
    fn = PROOF_VALIDATORS.get(validator_id)
    base = {"validator": validator_id,
            "version": record.get("validator_version", "")}
    if fn is None:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False, "reason": "validator not registered"}
    inputs = record.get("input_fact_ids") or {}
    required = REQUIRED_ROLES.get(validator_id, ())
    missing = [role for role in required if role not in inputs]
    if missing:
        return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                "reason": f"proof record missing roles: {sorted(missing)}"}
    snap = record.get("facts") or {}
    for role in required:
        fid = inputs[role]
        fact = fact_by_id.get(fid)
        if fact is None:
            return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                    "reason": f"input fact {role} does not resolve"}
        if canonical_json({k: v for k, v in fact.items()}) != snap.get(role):
            return {**base, "trigger": TRIGGERS[base["validator"]], "bad_outcome": OUTCOMES[base["validator"]], "survives": False,
                    "reason": f"input fact {role} snapshot mismatch"}
    facts = {role: fact_by_id[fid] for role, fid in inputs.items()}
    producer_path, consumer_path = _pair(record)
    if validator_id == V1:
        return fn(facts["provided"], facts["required"],
                  facts["provided_base"], facts["required_base"],
                  producer_path, consumer_path)
    if validator_id == V2:
        return fn(facts["signature"], facts["callsite"],
                  facts["signature_base"], facts["callsite_base"],
                  producer_path, consumer_path)
    return fn(facts["migration"], facts["requirement"],
              facts.get("migration_base") or {}, facts.get("requirement_base")
              or {}, producer_path, consumer_path)
