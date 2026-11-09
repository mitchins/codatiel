"""PHP adapter: tree-sitter parse-only extraction (no regex-as-parser).

Units: `function_definition` (named), `method_declaration` (class/trait
members), `anonymous_function` closures and `arrow_function`s (symbol from
enclosing assignment or `<closure>@line`). Regions: statement-level
`if/for/foreach/while/switch/try` inside function bodies, capped per
symbol with truncation reported.

Evidence preserved: line-normalized source slices (see python adapter note);
fingerprint abstracts identifiers only. Flags are parsing-aware surface
facts (strict `===`/`!==` operator nodes, `reference_modifier` params,
`isset`/`empty` call nodes, `static_modifier` closures) used as search
hints — never semantic proof. A tree with error nodes yields parse
failure (counted, never an empty success): unknown semantics stay unknown.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import tsutil

MAX_REGIONS_PER_SYMBOL = 8

FUNCTION_TYPES = frozenset({"function_definition", "method_declaration"})
CLOSURE_TYPES = frozenset({"anonymous_function", "arrow_function"})
REGION_TYPES = frozenset({
    "if_statement", "for_statement", "foreach_statement",
    "while_statement", "do_statement", "switch_statement", "try_statement",
})

CAPABILITY = {
    "grammar": "tree-sitter-php",
    "grammar_version": "0.24.1",
    "core_version": "tree-sitter 0.26.0",
    "enrichment": "none (parse-only)",
}


@dataclass
class Unit:
    symbol: str
    kind: str  # function | closure | region
    start_line: int
    end_line: int
    text: str
    fingerprint: str
    truncated_regions: bool = False
    flags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)  # called function names


def _name_text(node, src: bytes) -> str | None:
    name = tsutil.child_by_type(node, "name")
    if name is None:
        return None
    return tsutil.node_text(name, src)


def _called_names(node, src: bytes) -> list[str]:
    """Best-effort callee surface: function_call_expression names."""
    names: set[str] = set()
    for child in tsutil.iter_nodes(node, frozenset({"function_call_expression"})):
        name = tsutil.child_by_type(child, "name")
        if name is not None:
            text = tsutil.node_text(name, src)
            if text.isidentifier():
                names.add(text)
    return sorted(names)


def _closure_symbol(node, src: bytes, lines: list[str]) -> str:
    parent = node.parent
    if parent is not None and parent.type == "assignment_expression":
        left = parent.child_by_field_name("left")
        if left is not None:
            text = tsutil.node_text(left, src).strip().lstrip("$")
            if text.isidentifier():
                return text
    start, _ = tsutil.line_range(node)
    return f"<closure>@{start}"


def _flags(node, src: bytes) -> list[str]:
    """Parsing-aware surface flags (node shapes, never substring scans)."""
    flags: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "binary_expression":
            for child in current.children:
                if not child.is_named and tsutil.node_text(child, src) in ("===", "!=="):
                    flags.add("strict-comparison")
        elif current.type == "function_call_expression":
            name = tsutil.child_by_type(current, "name")
            if name is not None and tsutil.node_text(name, src) in ("isset", "empty"):
                flags.add("isset-present" if tsutil.node_text(name, src) == "isset" else "empty-present")
        elif current.type == "reference_modifier":
            flags.add("reference-param")
        elif current.type == "static_modifier" and node.type == "anonymous_function":
            flags.add("static-closure")
        stack.extend(current.children)
    return sorted(flags)


def extract(source: str) -> tuple[list[Unit], int, dict]:
    """Return (units, parse_failed, info {regions_truncated, closures_skipped}).

    All closures are indexed (closures_skipped is always 0; kept for a
    uniform adapter contract).
    """
    src = source.encode("utf-8")
    lines = source.splitlines()
    try:
        tree, has_error = tsutil.parse("php", src)
    except ImportError:
        raise
    if has_error:
        return [], 1, {"regions_truncated": 0, "closures_skipped": 0}
    units: list[Unit] = []
    truncated_total = 0
    for node in tsutil.iter_nodes(tree.root_node, FUNCTION_TYPES | CLOSURE_TYPES):
        start, end = tsutil.line_range(node)
        text = "\n".join(lines[start - 1 : end])
        if node.type in FUNCTION_TYPES:
            name = _name_text(node, src) or "<anonymous>"
            kind = "function"
        else:
            name = _closure_symbol(node, src, lines)
            kind = "closure"
        units.append(Unit(
            symbol=name, kind=kind, start_line=start, end_line=end, text=text,
            fingerprint=tsutil.fingerprint(node), flags=_flags(node, src),
            refs=_called_names(node, src),
        ))
        regions = [c for c in tsutil.iter_nodes(node, REGION_TYPES) if c is not node]
        truncated = len(regions) > MAX_REGIONS_PER_SYMBOL
        if truncated:
            truncated_total += 1
        for child in regions[:MAX_REGIONS_PER_SYMBOL]:
            cstart, cend = tsutil.line_range(child)
            units.append(Unit(
                symbol=f"{name}#region@{cstart}", kind="region",
                start_line=cstart, end_line=cend,
                text="\n".join(lines[cstart - 1 : cend]),
                fingerprint=tsutil.fingerprint(child),
                truncated_regions=truncated,
            ))
    return units, 0, {"regions_truncated": truncated_total, "closures_skipped": 0}
