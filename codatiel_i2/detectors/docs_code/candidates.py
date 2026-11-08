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
    r"without\s+.*support|does\s+not\s+require|do\s+not\s+require|"
    r"doesn['\u2019]t\s+require|not\s+required|without\s+requir)\b", re.I)

# A token that the sentence itself frames as data, not as a provided
# contract: "api_key_env names an env var", "stores only the name",
# "is set via host env". Static absence then proves nothing.
REFERENCE_CLAIM = re.compile(
    r"\bnam(es|ing)\b.{0,60}\benv\b|\bstores?\b.{0,60}\bnames?\b|"
    r"\bset via host env\b|\bthe name\b.{0,60}\b(stored|configured)\b", re.I)

# Doc lines that disclaim serving: "(verified 404)", "not served".
NOT_SERVED_LINE = re.compile(
    r"\b404\b|not\s+served|unserved|does\s+not\s+exist\b", re.I)


def _cli_norm(name: str) -> str:
    return name.lstrip("-")


REJECTION_REASONS = (
    "different_subject",
    "compatible_values",
    "compatible_call",
    "matched_interface",
    "present_contract",
    "negated_claim",
    "reference_not_contract",
    "non_contract_shape",
    "bare_mention",
    "ambiguous_call",
    "non_exhaustive_documentation",
    "unsupported_syntax",
    "insufficient_evidence",
    "ambiguous_absence",
    "duplicate_issue",
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
    if not name.startswith("--") and REFERENCE_CLAIM.search(doc_fact.get("claim", "")):
        # The sentence frames the token as data ("names an env var",
        # "stores only the name"): it is not itself a provided contract.
        return {"candidate": False, "reason": "reference_not_contract"}
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


def call_compatible(doc_if: dict, code_if: dict) -> tuple[bool | None, str]:
    """Python call semantics: positionals fill by ORDER (names irrelevant),
    keywords must match case-sensitively, required slots must be filled.

    ``*args`` absorbs extra POSITIONALS only; ``**kwargs`` absorbs extra
    KEYWORDS only; neither does the other's job. A case-only parameter-name
    difference on a positionally-passed argument (documented
    ``create_app(Settings.load(...))`` vs ``def create_app(settings``) is a
    WORKING call, not a contradiction.

    Returns (True, "") when the documented call works, (False, failure)
    when it demonstrably raises TypeError, and (None, reason) when
    compatibility cannot be established: ambiguity is never a verified
    break.
    """
    doc_params = doc_if.get("parameters")
    code_params = code_if.get("parameters")
    if not isinstance(doc_params, list) or not isinstance(code_params, list):
        return None, "parameter info unavailable: compatibility unprovable"
    # Interface-level flags; fall back to the legacy combined per-param
    # flag (lenient on both axes) for shapes that predate the split.
    legacy = any(p.get("variadic") for p in code_params)
    pos_var = code_if.get("variadic_pos", legacy)
    kw_var = code_if.get("variadic_kw", legacy)
    code_by_name = {p["name"]: p for p in code_params}
    positional_code = [p for p in code_params if not p.get("keyword_only")]
    doc_pos = [p for p in doc_params if not p.get("keyword")]
    doc_kw = [p for p in doc_params if p.get("keyword")]
    symbol = code_if.get("symbol") or doc_if.get("symbol") or "?"
    kw_names = {p["name"] for p in doc_kw}
    # Same argument twice: f(1, x=2) against def f(x, ...) is a TypeError
    # no splat can rescue (kwargs cannot refill a filled slot).
    positional_names = [p["name"] for p in positional_code[:len(doc_pos)]]
    duplicated = sorted({k for k in kw_names if k in positional_names})
    if duplicated:
        return False, (f"documented call passes {duplicated} both "
                       f"positionally and by keyword: TypeError")
    unexpected = [p["name"] for p in doc_kw if p["name"] not in code_by_name]
    if unexpected and not kw_var:
        accepts = sorted(code_by_name) or ["(nothing)"]
        return False, (f"documented keyword(s) {unexpected} are not accepted "
                       f"by {symbol} (accepts: {accepts}): TypeError")
    if len(doc_pos) > len(positional_code) and not pos_var:
        return False, (f"documented call supplies {len(doc_pos)} positional "
                       f"argument(s) but {symbol} accepts "
                       f"{len(positional_code)}: TypeError")
    missing: list[str] = []
    for i, cp in enumerate(positional_code):
        if not cp.get("required"):
            continue
        if i < len(doc_pos):
            continue
        if cp["name"] in kw_names:
            continue
        missing.append(cp["name"])
    for cp in code_params:
        if (cp.get("keyword_only") and cp.get("required")
                and cp["name"] not in kw_names):
            missing.append(cp["name"])
    if missing:
        return False, (f"documented call omits required {missing}: "
                       f"{symbol}() raises TypeError")
    return True, ""


def _sig_shape(iface: dict, documented: bool) -> str:
    parts = []
    for p in iface.get("parameters", []):
        if documented:
            parts.append(("kw:" if p.get("keyword") else "pos:") + p.get("name", "?"))
        else:
            parts.append(p.get("name", "?")
                         + ("" if p.get("required") else "?")
                         + ("#" if p.get("keyword_only") else ""))
    return ",".join(parts)


def issue_key(proof_class: str, fact_list: list[dict]) -> str:
    """Underlying issue identity: same defect, different evidence or pair,
    must collapse to one candidate before verification and one finding
    before delivery. Stable across pairs (no evidence IDs, no anchors)."""
    from codatiel_i2.detectors.docs_code.facts import (
        normalize_route_template, subject_key)
    doc = next((f for f in fact_list
                if f.get("fact_kind", "").startswith("documented")), None)
    code = next((f for f in fact_list
                 if not f.get("fact_kind", "").startswith("documented")), None)
    doc = doc or {}
    code = code or {}
    if proof_class == "named_contract":
        return "named|" + _cli_norm(doc.get("name", ""))
    if proof_class == "interface":
        doc_if = doc.get("interface", {})
        if doc_if.get("kind") == "http_route":
            method = doc_if.get("method") or "*"
            return ("route|" + str(method) + "|"
                    + normalize_route_template(doc_if.get("path", "")))
        code_if = code.get("interface", {})
        return ("sig|" + subject_key(doc_if.get("symbol", "")) + "|"
                + _sig_shape(doc_if, True) + "|" + _sig_shape(code_if, False))
    if proof_class == "literal_default":
        return ("literal|" + str(doc.get("subject", "")) + "|"
                + str(doc.get("value", "")) + "|"
                + str(code.get("value", "")) + "|"
                + str(doc.get("unit", "") or ""))
    if proof_class == "allowed_values":
        return ("allowed|" + str(doc.get("subject", "")) + "|"
                + ",".join(sorted(doc.get("values", []))) + "|"
                + ",".join(sorted(code.get("values", []))))
    return proof_class + "|unknown"


def form_interface_candidate(doc_fact: dict, code_fact: dict) -> dict:
    doc_if, code_if = doc_fact.get("interface", {}), code_fact.get("interface", {})
    if doc_if.get("kind") != code_if.get("kind"):
        return {"candidate": False, "reason": "different_subject"}
    if doc_if.get("kind") == "http_route":
        if doc_if.get("method") == code_if.get("method") and doc_if.get("path") == code_if.get("path"):
            return {"candidate": False, "reason": "matched_interface"}
        return {"candidate": True, "reason": "route_mismatch",
                "validator": "docs_code.interface_contract_mismatch.v1"}
    from codatiel_i2.detectors.docs_code.facts import subject_key as _skey
    if _skey(doc_if.get("symbol", "")) != _skey(code_if.get("symbol", "")):
        return {"candidate": False, "reason": "different_subject"}
    params = doc_if.get("parameters")
    if params is None or not isinstance(params, list):
        # Parameter info unavailable: compatibility is unprovable, and an
        # unprovable call is never a verified break.
        return {"candidate": False, "reason": "ambiguous_call"}
    if params == []:
        # A parameterless mention (```load()``` in prose) carries no
        # signature claim to contradict; pairing it with an unrelated
        # same-named function manufactures issues.
        return {"candidate": False, "reason": "bare_mention"}
    verdict, failure = call_compatible(doc_if, code_if)
    if verdict is None:
        return {"candidate": False, "reason": "ambiguous_call"}
    if verdict:
        return {"candidate": False, "reason": "compatible_call"}
    return {"candidate": True, "reason": "signature_mismatch",
            "validator": "docs_code.interface_contract_mismatch.v1",
            "failure": failure}
