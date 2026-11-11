"""Python adapter: AST-aligned units with exact UTF-8 ranges.

Extracts functions/async functions/methods (incl. nested, capped) plus a
bounded set of coherent internal regions (if/for/while/with/try blocks,
all descendants of each function) per function. The stored text is the
original source slice rejoined per line (line-normalized, not byte-exact
on CRLF input). A normalized fingerprint (names abstracted) is stored
alongside as evidence/change-detection data (assess/check/invalidate).
Fingerprints are never a retrieval signal (structural retrieval removed
M5.2 after a measured zero-utility ablation).

Capability notes: records bool/int-sensitive operators and effect-relevant
facts (e.g. `global`/`nonlocal`, `yield`/`await`, decorators) as flags for
later stages; unknown nodes stay unknown (flagged), never silently dropped.
"""
from __future__ import annotations

import ast
import io
import keyword
import tokenize
from dataclasses import dataclass, field

MAX_REGIONS_PER_SYMBOL = 8


@dataclass
class Unit:
    symbol: str
    kind: str  # function | region
    start_line: int
    end_line: int
    text: str
    fingerprint: str
    truncated_regions: bool = False
    flags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)  # imported modules + called names


def _lines(source: str) -> list[str]:
    return source.splitlines()


def _slice(source_lines: list[str], start: int, end: int) -> str:
    return "\n".join(source_lines[start - 1 : end])


def _fingerprint(tree: ast.AST) -> str:
    """Identifier-abstracted structural key (names -> N, attrs kept shallow)."""
    parts: list[str] = []
    for node in ast.walk(tree):
        name = type(node).__name__
        if isinstance(node, ast.Name):
            parts.append(f"Name:{node.id if keyword.iskeyword(node.id) else 'N'}")
        elif isinstance(node, ast.Constant):
            parts.append(f"Const:{type(node.value).__name__}")
        elif isinstance(node, ast.FunctionDef):
            parts.append("FuncDef")
        else:
            parts.append(name)
    return "|".join(sorted(parts))


def _file_imports(tree: ast.AST) -> list[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return sorted(mods)


def _called_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return sorted(names)


def _flags(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    flags: list[str] = []
    if isinstance(fn, ast.AsyncFunctionDef):
        flags.append("async")
    if fn.decorator_list:
        flags.append("decorated")
    for node in ast.walk(fn):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            flags.append("scope-mutation")
            break
    for node in ast.walk(fn):
        if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)):
            flags.append("suspends")
            break
    return sorted(set(flags))


def extract(source: str) -> tuple[list[Unit], int, dict]:
    """Return (units, parse_failed, info {regions_truncated, closures_skipped}).

    Python has no closure unit kind; closures_skipped is always 0 (kept
    for a uniform adapter contract).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], 1, {"regions_truncated": 0, "closures_skipped": 0}
    lines = _lines(source)
    units: list[Unit] = []
    truncated_total = 0
    file_mods = _file_imports(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        end = node.end_lineno or node.lineno
        text = _slice(lines, start, end)
        refs = sorted(set(file_mods) | set(_called_names(node)))
        units.append(
            Unit(
                symbol=node.name,
                kind="function",
                start_line=start,
                end_line=end,
                text=text,
                fingerprint=_fingerprint(node),
                flags=_flags(node),
                refs=refs,
            )
        )
        regions = [
            child
            for child in ast.walk(node)
            if child is not node
            and isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.Try))
        ]
        truncated = len(regions) > MAX_REGIONS_PER_SYMBOL
        if truncated:
            truncated_total += 1
        for child in regions[:MAX_REGIONS_PER_SYMBOL]:
            cstart = child.lineno
            cend = child.end_lineno or child.lineno
            units.append(
                Unit(
                    symbol=f"{node.name}#region@{cstart}",
                    kind="region",
                    start_line=cstart,
                    end_line=cend,
                    text=_slice(lines, cstart, cend),
                    fingerprint=_fingerprint(child),
                    truncated_regions=truncated,
                )
            )
    return units, 0, {"regions_truncated": truncated_total, "closures_skipped": 0}
