"""Detector-specific coverage (never a single generic PR percentage)."""

from __future__ import annotations

SCHEMA_VERSION = "codatiel_i2.detector_coverage.v1"

DETECTORS = ("contracts", "authorization", "docs_code", "operating_constraints", "semantic_twins")


def empty_coverage() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "detectors": {
            "docs_code": {"applicable_relationships": 0, "checked": 0, "unsupported": 0},
            "contracts": {"changed_boundaries": 0, "checked": 0, "unsupported": 0},
            "authorization": {"changed_entrypoints": 0, "checked": 0},
            "operating_constraints": {"changed_candidate_sites": 0, "checked": 0},
            "semantic_twins": {"changed_functions": 0, "queried": 0},
        },
    }


def summarize(coverage: dict) -> list[str]:
    d = coverage.get("detectors", {})
    lines = [
        f"Docs<->Code: {d.get('docs_code', {}).get('checked', 0)}/{d.get('docs_code', {}).get('applicable_relationships', 0)} relationships checked.",
        f"Contracts: {d.get('contracts', {}).get('checked', 0)}/{d.get('contracts', {}).get('changed_boundaries', 0)} boundaries checked.",
        f"Authorization: {d.get('authorization', {}).get('checked', 0)}/{d.get('authorization', {}).get('changed_entrypoints', 0)} entry points checked.",
        f"Operating constraints: {d.get('operating_constraints', {}).get('checked', 0)}/{d.get('operating_constraints', {}).get('changed_candidate_sites', 0)} sites checked.",
        f"Semantic twins: {d.get('semantic_twins', {}).get('queried', 0)}/{d.get('semantic_twins', {}).get('changed_functions', 0)} functions queried.",
    ]
    return lines
