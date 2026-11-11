"""Deterministic responsibility descriptions (model-free hypothesis layer).

A description is a *hypothesis used for search* — not a compiler fact and
not evidence. Evidence remains source spans + deterministic repository
facts (M0 §3). Two provenances:

- `heuristic-ast` (Python): verbs from the symbol name (canonicalized via
  the generic lexicon), parameter/return shape, control operators,
  effects (mutation/io/raise/suspend), called entities, literals mentioned.
- `heuristic-text` (PHP/Swift): name verbs + keyword-surface operators
  from the unit text. Weaker by construction; recorded as such.

Unknowns are stated (`unresolved:`), never filled in.
"""
from __future__ import annotations

import ast
import re

from . import expand as lex

_NAME_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_+")
_STOP = frozenset({"the", "a", "an", "of", "to", "for", "in", "on", "is", "it", "my", "our"})

# Generic mutating-method surface (stdlib container/io verbs). A call through
# one of these names is recorded as a possible side effect — the description
# cannot prove aliasing, so it stays a hypothesis flag, never a fact.
MUTATING_METHODS = frozenset({
    "append", "extend", "update", "add", "remove", "pop", "setdefault",
    "insert", "clear", "discard", "write", "writelines", "send", "publish",
    "log", "save", "delete",
})


def name_tokens(symbol: str) -> list[str]:
    base = symbol.split("#")[0].split("@")[0]
    base = base.lstrip("_$").lstrip("$")
    parts = [p for p in _NAME_SPLIT.split(base) if p]
    return [p.lower() for p in parts if p.lower() not in _STOP and len(p) > 1]


def _describe_python(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    toks = name_tokens(fn.name)
    canon = sorted({lex.canonical(t) for t in toks})
    params = [a.arg for a in fn.args.args if a.arg not in ("self", "cls")]
    ops: set[str] = set()
    effects: set[str] = set()
    calls: set[str] = set()
    literals: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            ops.add("branch")
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            ops.add("loop")
        elif isinstance(node, ast.Try):
            ops.add("exception-guard")
        elif isinstance(node, ast.With):
            ops.add("resource-scope")
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            ops.add("comprehension")
        elif isinstance(node, ast.Return):
            ops.add("returns")
        elif isinstance(node, ast.Raise):
            effects.add("raises")
        elif isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)):
            effects.add("suspends")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            effects.add("scope-mutation")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, (ast.Attribute, ast.Subscript)):
                    effects.add("mutates-target")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                calls.add(func.attr)
                if func.attr in MUTATING_METHODS:
                    effects.add("side-effect-call")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
            text = str(node.value)
            if len(text) <= 24:
                literals.add(f"literal:{text}")
    bits = [
        f"responsibility: {'/'.join(canon) if canon else 'unknown'} ({fn.name})",
        f"inputs: {', '.join(params) if params else 'none-listed'}",
        f"control: {', '.join(sorted(ops)) if ops else 'straight-line'}",
        f"effects: {', '.join(sorted(effects)) if effects else 'pure-computation'}",
    ]
    if calls:
        bits.append(f"calls: {', '.join(sorted(calls))}")
    if literals:
        bits.append(f"mentions: {', '.join(sorted(literals))}")
    bits.append("unresolved: caller expectations, units of measure, error contract")
    return "; ".join(bits)


def _describe_text(symbol: str, text: str) -> str:
    toks = name_tokens(symbol)
    canon = sorted({lex.canonical(t) for t in toks})
    low = text.lower()
    ops = [w for w in ("if", "for", "while", "guard", "switch", "try", "catch", "foreach", "return", "throw") if w in low]
    return (
        f"responsibility: {'/'.join(canon) if canon else 'unknown'} ({symbol}); "
        f"control-surface: {', '.join(ops) if ops else 'straight-line'}; "
        f"effects: unknown-heuristic-text; "
        f"unresolved: types, caller expectations, error contract"
    )


def describe(symbol: str, language: str, text: str) -> tuple[str, str]:
    """Return (description, provenance). Never raises on weird input."""
    if language == "python":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return _describe_text(symbol, text), "heuristic-text"
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol.split("#")[0]:
                return _describe_python(node), "heuristic-ast"
        return _describe_text(symbol, text), "heuristic-text"
    return _describe_text(symbol, text), "heuristic-text"
