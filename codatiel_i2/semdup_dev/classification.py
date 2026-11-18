"""SEMDUP-SIGNAL-01 finding classification schema (deterministic, no inference).

Two independent axes recorded from the Luna editorial response:

- primary_category: what responsibility the duplication concerns.
- divergence_impact: potential impact if the implementations diverge
  (NOT the severity of a confirmed defect).

Missing or malformed metadata always falls back to ``unknown`` and never
invalidates an otherwise valid review verdict. Classification never
suppresses, prioritises or alters publication.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "signal-classification-0.1"

PRIMARY_CATEGORIES = (
    "security_safety",
    "state_integrity",
    "lifecycle_recovery",
    "verification",
    "performance_resilience",
    "maintainability",
    "unknown",
)

DIVERGENCE_IMPACTS = (
    "critical",
    "high",
    "medium",
    "low",
    "unknown",
)

_MAX_EVIDENCE_CHARS = 500


def _clean_text(value: Any) -> str:
    try:
        text = str(value or "")
    except Exception:
        return ""
    return text[:_MAX_EVIDENCE_CHARS]


def normalize_classification(raw: Any) -> dict[str, Any]:
    """Normalize model-supplied classification metadata. Never raises.

    Returns a dict with ``primary_category`` and ``divergence_impact``
    drawn from the bounded enums (``unknown`` fallback), an optional
    ``secondary_category`` (None when absent or malformed — never
    fabricated), short source-backed evidence strings, and an explicit
    ``uncertainty`` flag (True whenever the impact cannot be
    established, including every ``unknown``).
    """
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "primary_category": "unknown",
        "secondary_category": None,
        "divergence_impact": "unknown",
        "shared_responsibility": "",
        "divergence_consequence": "",
        "uncertainty": True,
    }
    if not isinstance(raw, dict):
        return out
    try:
        primary = raw.get("primary_category")
        if isinstance(primary, str) and primary in PRIMARY_CATEGORIES:
            out["primary_category"] = primary
        secondary = raw.get("secondary_category")
        if (isinstance(secondary, str)
                and secondary in PRIMARY_CATEGORIES
                and secondary != out["primary_category"]):
            out["secondary_category"] = secondary
        impact = raw.get("divergence_impact")
        if isinstance(impact, str) and impact in DIVERGENCE_IMPACTS:
            out["divergence_impact"] = impact
        out["shared_responsibility"] = _clean_text(
            raw.get("shared_responsibility"))
        out["divergence_consequence"] = _clean_text(
            raw.get("divergence_consequence"))
        if isinstance(raw.get("uncertainty"), bool):
            out["uncertainty"] = raw["uncertainty"]
        else:
            out["uncertainty"] = (
                out["divergence_impact"] == "unknown"
                or out["primary_category"] == "unknown")
    except Exception:
        return {
            "schema_version": SCHEMA_VERSION,
            "primary_category": "unknown",
            "secondary_category": None,
            "divergence_impact": "unknown",
            "shared_responsibility": "",
            "divergence_consequence": "",
            "uncertainty": True,
        }
    return out


CLASSIFICATION_PROMPT_BLOCK = """
Also describe what kind of duplication this is, WITHOUT changing your
KEEP/REVISE/DROP judgment (classification never alters the decision).
Add a "classification" object to your JSON response:
{"primary_category": "security_safety|state_integrity|lifecycle_recovery|verification|performance_resilience|maintainability|unknown",
 "secondary_category": "one of the same values or null when not needed",
 "divergence_impact": "critical|high|medium|low|unknown",
 "shared_responsibility": "the shared invariant or responsibility, in a few words",
 "divergence_consequence": "what could go wrong if the implementations diverged, or \\"\\" when unknown",
 "uncertainty": true|false}
Rules: "critical"/"high" require concrete supporting evidence from the
delivered source — never promote speculative future consequences into
claims. Divergence impact is potential impact if the implementations
became inconsistent, not the severity of a confirmed defect. A
duplicated guard is not automatically High; a test-related finding is
not automatically Low. Omit the object or use "unknown" when the
evidence does not support a label."""
