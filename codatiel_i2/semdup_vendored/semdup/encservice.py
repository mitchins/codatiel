"""Persistent encoder cache: process-level encoder reuse with
cold/warm accounting.

 showed encoder cold-load dominates overlay build time. This module
keeps at most one loaded encoder per channel per process and reports
cold vs warm cost explicitly, so incremental-index cost is never
confused with repeated model startup. No servers, no queues: reuse is
a process-lifetime cache with a clean reset for tests.
"""
from __future__ import annotations

import time
from pathlib import Path

_CACHE: dict[str, dict] = {}
STATS: dict[str, dict] = {"harrier": {"cold_loads": 0, "warm_uses": 0,
                                      "cold_load_s": 0.0},
                          "gcb": {"cold_loads": 0, "warm_uses": 0,
                                  "cold_load_s": 0.0}}


def reset() -> None:
    _CACHE.clear()
    for v in STATS.values():
        v.update(cold_loads=0, warm_uses=0, cold_load_s=0.0)


def get(channel: str):
    """Return a cached encoder, loading once (cold) then reusing (warm)."""
    if channel in _CACHE:
        STATS[channel]["warm_uses"] += 1
        return _CACHE[channel]["encoder"]
    t0 = time.monotonic()
    if channel == "harrier":
        from .harrier import Encoder
        enc = Encoder()
    elif channel == "gcb":
        from .embed import Encoder
        enc = Encoder()
    else:
        raise ValueError(f"unknown encoder channel: {channel}")
    STATS[channel]["cold_loads"] += 1
    STATS[channel]["cold_load_s"] = round(time.monotonic() - t0, 1)
    _CACHE[channel] = {"encoder": enc}
    return enc


def embed_overlay(texts: list[str], overlay_dir: Path) -> dict:
    """Embed overlay unit texts with persistent encoders; write npz sidecars."""
    import numpy as np
    out: dict = {}
    try:
        from .harrier import available as har_available
    except ImportError:
        har_available = lambda: False  # noqa: E731
    if har_available():
        vecs = get("harrier").encode_docs(texts)
        np.savez_compressed(overlay_dir / "overlay_harrier.npz",
                            vectors=np.asarray(vecs))
        out["harrier"] = len(texts)
    try:
        vecs = get("gcb").encode(texts)
        np.savez_compressed(overlay_dir / "overlay_gcb.npz",
                            vectors=np.asarray(vecs))
        out["gcb"] = len(texts)
    except (ImportError, RuntimeError, AttributeError) as exc:
        out["gcb_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    out["embedded"] = len(texts)
    out["encoder_stats"] = {k: dict(v) for k, v in STATS.items()}
    return out


def stats() -> dict:
    return {k: dict(v) for k, v in STATS.items()}
