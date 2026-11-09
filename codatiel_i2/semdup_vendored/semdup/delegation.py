"""Delegation guard: single-call wrappers are uses, not independent copies.

A unit whose entire body is one call delegating to the peer symbol (e.g.
`return bucket_for(value)`) must not become an "introduced duplication"
finding with that peer. Detection is AST-precise for Python and matches
only bare-name calls: `self.method()` attribute calls never match a
module-level peer (receiver unqualified — matching them would suppress
real duplication), and method peers (`Class#method`) never match, keeping
the guard inert-but-safe there. Other languages report unknown (pair kept
for the assessor, never silently dropped — the safe direction for recall).
"""
from __future__ import annotations

import ast


def is_delegation(text: str, peer_symbol: str, language: str) -> bool:
    """True only when `text` provably delegates to `peer_symbol`."""
    peer = peer_symbol.split("#")[0].split("@")[0].lstrip("$")
    if language != "python":
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(fns) != 1:
        return False
    body = [s for s in fns[0].body if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return False
    value = body[0].value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if not isinstance(func, ast.Name):
        return False
    return func.id == peer
