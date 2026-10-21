"""Proof validator registry for docs_code.

Validators register here ONLY with a mechanical implementation:

    PROOF_VALIDATORS = {
        "docs_code.literal_value_mismatch.v1": validate_literal_value_mismatch,
    }

Each validator receives structured facts + evidence IDs and itself proves
the relationship. The central trust layer asks whether a proof kind is
authoritative; it never infers the contradiction itself.
"""

from __future__ import annotations

from typing import Any, Callable

PROOF_VALIDATORS: dict[str, Callable[..., Any]] = {}


def register_proof_validator(name: str, fn: Callable[..., Any]) -> None:
    if not callable(fn):
        raise ValueError(f"proof validator {name!r} is not callable")
    PROOF_VALIDATORS[name] = fn


def _states(proof_class: str, base_doc: dict, base_code: dict,
            head_doc: dict, head_code: dict) -> dict:
    return {
        "base_state": f"base docs={_short(base_doc)} code={_short(base_code)}",
        "head_state": f"head docs={_short(head_doc)} code={_short(head_code)}",
    }


def _short(fact: dict | None) -> str:
    if not fact:
        return "absent"
    for key in ("value", "values", "name", "interface"):
        if key in fact:
            return f"{key}={fact[key]}"
    return fact.get("fact_kind", "?")


def validate_literal_value_mismatch(doc_base: dict | None, code_base: dict | None,
                                    doc_head: dict | None, code_head: dict | None) -> dict:
    """Same subject, comparable units; base consistent, head inconsistent."""
    result = {"validator": "docs_code.literal_value_mismatch.v1", "version": "v1"}
    for label, fact in (("base_doc", doc_base), ("base_code", code_base),
                        ("head_doc", doc_head), ("head_code", code_head)):
        if not fact:
            return {**result, "survives": False, "reason": f"missing {label} fact",
                    **_states("literal_default", doc_base, code_base, doc_head, code_head)}
    subjects = {doc_base["subject"], code_base["subject"],
                doc_head["subject"], code_head["subject"]}
    if len(subjects) != 1 or "unknown" in subjects:
        return {**result, "survives": False, "reason": "subject mismatch across facts",
                **_states("literal_default", doc_base, code_base, doc_head, code_head)}
    subject = next(iter(subjects))
    if doc_base["value"] != code_base["value"]:
        return {**result, "survives": False, "reason": "baseline already inconsistent",
                **_states("literal_default", doc_base, code_base, doc_head, code_head)}
    for fact in (doc_base, code_base, doc_head, code_head):
        if fact.get("value_type") not in ("int", "float"):
            return {**result, "survives": False, "reason": "non-numeric value",
                    **_states("literal_default", doc_base, code_base, doc_head, code_head)}
    units = {f.get("unit") or "" for f in (doc_head, code_head)}
    units.discard("")
    if len(units) > 1:
        return {**result, "survives": False, "reason": "incompatible units",
                **_states("literal_default", doc_base, code_base, doc_head, code_head)}
    if doc_head["value"] == code_head["value"]:
        return {**result, "survives": False, "reason": "head consistent",
                **_states("literal_default", doc_base, code_base, doc_head, code_head)}
    unit = next(iter(units)) if units else ""
    unit_suffix = f" {unit}" if unit else ""
    return {**result, "survives": True, "reason": "value_mismatch",
            **_states("literal_default", doc_base, code_base, doc_head, code_head),
            "subject": subject, "documented_value": doc_head["value"],
            "runtime_value": code_head["value"], "unit": unit,
            "trigger": f"operator relies on the documented default of "
                       f"{doc_head['value']}{unit_suffix} for {subject}",
            "bad_outcome": f"runtime uses {code_head['value']}{unit_suffix} instead",
            "detail": f"{subject}: documented {doc_head['value']}{unit_suffix} != "
                      f"runtime {code_head['value']}{unit_suffix}",
            "artifact_fact_ids": [doc_head["fact_id"], code_head["fact_id"]]}


