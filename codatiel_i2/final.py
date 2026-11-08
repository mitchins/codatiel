"""FINAL-01: reserved tools-disabled final disposition."""

import json
from typing import Any

def _available_ids(tool_evidence):
    ids=[]
    seen=set()
    for e in tool_evidence:
        eid=e.get("evidence_id")
        if eid and eid not in seen:
            seen.add(eid)
            ids.append(eid)
    return ids

def final_prompt(avail):
    return (
        "Investigation budget exhausted. Provide final disposition "
        "based ONLY on evidence already returned in this run. "
        f"Available evidence IDs: {json.dumps(avail)}. "
        "Respond with one JSON object: "
        '{"status": "finding"|"silence"|"inconclusive", "summary": "...", '
        '"findings": [{"detector": "...", "title": "...", "path": "...", '
        '"revision": "base|head", "line_start": 1, "line_end": 1, '
        '"claim": "...", "evidence_ids": ["ev_..."]}], '
        '"reasons": ["..."] if inconclusive}. '
        "Findings may cite only IDs from the available list. "
        "Do not invent evidence."
    )

def validate_final(f_parsed, avail):
    status=f_parsed.get("status")
    if status not in ("finding","silence","inconclusive"):
        return False, "invalid_status"
    if status=="finding":
        for ff in f_parsed.get("findings",[]):
            for eid in ff.get("evidence_ids",[]):
                if eid not in avail:
                    return False, "missing_evidence"
    return True, status
