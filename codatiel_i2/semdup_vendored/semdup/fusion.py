"""Reciprocal rank fusion: the single ordering primitive.

Shared by the product retrieval path (`retrieve.all_channel_neighbors`,
`retrieve.neighbors`) and the qualification harnesses — one tested
formula, never duplicated. Deterministic and parameter-light:

    score(d) = sum over channels of w_ch / (k + rank_ch(d))

No channel vetoes another: any candidate present on any channel appears
in the fused list (union membership is never removed). The final top-k
cut is a documented operating budget (M0 contract leaves ranking/k
unfrozen), never a recall claim. Tie-break is unit id ascending —
fixed and documented.

Weights default to 1.0 (equal-weight RRF, the M4 measured mechanism).
Non-unit weights are an explicit M5.3 tuning instrument: any tuned
weight set must be recorded with its tuning data and scope, never
silently adopted.
"""
from __future__ import annotations

RRF_K = 60


def rrf_fuse(
    ranked_lists: dict[str, list[int]],
    k: int = RRF_K,
    weights: dict[str, float] | None = None,
) -> list[dict]:
    """Fuse per-channel ranked id lists.

    Returns [{id, score, rank, channel_ranks:{ch: 1-based rank or None}}]
    ordered by (score desc, id asc). Ranks are 0-based positions in the
    input lists (rank 0 = best). Channels absent from `weights` default
    to 1.0; a zero weight mutes a channel without deleting its members
    from other channels' contributions.
    """
    weights = weights or {}
    scores: dict[int, float] = {}
    positions: dict[int, dict[str, int]] = {}
    for channel, lst in ranked_lists.items():
        w = float(weights.get(channel, 1.0))
        for pos, uid in enumerate(lst):
            if w:
                scores[uid] = scores.get(uid, 0.0) + w / (k + pos)
            positions.setdefault(uid, {})[channel] = pos + 1
    channels = list(ranked_lists)
    fused = [
        {"id": uid,
         "score": scores.get(uid, 0.0),
         "channel_ranks": {c: positions[uid].get(c) for c in channels}}
        for uid in scores.keys() | positions.keys()
    ]
    fused.sort(key=lambda e: (-e["score"], e["id"]))
    for rank, entry in enumerate(fused, 1):
        entry["rank"] = rank
    return fused
