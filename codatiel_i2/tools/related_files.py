"""related_files: sibling/peer lookup (deterministic filename + symbol overlap)."""

from __future__ import annotations

from pathlib import Path

from codatiel_i2.core.calls import traced


@traced("related_files")
def related_files(repo_root: Path, path: str, *, max_results: int = 20) -> dict:
    root = Path(repo_root).resolve()
    target = Path(path)
    stem = target.stem.lower()
    parent = target.parent
    scored: list[dict] = []
    for cand in sorted(root.rglob("*")):
        if not cand.is_file() or ".git" in cand.parts:
            continue
        try:
            rel = str(cand.relative_to(root))
        except ValueError:
            continue
        if rel == path:
            continue
        score = 0
        reasons: list[str] = []
        if str(Path(rel).parent) == str(parent):
            score += 2
            reasons.append("same_dir")
        name = cand.stem.lower()
        if stem and (stem in name or name in stem):
            score += 3
            reasons.append("name_overlap")
        if cand.suffix == target.suffix:
            score += 1
            reasons.append("same_ext")
        if score > 0:
            scored.append({"path": rel, "score": score, "reason": "+".join(reasons)})
    scored.sort(key=lambda r: (-r["score"], r["path"]))
    return {"related": scored[:max_results]}
