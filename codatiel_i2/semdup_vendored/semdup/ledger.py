"""Attributable run ledger: per-call records + rollup (source-free).

Prompt/response bodies are NEVER written here — only hashes, sizes, and
accounting. The raw ledger file stays local with redaction/export controls;
only the redacted rollup (counts, model IDs, token totals with nulls
preserved) enters committed evidence.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class CallRecord:
    ts: float
    role: str  # judge | challenge | fast | embedding
    stage: str  # assessment | challenge | description | ablation
    endpoint: str
    served_model_id: str | None
    expected_model_id: str | None
    same_endpoint_as_judge: bool
    prompt_sha256: str
    prompt_chars: int
    prompt_version: str
    packet_sha256: str | None
    measured_prompt_tokens: int | None
    measured_completion_tokens: int | None
    estimated_prompt_tokens: int
    wall_s: float
    attempts: int
    retries: int
    failed: bool
    error: str | None
    cache_hit: bool = False
    unit: str | None = None  # "path:symbol" query unit for explorer calls


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[CallRecord] = []

    def add(self, record: CallRecord) -> None:
        self.records.append(record)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record)) + "\n")

    def rollup(self) -> list[dict]:
        by: dict[tuple[str, str], dict] = {}
        for r in self.records:
            key = (r.role, r.served_model_id or "unknown")
            slot = by.setdefault(key, {"role": r.role, "model_id": r.served_model_id or "unknown",
                                       "calls": 0, "failed_calls": 0, "prompt_tokens": 0,
                                       "completion_tokens": 0, "tokens_unknown": False, "wall_seconds": 0.0})
            slot["calls"] += 1
            slot["failed_calls"] += int(r.failed)
            slot["wall_seconds"] += r.wall_s
            if r.measured_prompt_tokens is None or r.measured_completion_tokens is None:
                slot["tokens_unknown"] = True
            else:
                slot["prompt_tokens"] += r.measured_prompt_tokens
                slot["completion_tokens"] += r.measured_completion_tokens
        out = []
        for slot in by.values():
            if slot.pop("tokens_unknown"):
                slot["prompt_tokens"] = None
                slot["completion_tokens"] = None
            out.append(slot)
        return out
