"""Finding assembly: validated assessment + re-validated spans -> v1 JSON.

Span re-validation happens here against the live snapshot root: a span
whose file hash drifted since indexing is rejected (stale evidence never
ships). Report-only levels only unless artifacts exist.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import spans as sp


class FindingError(RuntimeError):
    pass


def to_finding(assessment: dict, packet: dict, finding_id: str, root: Path) -> dict:
    members = []
    for m in packet["members"]:
        span = {"path": m["path"], "start_line": m["start_line"],
                "end_line": m["end_line"], "source_sha256": m["source_sha256"]}
        if m.get("symbol"):
            span["symbol"] = m["symbol"]
        errors = sp.validate_span({k: span[k] for k in ("path", "start_line", "end_line", "source_sha256")}, root)
        if errors:
            raise FindingError(f"stale/invalid member span: {errors}")
        members.append(span)
    if len(members) < 2:
        raise FindingError("a finding needs at least two independent members")
    support = []
    for s in packet.get("support", []):
        span = {"path": s["path"], "start_line": s["start_line"],
                "end_line": s["end_line"], "source_sha256": s["source_sha256"]}
        errors = sp.validate_span(span, root)
        if errors:
            raise FindingError(f"stale/invalid support span: {errors}")
        support.append(span)
    proposal = assessment["proposal"]
    target = None
    if proposal["target"] is not None:
        match = next((m for m in members
                      if (m["path"], m["start_line"], m["end_line"]) ==
                      (proposal["target"]["path"], proposal["target"]["start_line"], proposal["target"]["end_line"])), None)
        if match is None:
            raise FindingError("reuse target left the member set")
        target = match
    return {
        "id": finding_id,
        "responsibility": assessment["responsibility"],
        "relationship": assessment["relationship"],
        "action": assessment["action"],
        "members": members,
        "support": support,
        "observed_differences": assessment.get("observed_differences", []),
        "proposal": {
            "target": target,
            "interface": proposal["interface"],
            "preserved": proposal["preserved"],
            "remain_separate": proposal["remain_separate"],
            "benefit": proposal["benefit"],
            "behavior_changes": proposal["behavior_changes"],
        },
        "validation": assessment["validation"],
    }
