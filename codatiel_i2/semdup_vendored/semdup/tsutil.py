"""Shared tree-sitter helpers: cached languages, parse, ranges, fingerprints.

Parse-only: no compilation, no execution, no simulator. If a grammar is
unavailable the caller reports an explicit capability status (never a
silent empty index, never regex pretending to be a parser).
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=4)
def _language(kind: str):  # type: ignore[no-untyped-def]
    from tree_sitter import Language

    if kind == "php":
        import tree_sitter_php

        return Language(tree_sitter_php.language_php())
    if kind == "swift":
        import tree_sitter_swift

        return Language(tree_sitter_swift.language())
    raise ValueError(f"unknown grammar: {kind}")


def parse(kind: str, source_bytes: bytes):  # type: ignore[no-untyped-def]
    """Parse bytes; return (tree, has_error). Raises ImportError if missing."""
    from tree_sitter import Parser

    parser = Parser(_language(kind))
    tree = parser.parse(source_bytes)
    return tree, tree.root_node.has_error


def iter_nodes(node, types: frozenset[str]):  # type: ignore[no-untyped-def]
    """Yield descendant nodes (incl. self) whose type is in `types`."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in types:
            yield current
        stack.extend(reversed(current.children))


def child_by_type(node, typename: str):  # type: ignore[no-untyped-def]
    for child in node.children:
        if child.type == typename:
            return child
    return None


def node_text(node, source_bytes: bytes) -> str:  # type: ignore[no-untyped-def]
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def line_range(node) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """1-based inclusive (start, end) lines from tree-sitter points."""
    start = node.start_point.row + 1
    end = node.end_point.row + 1
    # A node ending at column 0 ends on the previous line's newline.
    if node.end_point.column == 0 and end > start:
        end -= 1
    return start, max(start, end)


def fingerprint(node) -> str:  # type: ignore[no-untyped-def]
    """Identifier-abstracted structural key over node-type multiset."""
    parts: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in ("name", "variable_name", "simple_identifier", "identifier"):
            parts.append("N")
        elif current.type in ("integer", "float", "string", "string_content"):
            parts.append("Const")
        elif current.named_child_count == 0 and not current.children:
            parts.append(current.type)
        else:
            parts.append(current.type)
        stack.extend(current.children)
    return "|".join(sorted(parts))
