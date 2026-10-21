"""Breadth interface for FrogNano-class models (§12). Same policy as FastContext."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BreadthResult:
    status: str
    anchors: list[dict]
    tokens_used: int = 0
    latency_seconds: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "anchors": self.anchors,
                "tokens_used": self.tokens_used,
                "latency_seconds": self.latency_seconds, "error": self.error}


def explore(query: str, *, base_url: str = "", model: str = "frognano-4b",
            max_anchors: int = 10) -> BreadthResult:
    if not base_url:
        return BreadthResult(status="unavailable", anchors=[],
                             error="FrogNano endpoint not configured")
    return BreadthResult(status="error", anchors=[],
                         error="FrogNano HTTP backend not yet implemented in Phase A")
