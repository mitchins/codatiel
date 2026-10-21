"""Symbol tools: find_definition / find_callers (AST + grep, deterministic)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from codatiel_i2.core.calls import traced
from codatiel_i2.core.evidence import checkout_holds_sha, make_evidence
from codatiel_i2.tools.search import MAX_EVIDENCE_LINE_BYTES


@traced("find_definition")
def find_definition(repo_root: Path, symbol: str, *, base_sha: str, head_sha: str, revision: str = "head",
                    max_results: int = 20) -> dict:
    root = Path(repo_root).resolve()
    if revision not in ("base", "head"):
        raise ValueError("find_definition revision must be 'base' or 'head'")
    expected = base_sha if revision == "base" else head_sha
    if not checkout_holds_sha(root, expected):
        raise ValueError(f"checkout does not hold claimed {revision} SHA {expected[:8]}")
    results: list[dict] = []
    for path in sorted(root.rglob("*.py")):
        if path.is_symlink():
            continue
        if ".git" in path.parts or len(results) >= max_results:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
                rel = str(path.relative_to(root))
                results.append({"path": rel, "line_start": node.lineno,
                                "line_end": getattr(node, "end_lineno", node.lineno) or node.lineno,
                                "kind": "definition", "symbol": symbol})
                if len(results) >= max_results:
                    break
    evidence = []
    for r in results:
        try:
            lines = (root / r["path"]).read_text(encoding="utf-8").splitlines()
            excerpt = "\n".join(lines[r["line_start"] - 1:r["line_end"]])
        except Exception:
            # Never mint synthetic bytes: a definition we cannot re-read
            # gets no evidence (fail-closed downstream).
            continue
        evidence.append(make_evidence(kind="source_excerpt", tool="find_definition", base_sha=base_sha, head_sha=head_sha,
                                      revision=revision,
                                      path=r["path"], line_start=r["line_start"], line_end=r["line_end"],
                                      excerpt=excerpt).to_dict())
    return {"definitions": results, "evidence": evidence}


@traced("find_callers")
def find_callers(repo_root: Path, symbol: str, *, base_sha: str, head_sha: str, revision: str = "head",
                 max_results: int = 50) -> dict:
    root = Path(repo_root).resolve()
    if revision not in ("base", "head"):
        raise ValueError("find_callers revision must be 'base' or 'head'")
    expected = base_sha if revision == "base" else head_sha
    if not checkout_holds_sha(root, expected):
        raise ValueError(f"checkout does not hold claimed {revision} SHA {expected[:8]}")
    pattern = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    results: list[dict] = []
    for path in sorted(root.rglob("*")):
        if len(results) >= max_results:
            break
        if path.is_symlink():
            continue
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix not in (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".swift", ".rb"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                # Full line is the evidence (stripped minting never replays
                # for indented call sites); display is for humans only.
                if len(line.encode("utf-8")) > MAX_EVIDENCE_LINE_BYTES:
                    continue
                results.append({"path": str(path.relative_to(root)), "line": i,
                                "match": line, "match_display": line.strip()[:2000],
                                "kind": "call"})
                if len(results) >= max_results:
                    break
    evidence = [make_evidence(kind="source_excerpt", tool="find_callers", base_sha=base_sha, head_sha=head_sha,
                              revision=revision,
                              path=r["path"], line_start=r["line"], line_end=r["line"],
                              excerpt=r["match"]).to_dict() for r in results]
    return {"callers": results, "evidence": evidence, "truncated": len(results) >= max_results}
