"""Fixed-point money for the platform ledger: integer micro-USD.

Floats must never drive admission or settlement: every cap, reservation
and settlement is an integer count of micro-dollars (1 USD = 1_000_000).
Display conversion rounds to cents half away from zero only at the edge.
"""

from __future__ import annotations

import math

MICRO_PER_USD = 1_000_000


def to_micro(usd: float) -> int:
    """USD float -> micro-USD int. Rejects NaN/inf/negatives."""
    if not isinstance(usd, (int, float)) or isinstance(usd, bool):
        raise ValueError(f"amount must be numeric, got {type(usd).__name__}")
    if math.isnan(usd) or math.isinf(usd):
        raise ValueError(f"amount must be finite, got {usd!r}")
    if usd < 0:
        raise ValueError(f"amount must be >= 0, got {usd!r}")
    return int(round(usd * MICRO_PER_USD))


def to_usd(micro: int) -> float:
    """Micro-USD int -> USD float for display/legacy interop."""
    if not isinstance(micro, int) or isinstance(micro, bool):
        raise ValueError(f"micro amount must be int, got {micro!r}")
    return micro / MICRO_PER_USD


def fmt_usd(micro: int) -> str:
    """Display string rounded to cents, e.g. 390696 -> '$0.39'."""
    cents = (abs(micro) * 100 + MICRO_PER_USD // 2) // MICRO_PER_USD
    sign = "-" if micro < 0 else ""
    return f"{sign}${cents // 100}.{cents % 100:02d}"
