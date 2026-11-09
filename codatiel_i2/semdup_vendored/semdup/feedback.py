"""Stable feedback: owner-recorded decisions scoped to responsibility + hashes.

A record pins (responsibility, member path+sha set) to a decision
(`keep_separate` / `accepted`). Exact match with unchanged hashes
suppresses re-report (the record IS the explicit owner action); any
content change reopens the finding. Never file-wide, never permanent
across content change.
"""
from __future__ import annotations

import json
from pathlib import Path


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def key_for(responsibility: str, members: list[dict]) -> str:
    """Match key: normalized responsibility + member span set.

    Per the frozen M0 §4 scope (responsibility + source/context hashes).
    Normalization tolerates case/whitespace variance only: a paraphrased
    responsibility is a different key and re-reports (noisy but safe —
    over-suppression would bury genuinely new claims). Any content change
    alters the hashes and reopens the finding.
    """
    parts = sorted(f"{m['path']}:{m['source_sha256']}" for m in members)
    return _normalize(responsibility) + "\n" + "\n".join(parts)


def load(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def match(records: list[dict], responsibility: str, members: list[dict]) -> dict | None:
    want = key_for(responsibility, members)
    for record in records:
        if record.get("key") == want:
            return record
    return None


def append(path: Path, responsibility: str, members: list[dict], decision: str) -> dict:
    record = {"key": key_for(responsibility, members), "decision": decision,
              "responsibility": responsibility,
              "members": [{"path": m["path"], "source_sha256": m["source_sha256"]} for m in members]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record
