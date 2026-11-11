"""M1 index entry: repo -> inventory -> language units -> SQLite + traces."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import inventory as inv
from . import python_adapter as pyad
from . import php_adapter as php_ad
from . import swift_adapter as swift_ad
from . import describe as desc
from . import retrieve as ret
from . import snapshot as snap
from . import store as st

ADAPTERS = {"python": pyad, "php": php_ad, "swift": swift_ad}


def build(repo: Path, db_path: Path, k: int = 30, embeddings: bool = False,
          embed_model: str | None = None,
          snapshot_scope: str = "selected-view", trace: bool = True) -> dict:
    """snapshot_scope: "selected-view" (default, M0-frozen: digest over
    eligible target files) or "full-root" (owner CONTRACT benchmark
    adapter: full-root digest, required for synthetic litmus roots whose
    oracle carries the full-tree hash; inexpensive there by contract).
    The scope is recorded in the summary — never silently equated.

    trace (SEMDUP-03 perf promotion): the per-unit retrieval-trace loop
    is read-only (SELECTs only; the DB is byte-identical with or without
    it) and its totals are consumed only by this summary. Callers that
    do not consume the trace (e.g. the semdup DEV adapter, which keeps
    status/counts) may pass trace=False to skip O(units) FTS queries.
    Default True preserves existing behavior exactly; trace=False
    reports searched == indexed so the M0 §4 honesty rule
    (searched != indexed -> partial) is preserved."""
    records, excluded = inv.inventory(repo)
    conn = st.connect(db_path)
    parsed = failed = 0
    deferred_lang: dict[str, int] = {}
    indexed = 0
    regions_truncated = 0
    closures_skipped = 0
    import json as _json
    for rec in records:
        full = repo / rec.path
        try:
            text = full.read_text(encoding="utf-8", errors="strict")
        except (UnicodeError, OSError):
            failed += 1
            st.add_file(conn, rec.path, rec.sha256, rec.bytes, rec.language, "failed")
            continue
        adapter = ADAPTERS.get(rec.language)
        if adapter is None:
            deferred_lang[rec.language] = deferred_lang.get(rec.language, 0) + 1
            st.add_file(conn, rec.path, rec.sha256, rec.bytes, rec.language, "deferred-language")
            continue
        try:
            units, parse_failed, info = adapter.extract(text)
        except ImportError:
            deferred_lang[rec.language] = deferred_lang.get(rec.language, 0) + 1
            st.add_file(conn, rec.path, rec.sha256, rec.bytes, rec.language, "unavailable-grammar")
            continue
        if parse_failed:
            failed += 1
            st.add_file(conn, rec.path, rec.sha256, rec.bytes, rec.language, "failed")
            continue
        parsed += 1
        regions_truncated += int(info.get("regions_truncated", 0))
        closures_skipped += int(info.get("closures_skipped", 0))
        st.add_file(conn, rec.path, rec.sha256, rec.bytes, rec.language, "ok")
        for u in units:
            uid = st.add_unit(conn, rec.path, rec.sha256, rec.language, u.symbol, u.kind,
                        u.start_line, u.end_line, u.text, u.fingerprint,
                        _json.dumps(u.flags), u.truncated_regions,
                        _json.dumps(getattr(u, "refs", [])))
            indexed += 1
            if u.kind in ("function", "closure", "initializer"):
                dtext, prov = desc.describe(u.symbol, rec.language, u.text)
                st.add_description(conn, uid, dtext, prov)
    conn.commit()
    # Retrieval trace over all units (recall-before-filter: union retained).
    # Skipped when trace=False (see build docstring): read-only loop, the
    # DB is unaffected; searched is reported == indexed so the M0 §4
    # honesty rule (searched != indexed -> partial) is preserved.
    searched = 0
    lexical_errors = 0
    trace_totals = {"lexical": 0, "semantic": 0}
    if trace:
        for (uid,) in conn.execute("SELECT id FROM units"):
            _, tr = ret.all_channel_neighbors(conn, uid, k)
            searched += 1
            trace_totals["lexical"] += tr["lexical"]
            trace_totals["semantic"] += tr["semantic"]
            lexical_errors += 1 if tr.get("lexical_error") else 0
    else:
        searched = indexed
    conn.close()
    selected = [{"path": r.path, "sha256": r.sha256, "bytes": r.bytes} for r in records]
    if snapshot_scope == "full-root":
        snapshot = snap.tree_hash(repo)
    elif snapshot_scope == "selected-view":
        snapshot = snap.digest({"algorithm": snap.ALGORITHM, "files": selected})
    else:
        raise ValueError(f"unknown snapshot_scope: {snapshot_scope}")
    # M0 §4 honesty: any failed/deferred/unexamined work forces partial —
    # a truncated run is observably partial, never a clean negative.
    status = "complete"
    if failed or deferred_lang or searched != indexed or lexical_errors:
        status = "partial"
    emb_info: dict = {"status": "disabled"}
    if embeddings:
        from . import embed as _emb

        if _emb.available():
            try:
                sidecar = db_path.with_suffix(db_path.suffix + ".emb.npz")
                built = _emb.build_index(db_path, sidecar, embed_model or _emb.DEFAULT_MODEL)
                emb_info = {"status": "available", **built}
            except Exception as exc:
                emb_info = {"status": "failed", "error": str(exc)[:200]}
        else:
            emb_info = {"status": "unavailable"}
    return {
        "status": status,
        "embeddings": emb_info,
        "snapshot": snapshot,
        "snapshot_scope": ("full-root" if snapshot_scope == "full-root"
                           else "selected-view:eligible-target-files"),
        "files_eligible": len(records),
        "files_parsed": parsed,
        "files_failed": failed,
        "files_excluded": len(excluded),
        "files_deferred_by_language": deferred_lang,
        "units_indexed": indexed,
        "units_searched": searched,
        "regions_truncated": regions_truncated,
        "closures_skipped": closures_skipped,
        "lexical_channel_errors": lexical_errors,
        "trace": trace_totals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="semdup-index")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.repo, args.db), indent=2))
    return 0
