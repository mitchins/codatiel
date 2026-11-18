"""SEMDUP-SIGNAL-01 stable pair identity (deterministic, no inference).

``pair_id`` correlates one candidate examination across retrieval,
examination, tools, spend events and persisted outcomes. It is a
content-free digest: unit identities (paths/symbols, never source text
or secrets) feed the hash; the hex digest is the only thing recorded.

Direction is encoded by role order (target -> counterpart): a mirror
reverse pair swaps roles and therefore gets a different id. Rank is the
pair's position in deterministic credibility order at examination time.

pair_id is a correlation LABEL, never an identity component: it must
not enter continuation or cross-revision cache keys (rank shifts across
revisions; re-resolved anchors shift lines). Those correctness rules
are unchanged.
"""

from __future__ import annotations

import hashlib
from typing import Any

PAIR_ID_VERSION = "signal-pair-v1"


def _unit_key(unit: Any) -> str:
    if not isinstance(unit, dict):
        return ""
    return f"{unit.get('path') or ''}::{unit.get('symbol') or ''}"


def pair_id_for(pair: dict, *, repo: str, pr: Any, base_rev: str,
                head_rev: str, rank: int) -> str:
    """Deterministic pair id. Never raises; never embeds source text."""
    try:
        target = _unit_key((pair or {}).get("target"))
        counterpart = _unit_key((pair or {}).get("counterpart"))
        classification = str((pair or {}).get("classification") or "")
        body = "\n".join([
            PAIR_ID_VERSION, str(repo or ""), str(pr or ""),
            str(base_rev or ""), str(head_rev or ""),
            target, counterpart, classification, str(int(rank)),
        ])
    except Exception:
        body = f"{PAIR_ID_VERSION}\nfallback\n{rank}"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"spair-{digest}"


def stamp_pairs(pairs: list, *, repo: str, pr: Any, base_rev: str,
                head_rev: str) -> list:
    """Attach pair_rank + pair_id in place. Order-preserving (no reorder)."""
    for rank, pair in enumerate(pairs or []):
        if not isinstance(pair, dict):
            continue
        try:
            pair["pair_rank"] = rank
            if not pair.get("pair_id"):
                pair["pair_id"] = pair_id_for(
                    pair, repo=repo, pr=pr, base_rev=base_rev,
                    head_rev=head_rev, rank=rank)
        except Exception:
            continue
    return pairs