def validate_named_contract_missing(doc_fact: dict | None, head_names: set[str],
                                    base_names: set[str], head_aliases: set[str],
                                    direction: str = "code_changed",
                                    surface_enumerated: bool = True) -> dict:
    """Documented name absent from the enumerable head surface.

    code_changed: the name must have been provided at baseline (a removal
    regression) and no alias may preserve it.
    docs_changed: the claim itself is new; baseline provision is not
    required, but aliases still suppress.
    """
    result = {"validator": "docs_code.named_contract_missing.v1", "version": "v1"}
    if not doc_fact:
        return {**result, "survives": False, "reason": "missing documented fact"}
    name = doc_fact.get("name", "")
    from codatiel_i2.detectors.docs_code.candidates import NEGATED_CLAIM, _cli_norm

    if NEGATED_CLAIM.search(doc_fact.get("claim", "")):
        return {**result, "survives": False, "reason": "negated_claim"}
    if not surface_enumerated:
        # An empty scan proves nothing: absence needs an enumerated surface.
        return {**result, "survives": False, "reason": "ambiguous_absence"}
    norm = _cli_norm(name)
    if any(_cli_norm(n) == norm for n in head_names):
        return {**result, "survives": False, "reason": "present_contract"}
    if direction == "code_changed" and not any(_cli_norm(n) == norm for n in base_names):
        return {**result, "survives": False, "reason": "no baseline presence",
                "detail": f"{name!r} never provided; cannot prove a regression removed it"}
    if norm in {_cli_norm(a) for a in head_aliases} or any(norm in _cli_norm(a) for a in head_aliases):
        return {**result, "survives": False, "reason": "alias_preserves_contract"}
    return {**result, "survives": True, "reason": "name_absent_from_surface",
            "subject": name, "surface": doc_fact.get("surface", ""),
            "base_state": f"base provides {name!r}",
            "head_state": f"head no longer provides {name!r}",
            "trigger": f"operator configures {name!r} per the documentation",
            "bad_outcome": f"{name!r} is silently ignored",
            "detail": f"documented {name!r} absent from the enumerable head surface",
            "artifact_fact_ids": [doc_fact["fact_id"]]}


def validate_allowed_values_mismatch(doc_fact: dict | None, code_fact: dict | None) -> dict:
    from codatiel_i2.detectors.docs_code.candidates import subjects_match

    result = {"validator": "docs_code.allowed_values_mismatch.v1", "version": "v1"}
    if not doc_fact or not code_fact:
        return {**result, "survives": False, "reason": "missing fact"}
    if not doc_fact.get("claim_directed") and not subjects_match(
            doc_fact.get("subject", ""), code_fact.get("subject", ""),
            doc_fact.get("subject_parts"), code_fact.get("subject_parts"),
            doc_fact.get("subject_raw_parts"), code_fact.get("subject_raw_parts")):
        return {**result, "survives": False, "reason": "subject mismatch"}
    doc_set, code_set = set(doc_fact.get("values", [])), set(code_fact.get("values", []))
    if doc_fact.get("claim_directed"):
        # Claim-directed pairs carry no subject contract: the claim value
        # was extracted from an added doc line and paired by file. The
        # membership test below is the entire proof.
        pass
    elif not subjects_match(doc_fact.get("subject", ""), code_fact.get("subject", ""),
                             doc_fact.get("subject_parts"), code_fact.get("subject_parts"),
            doc_fact.get("subject_raw_parts"), code_fact.get("subject_raw_parts")):
        return {**result, "survives": False, "reason": "subject mismatch"}
    if doc_fact.get("claim_directed"):
        claimed = sorted(doc_set - code_set)
        if not claimed:
            return {**result, "survives": False, "reason": "compatible_values"}
    else:
        if doc_fact.get("exhaustiveness") != "exhaustive":
            return {**result, "survives": False, "reason": "non_exhaustive_documentation"}
        if doc_set == code_set:
            return {**result, "survives": False, "reason": "compatible_values"}
    missing = sorted(doc_set - code_set)
    undocumented = sorted(code_set - doc_set)
    if missing:
        detail = f"`{missing[0]}` is documented but no longer accepted"
        bad = f"configuring `{missing[0]}` fails or is silently ignored"
    else:
        detail = f"`{undocumented[0]}` is accepted but undocumented"
        bad = f"operators cannot discover `{undocumented[0]}`"
    return {**result, "survives": True, "reason": "set_mismatch",
            "subject": doc_fact["subject"],
            "documented_unsupported": missing, "implemented_undocumented": undocumented,
            "base_state": f"documented {sorted(doc_set)}",
            "head_state": f"implemented {sorted(code_set)}",
            "trigger": f"operator configures {doc_fact['subject']} per the documentation",
            "bad_outcome": bad, "detail": detail,
            "artifact_fact_ids": [doc_fact["fact_id"], code_fact["fact_id"]]}


