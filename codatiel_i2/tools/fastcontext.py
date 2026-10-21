"""BreadthExplorer interface for FastContext-class models (§12).

Policy: retrieval engine only. Returns candidate source anchors — never a
publishable finding. Every cited candidate must be re-read through trusted
repo tools. Metrics (recall, precision@k, tokens/candidate, latency) are the
caller's responsibility.

Phase A: deterministic stub + optional HTTP backend. No endpoint configured
-> returns unavailable (never fake candidates).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BreadthResult:
    status: str  # ok | unavailable | error
    anchors: list[dict]
    tokens_used: int = 0
    latency_seconds: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "anchors": self.anchors,
                "tokens_used": self.tokens_used,
                "latency_seconds": self.latency_seconds, "error": self.error}


def explore(query: str, *, base_url: str = "", model: str = "fastcontext-8b",
            max_anchors: int = 10) -> BreadthResult:
    if not base_url:
        return BreadthResult(status="unavailable", anchors=[],
                             error="FastContext endpoint not configured")
    return BreadthResult(status="error", anchors=[],
                         error="FastContext HTTP backend not yet implemented in Phase A")
