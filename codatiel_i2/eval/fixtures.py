"""Eval fixtures contract: ground truth is detector-typed (§3J rewrite)."""

from __future__ import annotations

REQUIRED_KEYS = ("detector", "invariant", "changed_anchor", "counterpart",
                 "trigger", "bad_outcome", "proof_level")


def validate_fixture(row: dict) -> list[str]:
    return [f"missing {k}" for k in REQUIRED_KEYS if k not in row]
