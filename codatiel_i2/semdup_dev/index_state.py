"""Persistent base index + per-head overlay state (DEV only).

Layout under {state_dir}/semdup/:

  indexes/{repo_slug}/{base_rev}.sqlite        base index (IC.build)
  indexes/{repo_slug}/{base_rev}.harrier.npz   base embedding sidecar
  indexes/{repo_slug}/{base_rev}.gcb.npz       base embedding sidecar
  indexes/{repo_slug}/{base_rev}.meta.json     {repo, base_rev, snapshot, stats}
  overlays/{repo_slug}/{pr}/{head_rev}/        overlay dir (OV.build_overlay)
  overlays/{repo_slug}/{pr}/{head_rev}/base_snapshot.json
      {repo, base_rev, base_db_sha256, head_rev, built_ts}

The overlay records which base snapshot it extends (the engine's
overlay.json does not; see LIMITATIONS.md staleness gap). Any base
mismatch is stale: the overlay is rebuilt or the delta is deferred
with an explicit incomplete status — never silently evaluated
against the wrong base.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from codatiel_i2.semdup_vendored.semdup import index_cmd as IC


def slug(repo: str) -> str:
    return repo.replace("/", "__")


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def materialize(rev: str, repo_path: Path, dest: Path) -> None:
    """Materialize git revision `rev` into `dest` via git archive."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(repo_path), "archive", rev],
                             capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout,
                   check=True)


def base_paths(state_dir: Path, repo: str, rev: str) -> dict[str, Path]:
    root = state_dir / "semdup" / "indexes" / slug(repo)
    stem = rev[:16]
    return {
        "db": root / f"{stem}.sqlite",
        "harrier": root / f"{stem}.harrier.npz",
        "gcb": root / f"{stem}.gcb.npz",
        "meta": root / f"{stem}.meta.json",
    }


def overlay_path(state_dir: Path, repo: str, pr: int, rev: str) -> Path:
    return (state_dir / "semdup" / "overlays" / slug(repo) / str(pr)
            / rev[:16])


def base_index_status(state_dir: Path, repo: str, rev: str) -> dict:
    """Describe the stored base index without building anything."""
    paths = base_paths(state_dir, repo, rev)
    if not paths["db"].exists() or not paths["meta"].exists():
        return {"present": False, "reason": "no base index for rev"}
    try:
        meta = json.loads(paths["meta"].read_text())
    except (ValueError, OSError) as exc:
        return {"present": False, "reason": f"unreadable base meta: {exc}"}
    if meta.get("base_rev") != rev or meta.get("repo") != repo:
        return {"present": False, "reason": "base meta identity mismatch"}
    if _sha_file(paths["db"]) != meta.get("base_db_sha256", ""):
        return {"present": False, "reason": "base index file changed on disk"}
    return {"present": True, "meta": meta,
            "harrier": paths["harrier"].exists(),
            "gcb": paths["gcb"].exists()}


