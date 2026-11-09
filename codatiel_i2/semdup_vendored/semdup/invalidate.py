"""Dependency invalidation: conservative reverse-dependency reopening.

A changed callee, imported constant, or deleted symbol invalidates verdicts
about callers whose bodies did not change. Where relationships cannot be
resolved precisely, a wider documented scope is invalidated (symbol-name
matching repo-wide) rather than falsely reusing a semantic verdict.

Inputs are unit maps {key: {fingerprint, refs[]}}; keys are
(path, symbol) tuples. Outputs the reexamine set of head-unit keys.
"""
from __future__ import annotations


def _content(unit: dict) -> tuple:
    # Fingerprints abstract names and literal values by design, so a
    # value-only edit (e.g. `*2` -> `*3`) is invisible to them. Change
    # detection compares text as well: missing a real edit would falsely
    # reuse a stale verdict, while the reverse only widens reexamination.
    return (unit.get("fingerprint"), unit.get("text"))


def changed_symbols(base: dict, head: dict) -> set[str]:
    """Symbol names whose content changed, appeared, or disappeared."""
    changed: set[str] = set()
    base_by_sym: dict[str, list] = {}
    for (path, symbol), unit in base.items():
        base_by_sym.setdefault(symbol, []).append(_content(unit))
    head_by_sym: dict[str, list] = {}
    for (path, symbol), unit in head.items():
        head_by_sym.setdefault(symbol, []).append(_content(unit))
    for symbol in set(base_by_sym) | set(head_by_sym):
        if sorted(map(str, base_by_sym.get(symbol, []))) != sorted(map(str, head_by_sym.get(symbol, []))):
            changed.add(symbol)
    return changed


def reexamine(head: dict, changed: set[str]) -> set[tuple[str, str]]:
    """Head-unit keys whose refs intersect the changed set (wider scope:
    matched by symbol name repo-wide, documented over-approximation)."""
    out: set[tuple[str, str]] = set()
    for key, unit in head.items():
        if set(unit.get("refs", [])) & changed:
            out.add(key)
    return out