def _interfaces_equal(doc_if: dict, code_if: dict) -> bool:
    if doc_if.get("kind") != code_if.get("kind"):
        return False
    if doc_if.get("kind") == "http_route":
        if doc_if.get("method") in (None, "*") or code_if.get("method") in (None, "*"):
            # Path-existence claim: equal iff the documented path is served.
            return doc_if.get("path") == code_if.get("path")
        return (doc_if.get("method"), doc_if.get("path")) == (code_if.get("method"), code_if.get("path"))
    from codatiel_i2.detectors.docs_code.facts import subject_key as _skey

    if _skey(doc_if.get("symbol", "")) != _skey(code_if.get("symbol", "")):
        return False
    doc_params = {p["name"]: (p.get("required"), p.get("keyword_only")) for p in doc_if.get("parameters", [])}
    code_params = {p["name"]: (p.get("required"), p.get("keyword_only")) for p in code_if.get("parameters", [])}
    if doc_params == code_params:
        return True
    # Backward-compatible evolution: same names, code only relaxes
    # (required -> optional/default), no new required params, same
    # keyword-only discipline on shared required params.
    if set(doc_params) != set(code_params):
        return False
    for name, (doc_req, doc_kw) in doc_params.items():
        code_req, code_kw = code_params[name]
        if code_req and not doc_req:
            return False
        if doc_req and code_req and bool(code_kw) != bool(doc_kw):
            return False
    return True


def validate_interface_contract_mismatch(doc_fact: dict | None, code_fact: dict | None,
                                           implemented_paths: set[str] | None = None) -> dict:
    result = {"validator": "docs_code.interface_contract_mismatch.v1", "version": "v1"}
    if not doc_fact or not code_fact:
        return {**result, "survives": False, "reason": "missing fact"}
    doc_if, code_if = doc_fact.get("interface", {}), code_fact.get("interface", {})
    if not doc_if or not code_if:
        return {**result, "survives": False, "reason": "unsupported_syntax"}
    if _interfaces_equal(doc_if, code_if):
        return {**result, "survives": False, "reason": "matched_interface"}
    if doc_if.get("kind") == "http_route" and implemented_paths is not None:
        # Set semantics already decided the outcome at the call site; a
        # leftover equal pair here is a match, anything else mismatches.
        if doc_if.get("path") in implemented_paths and doc_if.get("method") in (None, "*"):
            return {**result, "survives": False, "reason": "matched_interface"}
    if doc_if.get("kind") == "http_route":
        if doc_if.get("method") in (None, "*"):
            detail = (f"documented path {doc_if.get('path')} is not served "
                      f"(implemented paths differ)")
        else:
            detail = (f"documented {doc_if.get('method')} {doc_if.get('path')} != "
                      f"implemented {code_if.get('method')} {code_if.get('path')}")
    else:
        detail = (f"documented {doc_if.get('symbol')}{doc_if.get('parameters')} != "
                  f"implemented {code_if.get('symbol')}{code_if.get('parameters')}")
    return {**result, "survives": True, "reason": "interface_mismatch",
            "subject": doc_fact.get("subject", ""),
            "base_state": f"documented {doc_if}",
            "head_state": f"implemented {code_if}",
            "trigger": "caller follows the documented interface",
            "bad_outcome": "calls fail or misbehave against the implementation",
            "detail": detail,
            "artifact_fact_ids": [doc_fact["fact_id"], code_fact["fact_id"]]}


register_proof_validator("docs_code.literal_value_mismatch.v1", validate_literal_value_mismatch)
register_proof_validator("docs_code.named_contract_missing.v1", validate_named_contract_missing)
register_proof_validator("docs_code.allowed_values_mismatch.v1", validate_allowed_values_mismatch)
register_proof_validator("docs_code.interface_contract_mismatch.v1", validate_interface_contract_mismatch)


