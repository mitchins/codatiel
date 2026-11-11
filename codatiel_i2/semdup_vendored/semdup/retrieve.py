"""M1 retrieval: independent lexical channel with attribution, plus the
M2 semantic channel.

Plus the M2 semantic channel: token overlap over deterministic
responsibility descriptions (hypotheses for search, never evidence) with
generic query expansion. All channels are independently eligible; none
vetoes another. Ordering is reciprocal rank fusion over per-channel
rankings via the shared `fusion.rrf_fuse` primitive (the M4 measured
mechanism, productized in M5.1): per-channel ranks come from FTS5 BM25
(lexical/semantic, best first) and cosine order (embedding, best
first). Results carry fused
score/rank plus per-channel ranks; trace counts are retained.
"""
from __future__ import annotations

import re
import sqlite3

from . import expand as lex
from . import fusion as _fusion

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STOP = frozenset({"def", "return", "for", "in", "if", "else", "import", "from", "None", "self"})


def significant_tokens(text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for tok in TOKEN_RE.findall(text):
        low = tok.lower()
        if low in STOP or tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= limit:
            break
    return seen


def _fts_quote(token: str) -> str:
    """Quote one token as an FTS5 phrase so operators (OR/AND/NOT, etc.)
    can never become query syntax. A query error then means a real
    failure, which is surfaced in the trace — never swallowed."""
    return '"' + token.replace('"', '""') + '"'


def _lexical_ids(conn: sqlite3.Connection, unit_id: int, k: int) -> tuple[list[int], dict]:
    """Lexical channel ranked by FTS5 BM25 (best first). Returns ids + meta.

    LIMIT k+1 compensates the excluded self-match so the operating budget
    covers up to k candidates (self is never a candidate). Documented,
    deterministic, not a recall claim.
    """
    row = conn.execute(
        "SELECT path, symbol, text FROM units WHERE id=?", (unit_id,)
    ).fetchone()
    if row is None:
        return [], {"hits": 0, "error": None}
    _, symbol, text = row
    tokens = significant_tokens(f"{symbol} {text}")
    if not tokens:
        return [], {"hits": 0, "error": None}
    query = " OR ".join(_fts_quote(t) for t in tokens)
    try:
        rows = conn.execute(
            "SELECT u.id FROM units_fts"
            " JOIN units u ON u.id = units_fts.rowid"
            " WHERE units_fts MATCH ? ORDER BY bm25(units_fts) LIMIT ?",
            (query, k + 1),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return [], {"hits": 0, "error": str(exc)}
    return [r[0] for r in rows if r[0] != unit_id][:k], {"hits": len(rows), "error": None}


def _semantic_ids(
    conn: sqlite3.Connection, unit_id: int, k: int, expand: bool = True
) -> tuple[list[int], dict]:
    """Semantic channel ranked by FTS5 BM25 over descriptions (best first).

    LIMIT k+1 compensates the excluded self-match (same documented rule
    as the lexical channel).
    """
    row = conn.execute(
        "SELECT text FROM descriptions WHERE unit_id=?", (unit_id,)
    ).fetchone()
    if row is None:
        return [], {"hits": 0, "error": "no-description"}
    tokens = significant_tokens(row[0], limit=12)
    expanded = lex.expand(tokens) if expand else tokens
    if not expanded:
        return [], {"hits": 0, "error": None}
    query = " OR ".join(_fts_quote(t) for t in expanded)
    try:
        rows = conn.execute(
            "SELECT u.id FROM desc_fts"
            " JOIN units u ON u.id = desc_fts.rowid"
            " WHERE desc_fts MATCH ? ORDER BY bm25(desc_fts) LIMIT ?",
            (query, k + 1),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return [], {"hits": 0, "error": str(exc)}
    return [r[0] for r in rows if r[0] != unit_id][:k], {"hits": len(rows), "error": None}


def _labels(conn: sqlite3.Connection, uids: list[int]) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for uid in uids:
        row = conn.execute("SELECT path, symbol FROM units WHERE id=?", (uid,)).fetchone()
        if row is not None:
            out[uid] = (row[0], row[1])
    return out


def neighbors(
    conn: sqlite3.Connection, unit_id: int, k: int = 30
) -> tuple[list[dict], dict]:
    exists = conn.execute("SELECT 1 FROM units WHERE id=?", (unit_id,)).fetchone()
    if exists is None:
        return [], {"lexical": 0,
                    "union": 0, "lexical_error": None, "rrf_k": _fusion.RRF_K}
    lex_ids, lex_meta = _lexical_ids(conn, unit_id, k)
    fused = _fusion.rrf_fuse({"lexical": lex_ids})
    labels = _labels(conn, [e["id"] for e in fused])
    ranked = []
    for entry in fused[:k]:
        path, symbol = labels.get(entry["id"], ("?", "?"))
        channels = [c for c, r in entry["channel_ranks"].items() if r is not None]
        ranked.append({"id": entry["id"], "path": path, "symbol": symbol,
                       "channels": channels, "channel_ranks": entry["channel_ranks"],
                       "rrf_score": entry["score"], "rank": entry["rank"]})
    # NOTE: per-channel LIMIT bounds these counts; the trace reports
    # fetched hits, not total matches. The final [:k] fused cut is a
    # documented operating budget, never a recall claim.
    trace = {"lexical": lex_meta["hits"],
             "union": len(ranked), "lexical_error": lex_meta["error"],
             "rrf_k": _fusion.RRF_K, "rrf_weights": {"lexical": 1.0}}
    return ranked, trace


def semantic_neighbors(
    conn: sqlite3.Connection, unit_id: int, k: int = 30, expand: bool = True
) -> tuple[list[dict], dict]:
    """Description-overlap channel with generic expansion (channel="semantic").

    Independent of lexical: a candidate found only here still
    reaches assessment. Descriptions are search hypotheses, not evidence.
    `expand=False` disables query expansion (ablation: canonicalization in
    descriptions alone still converges shared responsibility wording).
    Ordering is RRF over the single semantic channel (= its BM25 order,
    exposed with fused score/rank for trace uniformity).
    """
    sem_ids, meta = _semantic_ids(conn, unit_id, k, expand=expand)
    if meta["error"] == "no-description":
        return [], {"semantic": 0, "union": 0, "semantic_error": "no-description",
                    "rrf_k": _fusion.RRF_K}
    fused = _fusion.rrf_fuse({"semantic": sem_ids})
    labels = _labels(conn, [e["id"] for e in fused])
    ranked = [{"id": e["id"], "path": labels.get(e["id"], ("?", "?"))[0],
               "symbol": labels.get(e["id"], ("?", "?"))[1],
               "channels": ["semantic"], "channel_ranks": e["channel_ranks"],
               "rrf_score": e["score"], "rank": e["rank"]} for e in fused[:k]]
    return ranked, {"semantic": meta["hits"], "union": len(ranked),
                    "semantic_error": meta["error"], "rrf_k": _fusion.RRF_K}


def all_channel_neighbors(
    conn: sqlite3.Connection, unit_id: int, k: int = 30, emb: dict | None = None
) -> tuple[list[dict], dict]:
    """Deterministic RRF over lexical + semantic (M5.1, M5.2).

    The M4 measured mechanism, now the product ordering: per-channel
    rankings (BM25 / BM25) fused by `fusion.rrf_fuse`. Any candidate on
    any channel reaches the fused list — no veto. The final [:k] cut is
    a documented operating budget.

    The structural-fingerprint channel was removed in M5.2 after a
    measured ablation (zero unique recall, ordering-identical removal on
    all 51 gauntlet queries, one hard-negative false positive eliminated;
    suite unaffected outside the channel's own tests). Fingerprints
    remain as evidence/change-detection data (assess/check/invalidate),
    never as a retrieval signal.

    With emb=...: the deterministic RRF top-k is frozen first and
    embedding-only extras append after (truncation-safe; the M4 MUST-FIX
    guarantee, preserved pending the M5.3 weighting decision). Extras
    are ordered by embedding rank and carry full attribution.
    """
    lex_ids, lex_meta = _lexical_ids(conn, unit_id, k)
    sem_ids, sem_meta = _semantic_ids(conn, unit_id, k)
    det_lists = {"lexical": lex_ids, "semantic": sem_ids}
    det_fused = _fusion.rrf_fuse(det_lists)
    emb_ids: list[int] = []
    if emb is not None:
        from . import embed as _emb

        emb_ids = [e for e in _emb.embedding_neighbors(emb, unit_id, k)[0]]
    det_top = det_fused[:k]
    det_idset = {e["id"] for e in det_top}
    extras = [e for e in _fusion.rrf_fuse({"embedding": emb_ids})
              if e["id"] not in det_idset][:k] if emb is not None else []
    labels = _labels(conn, [e["id"] for e in det_top] + [e["id"] for e in extras])
    final = []
    for entry in det_top:
        path, symbol = labels.get(entry["id"], ("?", "?"))
        channels = [c for c, r in entry["channel_ranks"].items() if r is not None]
        if emb is not None and entry["id"] in set(emb_ids):
            channels = channels + ["embedding"]
        ranks = dict(entry["channel_ranks"])
        if emb is not None:
            ranks["embedding"] = emb_ids.index(entry["id"]) + 1 if entry["id"] in emb_ids else None
        final.append({"id": entry["id"], "path": path, "symbol": symbol,
                      "channels": channels, "channel_ranks": ranks,
                      "rrf_score": entry["score"], "rank": entry["rank"]})
    for extra in extras:
        path, symbol = labels.get(extra["id"], ("?", "?"))
        ranks = {"lexical": None, "semantic": None,
                 "embedding": extra["channel_ranks"]["embedding"]}
        final.append({"id": extra["id"], "path": path, "symbol": symbol,
                      "channels": ["embedding"], "channel_ranks": ranks,
                      "rrf_score": extra["score"], "rank": len(final) + 1})
    for i, cand in enumerate(final):
        cand["rank"] = i + 1
    trace = {"lexical": lex_meta["hits"],
             "semantic": sem_meta["hits"], "lexical_error": lex_meta["error"],
             "semantic_error": sem_meta["error"], "rrf_k": _fusion.RRF_K,
             "rrf_weights": {"lexical": 1.0, "semantic": 1.0},
             "embedding": sum(1 for c in final if "embedding" in c["channels"])
             if emb is not None else 0,
             "union_all": len(final)}
    return final, trace
