"""B1: frozen proof vocabulary for docs_code (five factual relationships + direction).

Each class names its validator (docs_code.<name>.v1) and the fact schema
that validator requires. Validators are registered in validators.py for
literal, named, allowed, and interface classes; the behaviour class has
vocabulary only, no validator (unimplemented).
"""

from __future__ import annotations

PROOF_CLASSES = (
    {
        "class_id": "literal_default",
        "validator": "docs_code.literal_value_mismatch.v1",
        "title": "Literal/default mismatch",
        "description": "Documented literal (default, constant, timeout) differs from the runtime value.",
        "directions": ("code_changed", "docs_changed"),
        "doc_fact_schema": {"kind": "documented_default", "subject": "str", "value": "scalar",
                            "unit": "str", "evidence_id": "str"},
        "code_fact_schema": {"kind": "runtime_default", "subject": "str", "value": "scalar",
                             "unit": "str", "evidence_id": "str"},
        "proof_rule": "same subject AND same semantic unit AND different values",
    },
    {
        "class_id": "named_contract",
        "validator": "docs_code.named_contract_missing.v1",
        "title": "Named option/config/env/CLI existence",
        "description": "Documentation names an option that no longer exists (renamed/removed) or vice versa.",
        "directions": ("code_changed", "docs_changed"),
        "doc_fact_schema": {"kind": "documented_name", "name": "str", "surface": "cli|env|config|key",
                            "evidence_id": "str"},
        "code_fact_schema": {"kind": "declared_name", "name": "str", "surface": "cli|env|config|key",
                             "evidence_id": "str"},
        "proof_rule": "documented name has no matching declaration on the same surface",
    },
    {
        "class_id": "allowed_values",
        "validator": "docs_code.allowed_values_mismatch.v1",
        "title": "Allowed-value / enum mismatch",
        "description": "Documented allowed set differs from the implemented set.",
        "directions": ("code_changed", "docs_changed"),
        "doc_fact_schema": {"kind": "documented_values", "subject": "str", "values": "list[str]",
                            "evidence_id": "str"},
        "code_fact_schema": {"kind": "implemented_values", "subject": "str", "values": "list[str]",
                             "evidence_id": "str"},
        "proof_rule": "same subject AND set difference in either direction",
    },
    {
        "class_id": "interface",
        "validator": "docs_code.interface_contract_mismatch.v1",
        "title": "Public route / signature / invocation mismatch",
        "description": "Documented route path, signature, or call convention differs from the implementation.",
        "directions": ("code_changed", "docs_changed"),
        "doc_fact_schema": {"kind": "documented_interface", "subject": "str", "signature": "str",
                            "evidence_id": "str"},
        "code_fact_schema": {"kind": "implemented_interface", "subject": "str", "signature": "str",
                             "evidence_id": "str"},
        "proof_rule": "same subject AND structurally different interface",
    },
    {
        "class_id": "behaviour",
        "validator": "docs_code.behaviour_claim_contradiction.v1",
        "title": "Behavioural factual contradiction",
        "description": "Documented behaviour claim contradicts implemented behaviour with no literal comparison. "
                       "Includes reverse-direction capability claims (docs promise unsupported behaviour).",
        "directions": ("code_changed", "docs_changed"),
        "doc_fact_schema": {"kind": "behaviour_claim", "subject": "str", "claim": "str",
                            "evidence_id": "str"},
        "code_fact_schema": {"kind": "code_behaviour", "subject": "str", "behaviour": "str",
                             "evidence_id": "str"},
        "proof_rule": "structured claim AND structured code fact AND exact evidence AND trigger AND outcome",
    },
)

CLASS_IDS = tuple(c["class_id"] for c in PROOF_CLASSES)
VALIDATOR_NAMES = tuple(c["validator"] for c in PROOF_CLASSES)

# Explicitly out of scope for first qualification (brief §Not yet).
OUT_OF_SCOPE = (
    "vague architecture prose",
    "performance documentation",
    "aspirational roadmap text",
    "comments describing rationale",
    "changelogs",
    "recommended behaviour",
    "subjective descriptions",
    "screenshots",
    "complex executable tutorials",
)
