"""Reranker arm: plausibility ordering over a FROZEN candidate pool.

Both rerankers receive byte-identical pool files (hash recorded); no
retrieval happens inside reranking. Output is an ordering only: unknown
ids are dropped with counts, duplicates collapsed, missing ids appended
in input order. Malformed output falls back to input order entirely
(fail-closed, counted) — a broken rerank never loses candidates, it
just contributes no signal. No equivalence, action, or safety claims;
no source mutation; no oracle contact.
"""
from __future__ import annotations

import hashlib
import json


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def render_prompt(query: dict, pool: list[dict], prompt_text: str,
                  excerpt_chars: int = 1500) -> str:
    lines = [prompt_text.strip(), "",
             f"## Query unit: {query['path']}:{query['symbol']} "
             f"(lines {query['start_line']}-{query['end_line']})",
             "```", query["text"][:excerpt_chars], "```",
             f"description: {query.get('description', '')}", "",
             f"## Candidates (frozen pool of {len(pool)})"]
    for i, cand in enumerate(pool, 1):
        lines += [f"### [{i}] {cand['path']}:{cand['symbol']} "
                  f"(lines {cand['start_line']}-{cand['end_line']}, "
                  f"found-by: {','.join(cand.get('channels', []))})",
                  "```", cand["text"][:excerpt_chars], "```",
                  f"description: {cand.get('description', '')}", ""]
    lines += ['Respond with exactly one JSON object: {"ranking": [{"id": int, '
              '"reason": str}]} listing every id 1..%d best-first. No prose.' % len(pool)]
    return "\n".join(lines)


def parse_ranking(content: str, n: int) -> tuple[list[int], dict]:
    """Return (ordered 1-based ids, stats). Fail-soft on entries, fail-closed overall."""
    import re as _re

    stats = {"unknown_ids": 0, "duplicates": 0, "missing_appended": 0, "malformed": False}
    text = content
    fence = _re.search(r"```(?:json)?\s*\n(.*?)```", content, _re.S)
    if fence:
        text = fence.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        stats["malformed"] = True
        return list(range(1, n + 1)), stats
    if not isinstance(value, dict) or not isinstance(value.get("ranking"), list):
        stats["malformed"] = True
        return list(range(1, n + 1)), stats
    ordered: list[int] = []
    for entry in value["ranking"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
            continue
        if entry["id"] < 1 or entry["id"] > n:
            stats["unknown_ids"] += 1
            continue
        if entry["id"] in ordered:
            stats["duplicates"] += 1
            continue
        ordered.append(entry["id"])
    missing = [i for i in range(1, n + 1) if i not in ordered]
    stats["missing_appended"] = len(missing)
    return ordered + missing, stats


def pool_hash(pool: list[dict]) -> str:
    return _sha(json.dumps(pool, sort_keys=True))
