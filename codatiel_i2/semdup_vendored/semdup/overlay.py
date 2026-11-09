"""Base + branch-overlay index: transient branch state over a
persistent base without full rebuilds.

Base: store-schema sqlite + harrier/gcb npz sidecars (unchanged).
Overlay dir: overlay.sqlite (overlay_units + tombstones) + overlay
npz vectors (new/changed units only). Query view: base units minus
masked files/tombstones plus overlay rows, provenance-tagged.
FTS merges both tables; vectors concatenate base + overlay matrices.

Correctness contract (proven by test, incl. tests/test_m11_product.py):
new -> branch entry; changed -> mask base + expose branch; deleted
-> tombstone; moved/renamed -> single exposure; unchanged -> base.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from . import store as st


def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def diff_branch(base_db: Path, branch_root: Path,
                suffixes: tuple[str, ...] = (".py", ".php", ".swift")) -> dict:
    """Compare branch worktree against base files table."""
    conn = sqlite3.connect(str(base_db))
    base = {r[0]: r[1] for r in conn.execute("SELECT path, sha256 FROM files").fetchall()}
    conn.close()
    new, changed, unchanged, seen = [], [], [], set()
    for full in sorted(branch_root.rglob("*")):
        if not full.is_file() or full.suffix not in suffixes:
            continue
        rel = full.relative_to(branch_root).as_posix()
        seen.add(rel)
        try:
            sha = file_sha(full)
        except OSError:
            continue
        if rel not in base:
            new.append(rel)
        elif base[rel] != sha:
            changed.append(rel)
        else:
            unchanged.append(rel)
    deleted = [rel for rel in base if rel not in seen and rel.endswith(suffixes)]
    return {"new": new, "changed": changed, "deleted": deleted, "unchanged": unchanged}


def build_overlay(base_db: Path, branch_root: Path, overlay_dir: Path,
                  embed: bool = True, trace: bool = True) -> dict:
    """Build transient overlay. Returns stats (wall, units, vectors, disk).

    trace (SEMDUP-03 perf promotion): forwarded to the scratch index
    build; the loop is read-only and its totals are not consumed by
    overlay consumers. Default True preserves existing behavior exactly."""
    import time as _t
    from . import index_cmd as IC
    t0 = _t.monotonic()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    odb = overlay_dir / "overlay.sqlite"
    if odb.exists():
        odb.unlink()
    diff = diff_branch(base_db, branch_root)
    touched = diff["new"] + diff["changed"]
    conn = st.connect(odb)
    conn.execute("CREATE TABLE IF NOT EXISTS tombstones(path TEXT PRIMARY KEY, reason TEXT)")
    bconn = sqlite3.connect(str(base_db))
    schema_units = bconn.execute("SELECT sql FROM sqlite_master WHERE name='units'").fetchone()[0]
    conn.execute(schema_units.replace("CREATE TABLE units", "CREATE TABLE overlay_units"))
    cols = [r[1] for r in bconn.execute("PRAGMA table_info(units)").fetchall()]
    bconn.close()
    # Re-extract touched files via the product indexer into scratch.
    scratch = overlay_dir / "_scratch.sqlite"
    if scratch.exists():
        scratch.unlink()
    tmp_root = overlay_dir / "_touched"
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir()
    for rel in touched:
        dest = tmp_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(branch_root / rel, dest)
    IC.build(tmp_root, scratch, trace=trace)
    sconn = sqlite3.connect(str(scratch))
    scols = [r[1] for r in sconn.execute("PRAGMA table_info(units)").fetchall()]
    shared = [c for c in cols if c in scols]
    n_units = 0
    for row in sconn.execute("SELECT * FROM units").fetchall():
        d = dict(zip(scols, row))
        conn.execute(f"INSERT INTO overlay_units ({','.join(shared)}) "
                     f"VALUES ({','.join('?' * len(shared))})", [d[c] for c in shared])
        n_units += 1
    sconn.close()
    for rel in diff["deleted"]:
        conn.execute("INSERT OR REPLACE INTO tombstones VALUES (?, 'deleted-file')", (rel,))
    conn.commit()
    stats: dict = {"new": diff["new"], "changed": diff["changed"],
                   "deleted": diff["deleted"], "n_unchanged": len(diff["unchanged"]),
                   "n_overlay_units": n_units}
    vec_stats: dict = {}
    if embed:
        from . import encservice as ES
        texts = [r[0] for r in conn.execute("SELECT text FROM overlay_units").fetchall()]
        if texts:
            vec_stats = ES.embed_overlay(texts, overlay_dir)
    conn.close()
    stats["vectors"] = vec_stats
    stats["wall_s"] = round(_t.monotonic() - t0, 1)
    stats["overlay_disk_bytes"] = sum(f.stat().st_size for f in overlay_dir.rglob("*")
                                      if f.is_file())
    (overlay_dir / "overlay.json").write_text(json.dumps(stats, indent=1, default=str))
    return stats


class OverlayIndex:
    """Live query view over base + overlay."""

    def __init__(self, base_db: Path, overlay_dir: Path):
        self.stats = json.loads((overlay_dir / "overlay.json").read_text())
        self.masked_files = set(self.stats.get("changed", [])) | set(self.stats.get("deleted", []))
        self.base = sqlite3.connect(str(base_db))
        self.base.row_factory = sqlite3.Row
        self.over = sqlite3.connect(str(overlay_dir / "overlay.sqlite"))
        self.over.row_factory = sqlite3.Row
        self.masked_files |= {r[0] for r in
                              self.over.execute("SELECT path FROM tombstones").fetchall()}

    def lookup(self, path_like: str, symbol: str) -> tuple[dict | None, str]:
        r = self.over.execute("SELECT path, symbol, start_line, end_line, text "
                              "FROM overlay_units WHERE path LIKE ? AND symbol=?",
                              (path_like, symbol)).fetchone()
        if r:
            return dict(r), "overlay"
        r = self.base.execute("SELECT path, symbol, start_line, end_line, text "
                              "FROM units WHERE path LIKE ? AND symbol=?",
                              (path_like, symbol)).fetchone()
        if r and r["path"] not in self.masked_files:
            return dict(r), "base"
        return None, "masked"

    def live_unit_count(self) -> dict:
        b = self.base.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        m = self.base.execute(
            f"SELECT COUNT(*) FROM units WHERE path IN ({','.join('?' * len(self.masked_files))})",
            tuple(self.masked_files)).fetchone()[0] if self.masked_files else 0
        o = self.over.execute("SELECT COUNT(*) FROM overlay_units").fetchone()[0]
        return {"base": b, "masked": m, "overlay": o, "live": b - m + o}
