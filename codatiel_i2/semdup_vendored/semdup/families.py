"""Candidate family formation: pairs grown only with full pairwise support.

Policy: families are responsibility hypotheses, not disjoint equivalence
classes. Candidate retrieval is pairwise. A family grows beyond two
members only when every additional member pairs with EVERY existing
member (clique growth — pairwise support in all directions, never
transitive merging through a hub, never connected components). The
assessment then confirms all members against one proposed responsibility
(`member_notes` covers every member); a grown family whose assessment
fails validation falls back to its pairs. Overlapping families survive:
growth drops subsumed pairs except pairs bridging two grown families.
"""
from __future__ import annotations

import sqlite3

from . import retrieve as ret

FUNCTION_KINDS = ("function", "closure", "initializer")


def _spans(conn: sqlite3.Connection) -> dict[int, tuple[str, int, int]]:
    return {uid: (path, sl, el) for uid, path, sl, el in conn.execute(
        "SELECT id, path, start_line, end_line FROM units WHERE kind IN "
        "('function','closure','initializer')")}


def _nested(spans: dict[int, tuple[str, int, int]], a: int, b: int) -> bool:
    """Same-file strict span containment: a closure/region inside its own
    function (or vice versa) is not an independent implementation of it.
    Helpers belong in support, never as co-members with their caller."""
    pa, sa, ea = spans[a]
    pb, sb, eb = spans[b]
    if pa != pb:
        return False
    return (sa, ea) != (sb, eb) and sa <= sb and ea >= eb or \
        (sa, ea) != (sb, eb) and sb <= sa and eb >= ea


def form_families(
    conn: sqlite3.Connection, neighbors_k: int = 30, top_per_unit: int = 5
) -> list[tuple[list[int], list[str]]]:
    """Return [([id_a, id_b], sorted_channels)] over function-level units."""
    units = [r[0] for r in conn.execute(
        "SELECT id FROM units WHERE kind IN ('function','closure','initializer') ORDER BY id")]
    by_id = set(units)
    spans = _spans(conn)
    seen: set[frozenset] = set()
    families: list[tuple[list[int], list[str]]] = []
    for uid in units:
        ranked, _ = ret.all_channel_neighbors(conn, uid, neighbors_k)
        for cand in ranked[:top_per_unit]:
            if cand["id"] not in by_id or cand["id"] == uid:
                continue
            if _nested(spans, uid, cand["id"]):
                continue
            key = frozenset({uid, cand["id"]})
            if key in seen:
                continue
            seen.add(key)
            families.append(([uid, cand["id"]], sorted(set(cand["channels"]))))
    families.sort(key=lambda f: (len(f[1]), min(f[0])), reverse=True)
    return families


def grow(pairs: list[tuple[list[int], list[str]]], max_size: int = 3) -> list[tuple[list[int], list[str]]]:
    """Grow pairs into multi-member families with full pairwise support.

    Deterministic (sorted id order, first fit: each unit joins at most one
    grown family). Pairs never subsumed stand as-is; subsumed pairs survive
    only as bridges between two different grown families (overlap).
    """
    adjacency: dict[int, set[int]] = {}
    channels: dict[frozenset, list[str]] = {}
    for (a, b), ch in pairs:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
        key = frozenset({a, b})
        channels[key] = sorted(set(channels.get(key, [])) | set(ch))
    grown: list[tuple[list[int], list[str]]] = []
    claimed: set[int] = set()
    for (a, b), _ in sorted(pairs, key=lambda f: (min(f[0]), max(f[0]))):
        if a in claimed or b in claimed:
            continue
        members = [a, b]
        common = adjacency.get(a, set()) & adjacency.get(b, set()) - {a, b}
        for candidate in sorted(common):
            if len(members) >= max_size or candidate in claimed:
                continue
            if all(candidate in adjacency.get(m, set()) for m in members):
                members.append(candidate)
        if len(members) > 2:
            claimed.update(members)
            merged: list[str] = []
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    merged = sorted(set(merged) | set(channels.get(frozenset({members[i], members[j]}), [])))
            grown.append((sorted(members), merged))
    grown_sets = [set(m) for m, _ in grown]
    subsumed = set()
    for members, _ in grown:
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                subsumed.add(frozenset({members[i], members[j]}))
    out = list(grown)
    for (a, b), ch in pairs:
        if frozenset({a, b}) not in subsumed:
            out.append(([a, b], ch))
            continue
        a_sets = [s for s in grown_sets if a in s]
        b_sets = [s for s in grown_sets if b in s]
        if any(s is not t for s in a_sets for t in b_sets):
            out.append(([a, b], ch))
    out.sort(key=lambda f: (len(f[0]), min(f[0])), reverse=True)
    return out


def focused_families(
    conn: sqlite3.Connection, unit_ids: list[int], neighbors_k: int = 10
) -> list[tuple[list[int], list[str]]]:
    """Directed retrieval: each listed unit queried against the whole index.

    This is the changed-vs-whole search (plus changed-vs-changed wherever
    both members are touched): every touched unit gets retrieval attention
    with its own budget, independent of whole-head ranking truncation.
    Same pair-only, no-merge policy as form_families.
    """
    by_id = {r[0] for r in conn.execute(
        "SELECT id FROM units WHERE kind IN ('function','closure','initializer')")}
    spans = _spans(conn)
    seen: set[frozenset] = set()
    families: list[tuple[list[int], list[str]]] = []
    for uid in unit_ids:
        if uid not in by_id:
            continue
        ranked, _ = ret.all_channel_neighbors(conn, uid, neighbors_k)
        for cand in ranked:
            if cand["id"] not in by_id or cand["id"] == uid:
                continue
            if _nested(spans, uid, cand["id"]):
                continue
            key = frozenset({uid, cand["id"]})
            if key in seen:
                continue
            seen.add(key)
            families.append(([uid, cand["id"]], sorted(set(cand["channels"]))))
    families.sort(key=lambda f: (len(f[1]), min(f[0])), reverse=True)
    return families