def ensure_base_index(repo_path: Path, repo: str, rev: str,
                      state_dir: Path,
                      allow_embeddings: bool = True) -> dict:
    """Build (or reuse) the persistent base index for (repo, rev).

    Synchronous build; callers schedule it asynchronously so it never
    delays the standard PR reviewer. Returns a status dict; on failure
    the caller must defer with incomplete status, never proceed.
    """
    from codatiel_i2.semdup_vendored.semdup import harrier as HAR
    paths = base_paths(state_dir, repo, rev)
    paths["db"].parent.mkdir(parents=True, exist_ok=True)
    status = base_index_status(state_dir, repo, rev)
    if status["present"]:
        return {"reused": True, **status}
    with tempfile.TemporaryDirectory(prefix="semdup-base-") as tmp:
        tree = Path(tmp) / "tree"
        try:
            materialize(rev, repo_path, tree)
        except subprocess.CalledProcessError as exc:
            return {"reused": False, "present": False,
                    "reason": f"cannot materialize base rev {rev[:12]}: {exc}"}
        summary = IC.build(tree, paths["db"])
    emb: dict = {"harrier": False, "gcb": False}
    if allow_embeddings:
        try:
            HAR.build_index(paths["db"], paths["harrier"])
            emb["harrier"] = paths["harrier"].exists()
        except Exception as exc:  # noqa: BLE001 - honest record, not fatal
            emb["harrier_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        if not emb["harrier"]:
            try:
                from codatiel_i2.semdup_vendored.semdup import embed as GCB
                GCB.build_index(paths["db"], paths["gcb"])
                emb["gcb"] = paths["gcb"].exists()
            except Exception as exc:  # noqa: BLE001
                emb["gcb_error"] = (f"{type(exc).__name__}: "
                                    f"{str(exc)[:160]}")
    meta = {"repo": repo, "base_rev": rev,
            "base_db_sha256": _sha_file(paths["db"]),
            "snapshot": summary.get("snapshot"),
            "summary": {k: summary.get(k) for k in
                        ("status", "files_eligible", "files_parsed",
                         "files_failed", "units_indexed", "units_searched",
                         "files_deferred_by_language")},
            "embeddings": emb, "built_ts": time.time()}
    paths["meta"].write_text(json.dumps(meta, indent=1, default=str))
    return {"reused": False, "present": True, "meta": meta,
            "harrier": emb["harrier"], "gcb": emb["gcb"]}


def build_overlay(repo_path: Path, repo: str, pr: int, base_rev: str,
                  head_rev: str, state_dir: Path,
                  base_db: Path,
                  allow_embeddings: bool = True) -> dict:
    """Build the head overlay over the pinned base index.

    Records the base identity actually used; staleness is checked by
    `overlay_status`, never assumed.
    """
    from codatiel_i2.semdup_vendored.semdup import overlay as OV
    dest = overlay_path(state_dir, repo, pr, head_rev)
    if dest.exists():
        shutil.rmtree(dest)
    with tempfile.TemporaryDirectory(prefix="semdup-head-") as tmp:
        tree = Path(tmp) / "tree"
        try:
            materialize(head_rev, repo_path, tree)
        except subprocess.CalledProcessError as exc:
            return {"ok": False,
                    "reason": f"cannot materialize head rev {head_rev[:12]}: {exc}"}
        try:
            # embed=False here: the engine's overlay sidecars are
            # vectors-only (no ids) and raw-text (not the base sidecar
            # format). The adapter builds id-keyed, base-format sidecars
            # below so base + overlay vectors fuse exactly.
            stats = OV.build_overlay(base_db, tree, dest, embed=False)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False,
                    "reason": f"overlay build failed: {type(exc).__name__}: {str(exc)[:200]}"}
    scratch = dest / "_scratch.sqlite"
    emb: dict = {"harrier": False, "gcb": False}
    if allow_embeddings and scratch.exists():
        try:
            from codatiel_i2.semdup_vendored.semdup import harrier as HAR
            HAR.build_index(scratch, dest / "overlay_harrier_ids.npz")
            emb["harrier"] = True
        except Exception as exc:  # noqa: BLE001
            emb["harrier_error"] = (f"{type(exc).__name__}: "
                                    f"{str(exc)[:160]}")
        if not emb["harrier"]:
            try:
                from codatiel_i2.semdup_vendored.semdup import embed as GCB
                GCB.build_index(scratch, dest / "overlay_gcb_ids.npz")
                emb["gcb"] = True
            except Exception as exc:  # noqa: BLE001
                emb["gcb_error"] = (f"{type(exc).__name__}: "
                                    f"{str(exc)[:160]}")
    record = {"repo": repo, "pr": pr, "base_rev": base_rev,
              "base_db_sha256": _sha_file(base_db), "head_rev": head_rev,
              "built_ts": time.time(), "stats": stats,
              "embeddings": emb}
    (dest / "base_snapshot.json").write_text(json.dumps(record, indent=1,
                                                        default=str))
    return {"ok": True, "overlay_dir": str(dest), "record": record}


def overlay_status(state_dir: Path, repo: str, pr: int, head_rev: str,
                   base_db: Path) -> dict:
    """Verify a stored overlay matches (base index, head rev)."""
    dest = overlay_path(state_dir, repo, pr, head_rev)
    rec_path = dest / "base_snapshot.json"
    if not dest.exists() or not rec_path.exists():
        return {"present": False, "reason": "no overlay for head rev"}
    try:
        rec = json.loads(rec_path.read_text())
    except (ValueError, OSError) as exc:
        return {"present": False, "reason": f"unreadable overlay record: {exc}"}
    if rec.get("head_rev") != head_rev:
        return {"present": False, "reason": "overlay head mismatch"}
    if not base_db.exists():
        return {"present": False, "reason": "base index missing",
                "stale": True}
    if (rec.get("base_rev") is None or
            _sha_file(base_db) != rec.get("base_db_sha256")):
        return {"present": False, "reason": "overlay base is stale",
                "stale": True, "record": rec}
    return {"present": True, "record": rec, "overlay_dir": str(dest)}
