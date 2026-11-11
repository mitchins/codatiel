"""Content-addressed embedding vector cache (promoted SEMDUP-02 Track A).

Key insight (measured): changing one function re-embeds every examinable
unit in every touched file, and every sidecar build cold-loads the
encoder. Both are pure waste: encoder output is a pure function of
(channel, encoder id, embedding profile, input text).

Cache key = sha256(CACHE_FORMAT, channel, model id, embedding profile,
embedding input text) where the input text is byte-identical to what the
vendored builders encode (`path :: symbol\\ntext`). A cache hit therefore
implies a byte-identical encoder input. Note on exactness: hits reuse the
vector produced for that exact input; vectors for *different* inputs are
always freshly encoded, but batch composition can shift float noise
(measured max abs drift 2.4e-3, mean 2.6e-5 on one 11-unit sidecar —
near-tie retrieval orderings should be treated as approximate, not exact).

Storage: one sqlite per (channel, model) under
{state_dir}/semdup/emb_cache/. Sidecars written are schema-identical to
the vendored builders' output (same npz keys), so merged_view fusion is
untouched. On any cache infrastructure failure the builder falls back to
the direct vendored build — the cache is an optimization, never a new
failure mode. Encoder instances come from the vendored encservice
process-level cache (warm reuse across base + overlay builds in one
process) instead of a fresh cold load per sidecar.

Eviction (SEMDUP-03): the cache is bounded (MAX_CACHE_VECTORS vectors,
MAX_CACHE_BYTES total blob bytes per cache DB). Least-recently-used keys
(by last_used timestamp, updated on hit and insert) are evicted first.
Eviction is deterministic for a given access order. No prompts or models
are touched.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

CACHE_FORMAT = 1

# Deterministic size bound: LRU eviction keeps the cache under these.
MAX_CACHE_VECTORS = 200_000
MAX_CACHE_BYTES = 2_147_483_648  # 2 GiB of stored vectors per cache DB
_EVICT_BATCH = 500


def _profile(channel: str, model: str) -> str:
    if channel == "harrier":
        from codatiel_i2.semdup_vendored.semdup import harrier as HAR
        return (f"harrier|{model}|{HAR.QUERY_INSTRUCTION}|"
                f"{HAR.MAX_TOKENS}|last-token-norm")
    from codatiel_i2.semdup_vendored.semdup import embed as GCB
    return f"gcb|{model}|mean-pool|{GCB.MAX_TOKENS}|batch{GCB.BATCH}"


def content_key(channel: str, model: str, text: str) -> str:
    body = json.dumps([CACHE_FORMAT, _profile(channel, model), text],
                      sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def cache_path(state_dir: Path, channel: str, model: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    return state_dir / "semdup" / "emb_cache" / f"{channel}__{slug}.sqlite"


def _read_rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, path, symbol, text FROM units WHERE kind IN "
            "('function','closure','initializer') ORDER BY id").fetchall()
    finally:
        conn.close()


def _direct(channel: str, db_path: Path, out_path: Path, model: str,
            perf=None) -> dict:
    """Today's behavior, verbatim: full fresh encode, no cache."""
    if channel == "harrier":
        from codatiel_i2.semdup_vendored.semdup import harrier as HAR
        with perf.stage("embed_harrier_direct") if perf else _noop():
            report = HAR.build_index(db_path, out_path, model)
    else:
        from codatiel_i2.semdup_vendored.semdup import embed as GCB
        with perf.stage("embed_gcb_direct") if perf else _noop():
            report = GCB.build_index(db_path, out_path, model)
    report = dict(report)
    report.update(reused=0, embedded=int(report.get("units", 0)),
                  cache_hits=0, cache_misses=int(report.get("units", 0)),
                  cached=False)
    return report


class _noop:
    def __enter__(self): return None
    def __exit__(self, *a): return False


