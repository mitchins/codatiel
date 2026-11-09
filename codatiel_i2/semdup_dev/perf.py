"""Lightweight deterministic performance instrumentation (promoted SEMDUP-02).

Stdlib-only, exception-free by design: timers use time.monotonic, memory
uses resource.getrusage (peak RSS) plus torch CUDA peak when available.
No I/O, no model calls, no network. A PerfLog is threaded through the
adapter path (index_state / worker) and summarized into base meta.json,
overlay records, and run.json — never into findings.

Counters use stable names so before/after runs can be compared:
  stage wall time:  perf["stages"][<stage>] (seconds, accumulated)
  cache:            perf["counters"]["emb_cache_hit/miss"],
                    "emb_units_reused/embedded", "emb_cache_evicted"
  retrieval:        "retrieval_candidates" (union size per investigate)
  model usage:      "model_calls" (stage-B turns + arbiter + repair),
                    "completion_tokens", "tool_calls", "investigate_wall_s"
  resources:        "peak_rss_mb", "peak_cuda_mb" (None when unavailable)
"""

from __future__ import annotations

import time
from collections import Counter
from contextlib import contextmanager
from typing import Iterator


class PerfLog:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.stages: dict[str, float] = {}
        self.counters: Counter = Counter()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (
                time.monotonic() - start)

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def note_investigation(self, rep: dict) -> None:
        """Aggregate per-target report accounting (read-only, no schema change).

        Only keys the staged engine actually emits are read: stage-B cards
        carry n_tool_calls/loop_wall_s (per-turn request counts are not
        propagated, so per-area model calls are reported as tool_calls;
        the arbiter accounts exactly one call per target).
        """
        try:
            cards = rep.get("stage_b") or []
            self.count("stage_b_areas", len(cards))
            for c in cards:
                self.count("tool_calls", int(c.get("n_tool_calls") or 0))
            sc = rep.get("stage_c") or {}
            self.count("completion_tokens",
                       int(sc.get("completion_tokens") or 0))
            self.count("model_calls", 1)  # exactly one arbiter call per target
            det = rep.get("stage_a") or {}
            self.count("retrieval_candidates", int(det.get("n_union") or 0))
            wall = rep.get("wall_s")
            if isinstance(wall, (int, float)):
                self.count("investigate_wall_s_x10", int(wall * 10))
        except (TypeError, ValueError, AttributeError):
            pass  # instrumentation never breaks the run

    def summary(self) -> dict:
        return {
            "stages": {k: round(v, 2) for k, v in sorted(self.stages.items())},
            "counters": dict(sorted(self.counters.items())),
            "wall_s": round(time.monotonic() - self.t0, 1),
            "peak_rss_mb": _peak_rss_mb(),
            "peak_cuda_mb": _peak_cuda_mb(),
        }


def _peak_rss_mb() -> float | None:
    try:
        import resource
        # Linux ru_maxrss is kilobytes.
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (ImportError, OSError, ValueError):
        return None


def _peak_cuda_mb() -> float | None:
    try:
        import torch
        if torch.cuda.is_available():
            return round(torch.cuda.max_memory_allocated() / (1 << 20), 1)
    except (ImportError, RuntimeError, AttributeError):
        pass
    return None
