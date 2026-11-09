"""Swift adapter: tree-sitter parse-only extraction (no toolchain build).

Units: `function_declaration` (free functions, type members, extensions),
`init_declaration` (initializers), and meaningful `lambda_literal` closures
(multi-statement or assigned; single-token pass-throughs are skipped to
keep the index signal-dense). Regions: `if/for/while/guard/do/switch`
statements inside function bodies, capped per symbol with truncation
reported.

Evidence preserved: line-normalized source slices; fingerprint abstracts
identifiers/literals only. Flags are parsing-aware surface facts (`async` /
`throws` / actor-attribute nodes, `inout` parameter modifiers, `mutating`
modifiers, availability attributes) used as search hints — parsing an
extension method proves nothing about overload resolution; unknown stays
unknown. Error trees yield parse failure (counted, never empty success).
Single-expression pass-through closures are skipped by a documented
meaningfulness filter with counts reported. Indexing never boots a
simulator or builds the app.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import tsutil

MAX_REGIONS_PER_SYMBOL = 8

FUNCTION_TYPES = frozenset({"function_declaration", "init_declaration"})
CLOSURE_TYPES = frozenset({"lambda_literal"})
REGION_TYPES = frozenset({
    "if_statement", "for_statement", "while_statement", "guard_statement",
    "do_statement", "switch_statement",
})

CAPABILITY = {
    "grammar": "tree-sitter-swift",
    "grammar_version": "0.7.4",
    "core_version": "tree-sitter 0.26.0",
    "enrichment": "none (parse-only; SwiftSyntax toolchain optional future)",
}


@dataclass
class Unit:
    symbol: str
    kind: str  # function | initializer | closure | region
    start_line: int
    end_line: int
    text: str
    fingerprint: str
    truncated_regions: bool = False
    flags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)  # called names (best-effort)


def _func_name(node, src: bytes) -> str | None:
    for child in node.children:
        if child.type in ("simple_identifier", "identifier"):
            return tsutil.node_text(child, src)
    return None


def _called_names(node, src: bytes) -> list[str]:
    """Best-effort callee surface: leading identifier of call_expressions."""
    names: set[str] = set()
    for child in tsutil.iter_nodes(node, frozenset({"call_expression"})):
        for part in child.children:
            if part.type in ("simple_identifier", "identifier"):
                text = tsutil.node_text(part, src)
                if text.isidentifier():
                    names.add(text)
                break
            if part.is_named:
                break
    return sorted(names)


def _closure_meaningful(node, src: bytes) -> bool:
    text = tsutil.node_text(node, src)
    if "\n" in text.strip():
        return True
    # Single-expression closures assigned to a name are still meaningful.
    parent = node.parent
    if parent is not None and parent.type in ("pattern", "value_binding_pattern", "property_declaration"):
        return True
    return len(text) > 24


def _closure_symbol(node, src: bytes) -> str:
    parent = node.parent
    probe = parent
    while probe is not None and probe.type not in ("property_declaration", "pattern"):
        probe = probe.parent
    if probe is not None:
        for child in probe.children:
            if child.type in ("simple_identifier", "pattern"):
                text = tsutil.node_text(child, src).strip()
                if text.isidentifier():
                    return text
    start, _ = tsutil.line_range(node)
    return f"<closure>@{start}"


def _flags(node, src: bytes) -> list[str]:
    """Parsing-aware surface flags (node shapes, never substring scans)."""
    text = tsutil.node_text(node, src)
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in ("async", "await_expression"):
            found.add("async")
        elif current.type == "throws":
            found.add("throws")
        elif current.type == "attribute":
            attr = tsutil.node_text(current, src)
            if "MainActor" in attr:
                found.add("actor-isolation")
            if "available" in attr:
                found.add("availability")
        elif current.type in ("parameter_modifier", "parameter_modifiers"):
            if "inout" in tsutil.node_text(current, src).split():
                found.add("inout")
        elif current.type == "modifiers":
            words = tsutil.node_text(current, src).split()
            if "mutating" in words:
                found.add("mutating")
        stack.extend(current.children)
    flags: list[str] = sorted(found)
    if "available" in text and "availability" not in flags:
        # Availability conditions expressed as #available() call sites.
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "availability_condition":
                flags.append("availability")
                break
            stack.extend(current.children)
    return sorted(set(flags))


def extract(source: str) -> tuple[list[Unit], int, dict]:
    """Return (units, parse_failed, info {regions_truncated, closures_skipped}).

    `closures_skipped` counts single-expression pass-through closures
    dropped by the documented meaningfulness filter; their enclosing
    function text is always retained.
    """
    src = source.encode("utf-8")
    lines = source.splitlines()
    tree, has_error = tsutil.parse("swift", src)
    if has_error:
        return [], 1, {"regions_truncated": 0, "closures_skipped": 0}
    units: list[Unit] = []
    truncated_total = 0
    skipped_closures = 0
    for node in tsutil.iter_nodes(tree.root_node, FUNCTION_TYPES | CLOSURE_TYPES):
        if node.type in CLOSURE_TYPES and not _closure_meaningful(node, src):
            skipped_closures += 1
            continue
        start, end = tsutil.line_range(node)
        text = "\n".join(lines[start - 1 : end])
        if node.type == "function_declaration":
            name = _func_name(node, src) or "<anonymous>"
            kind = "function"
        elif node.type == "init_declaration":
            name = "init"
            kind = "initializer"
        else:
            name = _closure_symbol(node, src)
            kind = "closure"
        units.append(Unit(
            symbol=name, kind=kind, start_line=start, end_line=end, text=text,
            fingerprint=tsutil.fingerprint(node), flags=_flags(node, src),
            refs=_called_names(node, src),
        ))
        regions = [c for c in tsutil.iter_nodes(node, REGION_TYPES)]
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
    return units, 0, {"regions_truncated": truncated_total, "closures_skipped": skipped_closures}