def _encode_misses(channel: str, model: str, texts: list[str],
                   perf=None):
    """Encode via the vendored process-cached encoder (warm after first use)."""
    from codatiel_i2.semdup_vendored.semdup import encservice as ES
    if channel == "harrier":
        from codatiel_i2.semdup_vendored.semdup import harrier as HAR
        if model != HAR.MODEL_ID:
            raise ValueError(f"uncached harrier model {model!r}")
        stage = "embed_harrier_encode"
        with perf.stage(stage) if perf else _noop():
            enc = ES.get("harrier")
            t0 = time.monotonic()
            vecs = enc.encode_docs(texts)
            wall = time.monotonic() - t0
    else:
        from codatiel_i2.semdup_vendored.semdup import embed as GCB
        if model != GCB.DEFAULT_MODEL:
            raise ValueError(f"uncached gcb model {model!r}")
        stage = "embed_gcb_encode"
        with perf.stage(stage) if perf else _noop():
            enc = ES.get("gcb")
            t0 = time.monotonic()
            vecs = enc.encode(texts)
            wall = time.monotonic() - t0
    return vecs, wall


def _ensure_schema(cconn: sqlite3.Connection) -> None:
    cconn.execute("CREATE TABLE IF NOT EXISTS vecs"
                  "(key TEXT PRIMARY KEY, dim INT NOT NULL, vec BLOB NOT NULL,"
                  " last_used REAL NOT NULL DEFAULT 0)")
    cols = [r[1] for r in cconn.execute("PRAGMA table_info(vecs)")]
    if "last_used" not in cols:
        cconn.execute("ALTER TABLE vecs ADD COLUMN last_used REAL NOT NULL"
                      " DEFAULT 0")


def _enforce_bound(cconn: sqlite3.Connection) -> int:
    """Evict least-recently-used keys while over budget. Returns evicted.

    Deletes only the excess (plus a small batch under bytes pressure),
    re-checking after each batch so the cache lands just under budget
    instead of being emptied.
    """
    evicted = 0
    while True:
        n, total = cconn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(vec)),0) FROM vecs"
        ).fetchone()
        if n <= 0:
            break
        if n <= MAX_CACHE_VECTORS and total <= MAX_CACHE_BYTES:
            break
        excess = max(n - MAX_CACHE_VECTORS, 1)
        if total > MAX_CACHE_BYTES and excess < _EVICT_BATCH:
            excess = min(_EVICT_BATCH, n)
        rows = cconn.execute(
            "SELECT key FROM vecs ORDER BY last_used ASC LIMIT ?",
            (excess,)).fetchall()
        if not rows:
            break
        cconn.execute(
            f"DELETE FROM vecs WHERE key IN ({','.join('?' * len(rows))})",
            [r[0] for r in rows])
        evicted += len(rows)
    return evicted


def cache_stats(state_dir: Path, channel: str, model: str) -> dict:
    """Inspect the cache without modifying it (tests/diagnostics)."""
    cpath = cache_path(state_dir, channel, model)
    if not cpath.exists():
        return {"exists": False, "vectors": 0, "bytes": 0}
    conn = sqlite3.connect(str(cpath))
    try:
        _ensure_schema(conn)
        n, total = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(vec)),0) FROM vecs"
        ).fetchone()
        return {"exists": True, "vectors": n, "bytes": total,
                "max_vectors": MAX_CACHE_VECTORS,
                "max_bytes": MAX_CACHE_BYTES}
    finally:
        conn.close()


