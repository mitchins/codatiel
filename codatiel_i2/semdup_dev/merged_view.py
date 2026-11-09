"""Merged head-view index: base live rows + overlay rows, one queryable DB.

The M11 engine (`staged.investigate`) takes a single sqlite path. The
overlay's `OverlayIndex` live view is connection-level, not a file, so
the adapter materializes the equivalent merged view:

  live = base units whose path is not masked (changed/deleted/tombstoned)
       + overlay units for touched files (new + changed)
  deleted paths contribute no rows (tombstoned).

Every merged row carries provenance `(source, source_id)` so embedding
vectors from the base and overlay sidecars can be fused into one
`emb` dict keyed by merged id. Changed-file rows come from the
overlay scratch DB (branch content, branch shas), so file hashes used
for span validation are head revision hashes, not base hashes.

If either embedding sidecar is missing (or the models differ — never
silently mixed), `emb` is None and coverage is reported reduced.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

UNIT_COLS = ("path", "sha256", "language", "symbol", "kind",
             "start_line", "end_line", "text", "fingerprint", "flags",
             "truncated_regions", "refs")


def _load_npz(path: Path):
    import numpy as np
    blob = np.load(str(path), allow_pickle=True)
    return {"ids": [int(i) for i in blob["ids"]],
            "vectors": blob["vectors"], "model": str(blob["model"])}


def build_merged_view(base_db: Path, overlay_dir: Path,
                      dest_db: Path) -> dict:
    """Materialize the live head view. Returns provenance + emb + coverage."""
    from codatiel_i2.semdup_vendored.semdup import store as st
    scratch = overlay_dir / "_scratch.sqlite"
    if not scratch.exists():
        return {"ok": False, "reason": "overlay scratch DB missing; "
                "overlay is incomplete, refusing to evaluate"}
    if dest_db.exists():
        dest_db.unlink()
    bconn = sqlite3.connect(str(base_db))
    sconn = sqlite3.connect(str(scratch))
    try:
        tombstoned = {r[0] for r in
                      sqlite3.connect(str(overlay_dir / "overlay.sqlite"))
                      .execute("SELECT path FROM tombstones").fetchall()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False,
                "reason": f"cannot read overlay tombstones: {exc}"}
    try:
        touched = {r[0] for r in
                   sconn.execute("SELECT DISTINCT path FROM units").fetchall()}
        masked = set(touched) | set(tombstoned)
        conn = st.connect(dest_db)
        provenance = {}
        examinable = set()
        # Base files: unmasked only. Touched files: branch shas from scratch.
        for (path, sha, nbytes, lang, pstat) in \
                bconn.execute("SELECT path, sha256, bytes, language, parse_status FROM files"):
            if path in masked:
                continue
            st.add_file(conn, path, sha, nbytes, lang, pstat)
        for (path, sha, nbytes, lang, pstat) in \
                sconn.execute("SELECT path, sha256, bytes, language, parse_status FROM files"):
            st.add_file(conn, path, sha, nbytes, lang, pstat)
        # Base live units.
        for row in bconn.execute(
                f"SELECT id, {', '.join(UNIT_COLS)} FROM units"):
            old_id, vals = row[0], list(row[1:])
            if vals[0] in masked:
                continue
            new_id = st.add_unit(conn, *vals)
            provenance[new_id] = ["base", old_id]
            if vals[4] in ("function", "closure", "initializer"):
                examinable.add(new_id)
        # Overlay units (branch content wins for every touched file).
        for row in sconn.execute(
                f"SELECT id, {', '.join(UNIT_COLS)} FROM units"):
            old_id, vals = row[0], list(row[1:])
            new_id = st.add_unit(conn, *vals)
            provenance[new_id] = ["overlay", old_id]
            if vals[4] in ("function", "closure", "initializer"):
                examinable.add(new_id)
        # Descriptions follow their units.
        bdesc = {r[0]: (r[1], r[2]) for r in
                 bconn.execute("SELECT unit_id, text, provenance FROM descriptions")}
        sdesc = {r[0]: (r[1], r[2]) for r in
                 sconn.execute("SELECT unit_id, text, provenance FROM descriptions")}
        for new_id, (src, old_id) in provenance.items():
            table = bdesc if src == "base" else sdesc
            if old_id in table:
                text, prov = table[old_id]
                st.add_description(conn, new_id, text, prov)
        conn.commit()
        counts = {"live_units": len(provenance),
                  "base_units": sum(1 for v in provenance.values()
                                    if v[0] == "base"),
                  "overlay_units": sum(1 for v in provenance.values()
                                       if v[0] == "overlay")}
    finally:
        bconn.close()
        sconn.close()
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    emb, coverage = _fuse_embeddings(base_db, overlay_dir, provenance,
                                      examinable)
    return {"ok": True, "db": str(dest_db), "provenance": provenance,
            "counts": counts, "emb": emb, "retrieval_coverage": coverage}


def _fuse_embeddings(base_db: Path, overlay_dir: Path,
                     provenance: dict[int, list],
                     examinable: set[int]) -> tuple[dict | None, str]:
    """Fuse base + overlay sidecars into one merged-id emb dict.

    Prefers harrier; falls back to gcb only when the harrier pair is
    unusable. Different models are never mixed. Coverage is "full" only
    when every examinable (function/closure/initializer) merged unit
    has a vector; otherwise the exact shortfall is reported, never
    claimed full.
    """
    for channel in ("harrier", "gcb"):
        base_side = base_db.parent / f"{base_db.stem}.{channel}.npz"
        # Adapter-built id-keyed overlay sidecars (base sidecar format:
        # `path :: symbol` text, examinable kinds, ids saved). The
        # engine's vectors-only overlay npz files are not used: without
        # ids they cannot be mapped onto merged rows.
        over_side = overlay_dir / f"overlay_{channel}_ids.npz"
        if not base_side.exists():
            continue
        try:
            base = _load_npz(base_side)
        except Exception:  # noqa: BLE001 - unreadable sidecar, try next
            continue
        over = None
        if over_side.exists():
            try:
                over = _load_npz(over_side)
            except Exception:  # noqa: BLE001
                over = None
            if over is not None and over["model"] != base["model"]:
                over = None  # never mix models; overlay units go vectorless
        base_vec = {i: v for i, v in zip(base["ids"], base["vectors"])}
        over_vec = ({i: v for i, v in zip(over["ids"], over["vectors"])}
                    if over else {})
        have: dict[int, object] = {}
        for new_id, (src, old_id) in provenance.items():
            table = base_vec if src == "base" else over_vec
            if old_id in table:
                have[new_id] = table[old_id]
        missing = sorted(nid for nid in examinable if nid not in have)
        ids = sorted(have)
        vecs = [have[nid] for nid in ids]
        if not ids:
            continue
        import numpy as np
        emb = {"ids": ids, "vectors": np.stack(vecs),
               "model": base["model"]}
        if missing:
            return (emb,
                    f"reduced: {len(missing)} examinable merged units without "
                    f"{channel} vectors (deterministic channels cover all units)")
        return emb, "full"
    return None, ("reduced: embedding sidecars unavailable for the "
                  "merged view (deterministic retrieval only)")
