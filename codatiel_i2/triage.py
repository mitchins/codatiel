"""Deterministic triage inventory for M9.2 arm C (no LLM, no case rules).

Signals (all generic, computed offline before any model call):
- per-file change kind + churn (added/removed lines, deletion-heavy flag);
- path class (auth, persistence/schema/migration, config, api/route,
  tests, docs, other) by path pattern;
- added/removed definition lines by language-agnostic heuristic;
- in-repo reference counts for changed symbols via the existing I2
  search tool (offline call, no model);
- test-only vs source-change flag.

Output is a ranked (churn-descending), capped list with the raw facts.
Ranking key is exactly additions+deletions, descending; nothing else is
weighted. Token cost is recorded in the header line of every rendering.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

TRIAGE_INVENTORY_STRATEGY = "triage-0.1"
TRIAGE_MAX_FILES = 25
TRIAGE_MAX_SYMBOL_SEARCHES = 12

_PATH_CLASSES: list[tuple[str, re.Pattern]] = [
    ("tests", re.compile(r"(^|/)(test_|_test\.|tests?/|spec/|__tests__/)", re.I)),
    ("docs", re.compile(r"\.(md|rst|txt)$|^(docs?/|README|CHANGELOG|LICENSE)", re.I)),
    ("config", re.compile(r"\.(json|ya?ml|toml|ini|cfg|conf|acls?)$|(^|/)(Dockerfile|Makefile|\.github/)", re.I)),
    ("auth", re.compile(r"auth|permission|owner|role|guard|polic|acl|login|session|token|passwd|secret|trusted|verif", re.I)),
    ("persistence/schema/migration", re.compile(r"migrat|schema|models?/|store|ledger|_db|database|d1_|r2_|queue", re.I)),
    ("api/route", re.compile(r"route|router|handler|endpoint|controller|/api/|views?/|haproxy|vhost|\.conf$", re.I)),
]

_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:def|class|func|function|interface|type|fn|sub|proc|procedure)\s+"
    r"([A-Za-z_][\w$]*)"
)


def path_class(path: str) -> str:
    for name, rx in _PATH_CLASSES:
        if rx.search(path):
            return name
    return "other"


def _symbols_from_hunks(diff_text: str) -> tuple[list[str], list[str]]:
    """Heuristic added/removed definition names from a unified diff.

    Language-agnostic and intentionally narrow: only definition keywords.
    Reported as raw facts; the model decides what they mean.
    """
    added, removed = [], []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            m = _DEF_RE.match(line[1:])
            if m and m.group(1) not in added:
                added.append(m.group(1))
        elif line.startswith("-"):
            m = _DEF_RE.match(line[1:])
            if m and m.group(1) not in removed:
                removed.append(m.group(1))
    return added[:5], removed[:5]


def _reference_counts(symbols: list[str], *, head_dir: Path,
                      base_sha: str, head_sha: str) -> dict[str, Any]:
    """Count files referencing each symbol in HEAD via the I2 search tool.

    Offline deterministic call (no model, no ledger). Failures record
    "unknown", never zero, so absence of evidence is not faked.
    """
    from codatiel_i2.tools.search import search as i2_search

    counts: dict[str, Any] = {}
    for sym in symbols[:TRIAGE_MAX_SYMBOL_SEARCHES]:
        try:
            out = i2_search(head_dir, sym, base_sha=base_sha,
                            head_sha=head_sha, revision="head",
                            regex=False, max_results=50, context_lines=0)
            files = {r.get("path") for r in out.get("results", [])
                     if isinstance(r, dict) and r.get("path")}
            counts[sym] = len(files)
        except Exception:
            counts[sym] = "unknown"
    skipped = max(0, len(symbols) - TRIAGE_MAX_SYMBOL_SEARCHES)
    if skipped:
        counts["_searches_skipped"] = skipped
    return counts


def render_triage_inventory(changes: list[dict], classification: dict,
                            det_candidates: list[dict], *, diff_repo: Path,
                            head_dir: Path, base_sha: str,
                            head_sha: str) -> str:
    """Ranked, capped triage list with raw facts. Deterministic."""
    rows = []
    for c in changes:
        path = str(c.get("path", ""))
        add = int(c.get("additions", 0) or 0)
        dele = int(c.get("deletions", 0) or 0)
        rows.append({"path": path, "additions": add, "deletions": dele,
                     "churn": add + dele, "class": path_class(path),
                     "status": ("added" if add and not dele else
                                "removed" if dele and not add else "modified")})
    rows.sort(key=lambda r: (-r["churn"], r["path"]))
    shown = rows[:TRIAGE_MAX_FILES]

    # Definition facts from per-file diffs (best effort, raw).
    from codatiel_i2.tools.diff_window import diff_window as i2_diff
    from pathlib import Path as _P
    symbols: list[str] = []
    sym_by_path: dict[str, dict[str, list[str]]] = {}
    for r in shown:
        try:
            d = i2_diff(_P(diff_repo), base_sha, head_sha, r["path"])
            text = d.get("diff", "") if isinstance(d, dict) else ""
        except Exception:
            text = ""
        added, removed = _symbols_from_hunks(text)
        sym_by_path[r["path"]] = {"added": added, "removed": removed}
        for s in added:
            if s not in symbols:
                symbols.append(s)
    refs = _reference_counts(symbols, head_dir=Path(head_dir),
                             base_sha=base_sha, head_sha=head_sha)

    lines = [f"Triage inventory ({len(changes)} changed files, "
             f"top {len(shown)} by churn=additions+deletions):"]
    test_only = all(r["class"] == "tests" for r in rows) if rows else True
    src = [r for r in rows if r["class"] not in ("tests", "docs")]
    lines.append(f"Source changes (non-test, non-doc): {len(src)} files. "
                 + ("Test/docs-only change." if not src else ""))
    for r in shown:
        sym = sym_by_path.get(r["path"], {})
        added = sym.get("added", [])
        refbits = ", ".join(
            f"{s}: {refs[s]} files" if s in refs else f"{s}: not-searched"
            for s in added[:3])
        lines.append(
            f"- {r['path']} [{r['status']}, class={r['class']}, "
            f"+{r['additions']}/-{r['deletions']}, churn={r['churn']}"
            + (" DELETION-HEAVY" if r["deletions"] > r["additions"] and r["deletions"] > 50 else "")
            + (f", defs+={added}" if added else "")
            + (f", defs-={sym.get('removed', [])}" if sym.get("removed") else "")
            + (f", refs({refbits})" if refbits else "")
            + ")")
    if len(rows) > len(shown):
        lines.append(f"- ... and {len(rows) - len(shown)} more (capped).")
    lines.append(f"Deterministic docs_code candidates: {len(det_candidates)}.")
    lines.append("Read the highest-churn and authority-class (auth, "
                 "persistence/schema/migration, config, api/route) changes "
                 "first with the source tools. Fetch every span you rely on.")
    body = "\n".join(lines)
    est_tokens = max(1, len(body) // 4)
    return (f"[triage-0.1 chars={len(body)} ~tokens={est_tokens}]\n" + body)
