"""Bounded candidate formation: a retrieved counterpart is not a contradiction.

Pipeline per retrieved pair:
  counterpart -> extract code fact -> extract doc fact
  -> check comparable subject/type -> emit or reject with reason.

Rejection reasons (persisted for debuggability):
  different_subject, compatible_values, matched_interface, present_contract,
  non_exhaustive_documentation, unsupported_syntax, insufficient_evidence,
  ambiguous_absence.
"""

from __future__ import annotations

import re

NEGATED_CLAIM = re.compile(
    r"\b(did\s+not\s+add|didn['\u2019]t\s+add|not\s+added|never\s+added|"
    r"rejected|decided\s+against|does\s+not\s+support|doesn['\u2019]t\s+support|"
    r"without\s+.*support)\b", re.I)


def _cli_norm(name: str) -> str:
    return name.lstrip("-")


REJECTION_REASONS = (
    "different_subject",
    "compatible_values",
    "matched_interface",
    "present_contract",
    "negated_claim",
    "non_exhaustive_documentation",
    "unsupported_syntax",
    "insufficient_evidence",
    "ambiguous_absence",
)


def subjects_match(doc_subject: str, code_subject: str,
                   doc_parts: list | None = None,
                   code_parts: list | None = None,
                   doc_raw: list | None = None,
                   code_raw: list | None = None) -> bool:
    from codatiel_i2.detectors.docs_code.facts import strips_compatible

    if doc_parts is not None and code_parts is not None:
        doc_set = {p for p in doc_parts if p}
        code_set = {p for p in code_parts if p}
        if doc_set and code_set and (doc_set <= code_set or code_set <= doc_set):
            if doc_raw is None or code_raw is None:
                return True
            return strips_compatible(doc_raw, sorted(doc_set), code_raw, sorted(code_set))
        # Abbreviation compatibility (documented risk): every doc part must
        # prefix-match some code part, e.g. output ~ OutFormat. Minimum stem
        # 3. The value/set validator is the backstop, and corpus specificity
        # measures the cost. Never used for proof.
        if doc_set and code_set and all(
                any(c == d or (len(c) >= 3 and d.startswith(c)) or (len(d) >= 3 and c.startswith(d))
                    for c in code_set) for d in doc_set):
            return True
        return False
    return bool(doc_subject) and doc_subject != "unknown" and doc_subject == code_subject


def form_literal_candidate(doc_fact: dict, code_fact: dict) -> dict:
    """Comparable numeric defaults with different values -> candidate."""
    if not subjects_match(doc_fact.get("subject", ""), code_fact.get("subject", ""),
                           doc_fact.get("subject_parts"), code_fact.get("subject_parts"),
                           doc_fact.get("subject_raw_parts"), code_fact.get("subject_raw_parts")):
        return {"candidate": False, "reason": "different_subject"}
    if doc_fact.get("value_type") != code_fact.get("value_type"):
        if {doc_fact.get("value_type"), code_fact.get("value_type")} <= {"int", "float"}:
            pass
        else:
            return {"candidate": False, "reason": "different_subject"}
    unit_d, unit_c = (doc_fact.get("unit") or ""), (code_fact.get("unit") or "")
    if unit_d and unit_c and unit_d != unit_c:
        return {"candidate": False, "reason": "insufficient_evidence"}
    if doc_fact.get("value") == code_fact.get("value"):
        return {"candidate": False, "reason": "compatible_values"}
    return {"candidate": True, "reason": "value_mismatch",
            "validator": "docs_code.literal_value_mismatch.v1"}


def form_named_candidate(doc_fact: dict, code_facts: list[dict]) -> dict:
    """Documented name with no live declaration -> candidate (absence is
    decided by the caller enumerating the surface, not here)."""
    name = doc_fact.get("name", "")
    if NEGATED_CLAIM.search(doc_fact.get("claim", "")):
        return {"candidate": False, "reason": "negated_claim"}
    norm = _cli_norm(name)
    live = [f for f in code_facts if _cli_norm(f.get("name", "")) == norm]
    if live:
        return {"candidate": False, "reason": "present_contract"}
    return {"candidate": True, "reason": "name_absent_from_surface",
            "validator": "docs_code.named_contract_missing.v1"}


def form_allowed_candidate(doc_fact: dict, code_fact: dict) -> dict:
    if doc_fact.get("claim_directed"):
        # "X is now supported" + X absent from the implemented set is a
        # concrete contradiction without any exhaustiveness claim.
        claimed = [v for v in doc_fact.get("values", []) if v not in code_fact.get("values", [])]
        if not claimed:
            return {"candidate": False, "reason": "compatible_values"}
        return {"candidate": True, "reason": "set_mismatch",
                "validator": "docs_code.allowed_values_mismatch.v1",
                "documented_unsupported": claimed,
                "implemented_undocumented": []}
    if not subjects_match(doc_fact.get("subject", ""), code_fact.get("subject", ""),
                           doc_fact.get("subject_parts"), code_fact.get("subject_parts"),
                           doc_fact.get("subject_raw_parts"), code_fact.get("subject_raw_parts")):
        return {"candidate": False, "reason": "different_subject"}
    if doc_fact.get("exhaustiveness") != "exhaustive":
        return {"candidate": False, "reason": "non_exhaustive_documentation"}
    doc_set, code_set = set(doc_fact.get("values", [])), set(code_fact.get("values", []))
    if doc_set == code_set:
        return {"candidate": False, "reason": "compatible_values"}
    return {"candidate": True, "reason": "set_mismatch",
            "validator": "docs_code.allowed_values_mismatch.v1",
            "documented_unsupported": sorted(doc_set - code_set),
            "implemented_undocumented": sorted(code_set - doc_set)}


def form_interface_candidate(doc_fact: dict, code_fact: dict) -> dict:
    doc_if, code_if = doc_fact.get("interface", {}), code_fact.get("interface", {})
    if doc_if.get("kind") != code_if.get("kind"):
        return {"candidate": False, "reason": "different_subject"}
    if doc_if.get("kind") == "http_route":
        if doc_if.get("method") == code_if.get("method") and doc_if.get("path") == code_if.get("path"):
            return {"candidate": False, "reason": "matched_interface"}
        return {"candidate": True, "reason": "route_mismatch",
                "validator": "docs_code.interface_contract_mismatch.v1"}
    doc_params = {p["name"]: p for p in doc_if.get("parameters", [])}
    code_params = {p["name"]: p for p in code_if.get("parameters", [])}
    from codatiel_i2.detectors.docs_code.facts import subject_key as _skey
    from codatiel_i2.detectors.docs_code.validators import _interfaces_equal
    if _interfaces_equal({**doc_if, "symbol": doc_if.get("symbol", "")},
                         {**code_if, "symbol": code_if.get("symbol", "")}):
        return {"candidate": False, "reason": "matched_interface"}
    return {"candidate": True, "reason": "signature_mismatch",
            "validator": "docs_code.interface_contract_mismatch.v1"}
