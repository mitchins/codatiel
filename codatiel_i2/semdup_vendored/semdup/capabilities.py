"""Language capability matrix: pinned grammars + availability status.

Missing toolchains are explicit statuses (may force `partial` for that
language, never a silent skip). Versions pinned to the tested environment;
see pyproject for dependency pins.
"""
from __future__ import annotations

import importlib.util


def _grammar_version(module: str) -> str | None:
    try:
        dist = importlib.metadata.version(module)  # type: ignore[attr-defined]
        return str(dist)
    except Exception:
        return None


def matrix() -> dict[str, dict[str, str]]:
    import importlib.metadata

    def ver(mod: str) -> str:
        try:
            return importlib.metadata.version(mod)
        except Exception:
            return "unavailable"

    ts_core = ver("tree-sitter")
    result: dict[str, dict[str, str]] = {
        "python": {
            "parser": "stdlib-ast",
            "grammar": "CPython 3.12 ast (stdlib)",
            "status": "available",
        },
        "php": {
            "parser": "tree-sitter-php",
            "grammar": ver("tree-sitter-php"),
            "core": f"tree-sitter {ts_core}",
            "status": "available"
            if importlib.util.find_spec("tree_sitter_php") else "unavailable",
        },
        "swift": {
            "parser": "tree-sitter-swift",
            "grammar": ver("tree-sitter-swift"),
            "core": f"tree-sitter {ts_core}",
            "status": "available"
            if importlib.util.find_spec("tree_sitter_swift") else "unavailable",
        },
    }
    return result