def build_sidecar(db_path: Path, out_path: Path, *, channel: str,
                  model: str, state_dir: Path, perf=None) -> dict:
    """Build an id-keyed embedding sidecar, reusing cached unit vectors.

    Returns a build report with reused/embedded split and cache hit/miss
    counters (wired into PerfLog by the caller via `perf`).
    """
    import numpy as np
    rows = _read_rows(db_path)
    if not rows:
        return _direct(channel, db_path, out_path, model, perf)
    texts = [f"{path} :: {symbol}\n{text}" for _, path, symbol, text in rows]
    ids = [r[0] for r in rows]
    keys = [content_key(channel, model, t) for t in texts]
    cpath = cache_path(state_dir, channel, model)
    evicted = 0
    try:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cconn = sqlite3.connect(str(cpath))
        _ensure_schema(cconn)
        now = time.time()
        have: dict[str, object] = {}
        for i in range(0, max(1, len(keys)), 500):
            chunk = keys[i:i + 500]
            for k, d, v in cconn.execute(
                    f"SELECT key, dim, vec FROM vecs WHERE key IN "
                    f"({','.join('?' * len(chunk))})", chunk):
                have[k] = (d, v)
        miss_idx = [i for i, k in enumerate(keys) if k not in have]
        hits = len(keys) - len(miss_idx)
        misses = len(miss_idx)
        encode_wall = 0.0
        if miss_idx:
            vecs, encode_wall = _encode_misses(
                channel, model, [texts[i] for i in miss_idx], perf)
            vecs = np.asarray(vecs, dtype="float32")
            cconn.executemany(
                "INSERT OR REPLACE INTO vecs(key, dim, vec, last_used)"
                " VALUES(?,?,?,?)",
                [(keys[i], int(vecs[r].shape[0]), vecs[r].tobytes(), now)
                 for r, i in enumerate(miss_idx)])
            for r, i in enumerate(miss_idx):
                have[keys[i]] = (int(vecs[r].shape[0]), vecs[r].tobytes())
        if hits:
            hit_keys = [k for k in keys if k in have and k not in
                        {keys[i] for i in miss_idx}]
            for i in range(0, max(1, len(hit_keys)), 500):
                chunk = hit_keys[i:i + 500]
                if chunk:
                    cconn.execute(
                        f"UPDATE vecs SET last_used=? WHERE key IN "
                        f"({','.join('?' * len(chunk))})",
                        [now, *chunk])
        cconn.commit()
        evicted = _enforce_bound(cconn)
        cconn.commit()
        ordered = [np.frombuffer(have[k][1], dtype="float32") for k in keys]
        mat = np.stack(ordered)
        cconn.close()
    except Exception as exc:  # noqa: BLE001 - cache never fails the build
        if perf is not None:
            perf.count("emb_cache_errors")
        direct = _direct(channel, db_path, out_path, model, perf)
        direct["cache_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return direct
    dim = int(mat.shape[1])
    sha = hashlib.sha256(
        "\n".join(f"{i}:{t[:200]}" for i, t in zip(ids, texts)).encode()
    ).hexdigest()
    if channel == "harrier":
        from codatiel_i2.semdup_vendored.semdup import harrier as HAR
        np.savez_compressed(out_path, ids=np.array(ids, dtype=np.int64),
                            vectors=mat.astype("float32"),
                            model=np.array(model),
                            instruction=np.array(HAR.QUERY_INSTRUCTION),
                            sha=np.array(sha))
    else:
        np.savez_compressed(out_path, ids=np.array(ids, dtype=np.int64),
                            vectors=mat.astype("float32"),
                            model=np.array(model), sha=np.array(sha))
    if perf is not None:
        perf.count("emb_cache_hit", hits)
        perf.count("emb_cache_miss", misses)
        perf.count("emb_units_reused", hits)
        perf.count("emb_units_embedded", misses)
        if evicted:
            perf.count("emb_cache_evicted", evicted)
    from codatiel_i2.semdup_vendored.semdup import encservice as ES
    return {"model": model, "dim": dim, "units": len(ids),
            "reused": hits, "embedded": misses,
            "bytes": out_path.stat().st_size,
            "encode_wall_s": round(encode_wall, 1),
            "cache_hits": hits, "cache_misses": misses, "cached": True,
            "cache_evicted": evicted,
            "encoder_stats": ES.stats()}
