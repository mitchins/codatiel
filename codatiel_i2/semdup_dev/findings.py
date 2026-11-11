"""DEV findings contract (separate `semdup-dev` namespace).

A finding is built ONLY from the final challenged `assessment`
(`rep["assessment"]`). The preliminary Stage-C `verdict` is stored as
`preliminary_verdict` for audit, never published as actionable:

- assessment action in {reuse_existing, extract_shared_part,
  share_contract} -> actionable finding with that recommendation;
- assessment action investigate/keep_separate, challenger downgrade,
  missing assessment (no Stage D), or no valid cards -> recorded as
  investigate/deferred, never actionable;
- anything incomplete (stale/missing index, unresolvable target,
  exhausted budget) -> deferred with reason, never a clean negative.

No finding is emitted from an unchallenged or unvalidated preliminary
verdict. AI-authorship judgments are never made (no such field exists).
"""

from __future__ import annotations

import time
from typing import Any

from codatiel_i2.semdup_dev import SEMDUP_DEV_DETECTOR


def build_finding(*, repo: str, pr: int, base_rev: str, head_rev: str,
                  from_head: str, delta_fingerprint: str,
                  target: dict, report: dict, provenance: dict,
                  classification: str,
                  retrieval_coverage: str | None = None) -> dict:
    """Assemble one namespace record from a completed M11 report."""
    assessment = report.get("assessment")
    challenged = bool(report.get("stage_d"))
    preliminary = report.get("verdict", {})
    action = (assessment or {}).get("action")
    actionable = challenged and action in ("reuse_existing",
                                           "extract_shared_part",
                                           "share_contract")
    members = (assessment or {}).get("member_notes", [])
    family = list(preliminary.get("recommended_family") or [])
    if actionable:
        status = "finding"
        recommendation = action
    elif challenged:
        status = "investigate"
        recommendation = "investigate"
    else:
        status = "deferred"
        recommendation = "investigate"
        action = "investigate"
    return {
        "detector": SEMDUP_DEV_DETECTOR,
        "status": status,
        "repo": repo, "pr": pr,
        "base_rev": base_rev, "head_rev": head_rev,
        "from_head": from_head, "delta_fingerprint": delta_fingerprint,
        "classification": classification,  # introduced | worsened
        "target": {"path": target["path"], "symbol": target["symbol"],
                   "start_line": target.get("start_line"),
                   "end_line": target.get("end_line")},
        "counterparts": family,
        "relationship": (assessment or {}).get("relationship", "unknown"),
        "recommended_action": recommendation,
        "preliminary_action": preliminary.get("action"),
        "preliminary_published": False,
        "proposed_boundary": (assessment or {}).get("proposal"),
        "material_differences": (assessment or {}).get(
            "observed_differences", []),
        "must_remain_separate": (assessment or {}).get(
            "remain_separate", []),
        "challenge": {
            "challenged": challenged,
            "fatal_claims": report.get("stage_d", {}).get("fatal_claims", []),
            "surviving_objections": (assessment or {}).get(
                "validation", {}).get("unresolved", []),
            "validation_level": (assessment or {}).get(
                "validation", {}).get("level"),
        },
        "member_notes": members,
        "evidence": {
            "finalists": report.get("finalists", []),
            "finalist_truncated": report.get("finalist_truncated", []),
            # Adapter-measured coverage wins: the engine reports "full"
            # for any non-None emb, even a partially-vectored one.
            "retrieval_coverage": (retrieval_coverage
                                   if retrieval_coverage is not None
                                   else report.get("retrieval_coverage")),
            "guards": {
                "family_guardrail": report.get("family_guardrail", {}),
                "wrapper_guard": report.get("wrapper_guard", {}),
                "ownership_guard": report.get("ownership_guard", {}),
            },
        },
        "provenance": provenance,
        "timing": {"wall_s": report.get("wall_s"),
                   "report_sha256": report.get("report_sha256")},
        "created_ts": time.time(),
    }


def deferred_record(*, repo: str, pr: int, base_rev: str, head_rev: str,
                    from_head: str, delta_fingerprint: str,
                    reason: str, detail: str = "",
                    classification: str = "unknown") -> dict:
    """Explicit incomplete/deferred record. Never a clean negative."""
    return {
        "detector": SEMDUP_DEV_DETECTOR,
        "status": "deferred",
        "reason": reason, "detail": detail,
        "repo": repo, "pr": pr,
        "base_rev": base_rev, "head_rev": head_rev,
        "from_head": from_head, "delta_fingerprint": delta_fingerprint,
        "classification": classification,
        "recommended_action": "investigate",
        "preliminary_published": False,
        "created_ts": time.time(),
    }


def summarize_records(records: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    for r in records:
        key = f"{r.get('status')}:{r.get('recommended_action', '')}" \
            if r.get("status") == "finding" else r.get("status", "?")
        kinds[key] = kinds.get(key, 0) + 1
    return {"n": len(records), "by_status": kinds,
            "actionable": sum(1 for r in records
                              if r.get("status") == "finding")}
