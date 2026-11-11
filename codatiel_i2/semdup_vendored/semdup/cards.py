"""Evidence-card contract (card_v2): span-backed, role-explicit cards.

A card is an attention map, not evidence: every behavior claim and
every material difference carries a path:start-end span re-validated
against the live snapshot (spans.py). Relationship and boundary are
separate (§6): strong relationship never implies whole-unit reuse.
Candidate role (§5) forces the wrapper/implementation distinction
that  proved harmful when blurred.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import schema_check as sc
from . import spans as SP

CARD_VERSION = "card_v2"

CANDIDATE_ROLES = ("implementation", "wrapper", "orchestrator", "policy",
                   "adapter", "helper", "generated_artifact", "delegator",
                   "mixed", "unknown")

RELATIONSHIPS = ("equivalent", "shared_part", "diverged", "related_only",
                 "unrelated", "unknown")

DIFF_KINDS = ("behavioral", "contract", "effect", "ownership", "lifecycle",
              "error", "other")

CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidate_id", "candidate_role", "relationship_hypothesis",
                 "shared_responsibility", "shared_behavior",
                 "material_differences", "existing_abstraction",
                 "possible_shared_boundary", "what_would_refute_this",
                 "confidence"],
    "properties": {
        "candidate_id": {"type": "string", "minLength": 1},
        "candidate_role": {"type": "string", "enum": list(CANDIDATE_ROLES)},
        "relationship_hypothesis": {"type": "string", "enum": list(RELATIONSHIPS)},
        "shared_responsibility": {"type": "string"},
        "shared_behavior": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["claim", "span"],
                      "properties": {"claim": {"type": "string", "minLength": 1},
                                     "span": {"type": "string", "minLength": 1}}},
        },
        "material_differences": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["kind", "claim", "span"],
                      "properties": {
                          "kind": {"type": "string", "enum": list(DIFF_KINDS)},
                          "claim": {"type": "string", "minLength": 1},
                          "span": {"type": "string", "minLength": 1}}},
        },
        "existing_abstraction": {
            "anyOf": [{"type": "null"},
                      {"type": "object", "additionalProperties": False,
                       "required": ["symbol", "span"],
                       "properties": {"symbol": {"type": "string", "minLength": 1},
                                      "span": {"type": "string", "minLength": 1}}}]},
        "possible_shared_boundary": {
            "type": "object", "additionalProperties": False,
            "required": ["description", "span_or_null"],
            "properties": {"description": {"type": "string"},
                            "span_or_null": {"anyOf": [{"type": "null"},
                                                       {"type": "string"}]}}},
        "what_would_refute_this": {"type": "array",
                                   "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}


def _parse_span(text: str) -> dict[str, Any] | None:
    """path:start-end (or path:line = single line) -> span dict shell."""
    if not isinstance(text, str) or ":" not in text:
        return None
    path, _, rng = text.rpartition(":")
    if "-" not in rng:
        try:
            line = int(rng)
        except (ValueError, TypeError):
            return None
        return {"path": path.strip(), "start_line": line, "end_line": line}
    start, _, end = rng.partition("-")
    try:
        return {"path": path.strip(), "start_line": int(start),
                "end_line": int(end)}
    except (ValueError, TypeError):
        return None


def validate_card(value: dict[str, Any], root: Path,
                  file_hashes: dict[str, str] | None = None) -> list[str]:
    """Schema + span re-validation. Returns error list (empty = valid).

    file_hashes: {relpath: sha256} snapshot record; when provided, span
    staleness is checked without disk re-read per span.
    """
    errors = sc.validate(value, CARD_SCHEMA)
    if errors or not isinstance(value, dict):
        return errors
    try:
        from .spans import file_hash
    except ImportError:
        file_hash = None  # type: ignore[assignment]

    def check_span(text: str, where: str) -> None:
        shell = _parse_span(text)
        if shell is None and text:
            # Empty span allowed only where explicitly nullable; behavior
            # and difference spans must locate evidence.
            errors.append(f"{where}: span must be path:start-end, got {text!r}")
            return
        if shell is None:
            return
        span = dict(shell)
        if file_hashes is not None and span["path"] in file_hashes:
            span["source_sha256"] = file_hashes[span["path"]]
        elif file_hash is not None:
            try:
                span["source_sha256"] = file_hash(
                    SP.safe_path(root, span["path"]))
            except (ValueError, OSError) as exc:
                errors.append(f"{where}: {exc}")
                return
        else:
            return
        for err in SP.validate_span(span, root):
            errors.append(f"{where}: {err}")

    for i, b in enumerate(value.get("shared_behavior", []) or []):
        check_span(b.get("span", ""), f"shared_behavior[{i}]")
    for i, d in enumerate(value.get("material_differences", []) or []):
        check_span(d.get("span", ""), f"material_differences[{i}]")
    abs_ = value.get("existing_abstraction")
    if isinstance(abs_, dict):
        check_span(abs_.get("span", ""), "existing_abstraction")
    bnd = (value.get("possible_shared_boundary") or {})
    if bnd.get("span_or_null"):
        check_span(bnd["span_or_null"], "possible_shared_boundary")
    # §5/§6 cross-checks (structural, model hypothesis flagged).
    role = value.get("candidate_role")
    rel = value.get("relationship_hypothesis")
    if role in ("wrapper", "delegator", "generated_artifact") and rel == "equivalent":
        errors.append("role/relationship: wrapper/delegator/generated_artifact "
                      "cannot be 'equivalent' (layer confusion —  harm class)")
    if rel == "shared_part" and not (bnd.get("description") or "").strip():
        errors.append("shared_part requires a possible_shared_boundary "
                      "description (or an explicit cannot-identify statement)")
    if rel == "equivalent" and not (value.get("shared_behavior") or []):
        errors.append("equivalent requires at least one span-backed shared_behavior")
    return errors