def _check_facts_snapshot(record: dict, fact_by_id: dict) -> list[str]:
    """Every input fact must resolve, equal its snapshot, and be bound to
    a revision with evidence. Unbound facts (no revision/SHA/evidence)
    cannot replay, even when snapshots agree."""
    errors: list[str] = []
    for role, fid in (record.get("input_fact_ids") or {}).items():
        fact = fact_by_id.get(fid)
        if fact is None:
            errors.append(f"input fact {role} ({fid}) does not resolve")
            continue
        snapshot = (record.get("facts_snapshot") or {}).get(role)
        if snapshot is None:
            errors.append(f"input fact {role} has no recorded snapshot")
        elif {k: v for k, v in fact.items()} != {k: v for k, v in snapshot.items()}:
            errors.append(f"input fact {role} was tampered after validation")
        if not fact.get("revision") or not fact.get("revision_sha"):
            errors.append(f"input fact {role} is not revision-bound")
        if not fact.get("evidence_ids"):
            errors.append(f"input fact {role} has no evidence linkage")
    return errors


REQUIRED_ROLES = {
    "docs_code.literal_value_mismatch.v1": ("doc_base", "code_base", "doc_head", "code_head"),
    "docs_code.named_contract_missing.v1": ("doc_fact",),
    "docs_code.allowed_values_mismatch.v1": ("doc_fact", "code_fact"),
    "docs_code.interface_contract_mismatch.v1": ("doc_fact",),
}


def replay_proof(record: dict, fact_by_id: dict, *,
                 base_checkout=None, head_checkout=None) -> dict:
    """Re-run the registered validator from ledger facts (+ recomputed
    repository context). Returns the replayed result dict."""
    from codatiel_i2.detectors.docs_code import facts as facts_mod

    validator_id = record.get("validator_id", "")
    fn = PROOF_VALIDATORS.get(validator_id)
    base = {"validator": validator_id, "version": record.get("validator_version", "")}
    if fn is None:
        return {**base, "survives": False, "reason": "validator not registered"}
    inputs = record.get("input_fact_ids") or {}
    required = REQUIRED_ROLES.get(validator_id, ())
    missing = [role for role in required if role not in inputs]
    if missing:
        return {**base, "survives": False,
                "reason": f"proof record missing roles: {sorted(missing)}"}
    errors = _check_facts_snapshot(record, fact_by_id)
    if errors:
        return {**base, "survives": False, "reason": "; ".join(errors)}
    facts = {role: fact_by_id[fid] for role, fid in inputs.items()}
    extra = record.get("proof_inputs") or {}
    if validator_id == "docs_code.literal_value_mismatch.v1":
        return fn(facts["doc_base"], facts["code_base"],
                  facts["doc_head"], facts["code_head"])
    if validator_id == "docs_code.named_contract_missing.v1":
        # Recompute the surface from the exact checkouts: persisted sets
        # are audit data, never proof inputs. Detection enumerates head
        # repo-wide, so recomputation must match exactly; base is
        # recomputed repo-wide (a superset of the file-scoped baseline,
        # which preserves survive verdicts monotonically).
        head_names, head_aliases, scanned, _rel = facts_mod.enumerate_code_names(head_checkout)
        if set(extra.get("head_names", [])) != head_names:
            return {**base, "survives": False,
                    "reason": "recomputed surface differs from proof record"}
        base_names, _aliases, _n, _r = facts_mod.enumerate_code_names(base_checkout)
        return fn(facts["doc_fact"], head_names, base_names, head_aliases,
                  direction=extra.get("direction", "code_changed"),
                  surface_enumerated=scanned > 0)
    if validator_id == "docs_code.allowed_values_mismatch.v1":
        return fn(facts["doc_fact"], facts["code_fact"])
    if validator_id == "docs_code.interface_contract_mismatch.v1":
        implemented = set(extra.get("implemented_paths", []))
        code_fact = facts.get("code_fact")
        return fn(facts["doc_fact"], code_fact,
                  implemented_paths=implemented if implemented else None)
    return {**base, "survives": False, "reason": "unknown validator"}
