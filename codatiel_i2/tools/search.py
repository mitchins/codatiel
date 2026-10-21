"""search tool (ripgrep-backed, path-confined) with evidence envelope."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from codatiel_i2.common.paths import safe_join
from codatiel_i2.core.calls import traced
from codatiel_i2.core.evidence import checkout_holds_sha, make_evidence


@traced("search")
def search(repo_root: Path, query: str, *, base_sha: str, head_sha: str,
           revision: str = "head",
           regex: bool = True, max_results: int = 50, context_lines: int = 2) -> dict:
    """Path-confined search with evidence envelopes.

    Backend parity limits (documented, not hidden): the ripgrep path honors
    .gitignore and counts --max-count per file; the pure-Python fallback
    walks everything and ignores context_lines. Both return the same
    {results, evidence} shape with identical envelope guarantees.
    """
    root = Path(repo_root).resolve()
    if revision not in ("base", "head"):
        raise ValueError("search revision must be 'base' or 'head'")
    expected = base_sha if revision == "base" else head_sha
    if not checkout_holds_sha(root, expected):
        raise ValueError(f"checkout does not hold claimed {revision} SHA {expected[:8]}")
    if not query:
        raise ValueError("query is required")
    # Prefer rg; fall back to python grep for tests without rg.
    if _has_rg():
        return _rg_search(root, query, base_sha, head_sha, revision, regex, max_results, context_lines)
    return _py_search(root, query, base_sha, head_sha, revision, regex, max_results)


def _has_rg() -> bool:
    try:
        proc = subprocess.run(["rg", "--version"], capture_output=True, timeout=10)
        return proc.returncode == 0
    except Exception:
        return False


MAX_EVIDENCE_LINE_BYTES = 200_000


def _rg_search(root: Path, query: str, base_sha: str, head_sha: str, revision: str,
               regex: bool, max_results: int, context: int) -> dict:
    cmd = ["rg", "--line-number", "--no-heading", f"-C{context}", "--max-count", str(max_results), "--", query, "."]
    if not regex:
        cmd.insert(1, "--fixed-strings")
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=60)
    # rg returns 1 when no matches; treat as empty.
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"search failed: {proc.stderr.strip()[:500]}")
    results = _parse_rg_output(root, proc.stdout, max_results)
    evidence = []
    skipped_long_lines = 0
    for r in results:
        if len(r["match"].encode("utf-8")) > MAX_EVIDENCE_LINE_BYTES:
            skipped_long_lines += 1
            continue
        ev = make_evidence(kind="source_excerpt", tool="search", base_sha=base_sha, head_sha=head_sha,
                           revision=revision,
                           path=r["path"], line_start=r["line"], line_end=r["line"],
                           excerpt=r["match"])
        evidence.append(ev.to_dict())
    return {"results": results, "evidence": evidence, "truncated": len(results) >= max_results,
            "skipped_long_lines": skipped_long_lines}


def _parse_rg_output(root: Path, text: str, limit: int) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        # Hit format: path:line:content. Context lines use path-line-content
        # (dash separators) and MUST NOT parse as hits: a colon-bearing
        # context body (e.g. `a.py-2-foo:123:bar`) would otherwise mint a
        # phantom path. Accept only entries resolving to a real file.
        if line.strip() in ("--", ""):
            continue
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].strip().isdigit() and parts[2].strip():
            rel = parts[0].lstrip("./")
            try:
                target = safe_join(root, rel)
            except ValueError:
                continue
            if not target.is_file():
                continue
            # Integrity over convenience: the full source line is the
            # evidence; the display snippet is for humans only.
            out.append({"path": rel, "line": int(parts[1].strip()), "match": parts[2],
                        "match_display": parts[2][:2000]})
            if len(out) >= limit:
                break
    return out


def _py_search(root: Path, query: str, base_sha: str, head_sha: str, revision: str, regex: bool, limit: int) -> dict:
    try:
        pattern = re.compile(query) if regex else None
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}")
    results: list[dict] = []
    skipped_long_lines = 0
    for path in sorted(root.rglob("*")):
        if len(results) >= limit:
            break
        if path.is_symlink():
            continue
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            hit = bool(pattern.search(line)) if pattern else (query in line)
            if hit and line.strip():
                if len(line.encode("utf-8")) > MAX_EVIDENCE_LINE_BYTES:
                    skipped_long_lines += 1
                    continue
                rel = str(path.relative_to(root))
                results.append({"path": rel, "line": i, "match": line,
                                "match_display": line[:2000]})
                if len(results) >= limit:
                    break
    evidence = [make_evidence(kind="source_excerpt", tool="search", base_sha=base_sha, head_sha=head_sha,
                              revision=revision,
                              path=r["path"], line_start=r["line"], line_end=r["line"],
                              excerpt=r["match"]).to_dict() for r in results]
    return {"results": results, "evidence": evidence, "truncated": len(results) >= limit,
            "skipped_long_lines": skipped_long_lines}
