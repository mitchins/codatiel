"""Deterministic ownership guardrails (M6.4b).

M6.4 evidence ablation proved the judge ignores even explicitly provided
delegation/provenance facts (variant B/C changed nothing on F1/F2), so
these two mechanisms are enforced from facts, not begged from the model:
a guardrail never upgrades, never touches keep_separate/investigate —
it only downgrades ACTIONABLE findings to investigate with stated
reasons when deterministic triggers fire:

G1 pure-forwarder-to-sibling: a member whose recorded refs are exactly
   one sibling-member symbol and whose body is small. Such a member
   DEFINES nothing — it delegates. Consolidating it "into" its own
   implementation is a no-op at best, alias-deletion at worst.
G2 generated-artifact member: path markers or file-header prohibition
   (DO NOT EDIT / generated-from). Generated outputs must never be
   reuse targets; truth lives in the schema/source.

Both rules are mechanism-general (F1: 3 litmus + 3 dev cases; F2: 1
litmus + 1 dev case), auditable, and tested. They do not patch
individual examples: triggers are structural, thresholds documented.
Arithmetic-equivalence under-recognition (F3) is NOT addressed here —
no sound deterministic trigger exists; it stays a known limitation
(conservative direction, no damage).
"""
from __future__ import annotations

import re
from pathlib import Path

ACTIONABLE = ("reuse_existing", "extract_shared_part", "share_contract")

GENERATED_PATH_MARKERS = ("generated", "codegen", "__generated__")
GENERATED_HEADER_RE = re.compile(
    r"DO NOT EDIT|generated from|auto-generated|GENERATED\b|do not modify", re.IGNORECASE)
# Member lookback requires DIRECTIVE-shaped markers (bare "generated" in
# prose is too weak this close to code); file headers admit the broader
# pattern above since directive headers live at line 1.
# Known overbreadth (safe direction): a DESCRIPTIVE mention of a
# directive phrase near hand-written code also fires (e.g. a comment
# discussing DO NOT EDIT policy). The downgrade is to investigate, never
# harmful; descriptive mentions are rare outside generated contexts.
GENERATED_DIRECTIVE_RE = re.compile(
    r"DO NOT EDIT|generated from|auto-generated|@generated|do not modify", re.IGNORECASE)

# File provenance lives in headers: scan the first lines of the file plus
# the comment lines immediately above the member span (mixed hand-written +
# generated files attach markers per section, not just at line 1).
# Threshold sensitivity (measured, not tuned): header 3 lines catches
# line-1 directives (the observed real pattern: litmus generated files,
# dev gen/*); member lookback 6 catches per-section markers (dev
# trust.php:20/25). Narrowing header 10->3 removed prose-mention
# false positives (file-level descriptive comments mentioning
# "generated" beyond line 3 no longer fire) with zero change on all
# measured cases (unit tests + M6.3 dev re-runs). MAX_FORWARDER_LINES=6
# admits single-call forwarders with docstrings; larger wrappers with
# extra logic are spared (tested).
HEADER_SCAN_LINES = 3
MEMBER_LOOKBACK_LINES = 6

# A pure forwarder body is tiny: at most this many non-blank lines
# (signature + return/call + end-marker). Anything larger is not "pure".
MAX_FORWARDER_LINES = 6


def _code_lines(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "//", "*", '"""', "'''", "<!--")):
            continue
        out.append(s)
    return out


def pure_forwarder_to_sibling(text: str, refs: list, siblings: set[str]) -> str | None:
    """Trigger G1: refs name exactly one sibling and the body is trivial."""
    names = [r if isinstance(r, str) else r.get("name") for r in (refs or [])]
    names = [n for n in names if isinstance(n, str)]
    sibs = [n for n in names if n in siblings]
    if len(names) != 1 or len(sibs) != 1:
        return None
    if len(_code_lines(text)) > MAX_FORWARDER_LINES:
        return None
    return (f"pure-forwarder-to-sibling: body delegates solely to sibling "
            f"'{sibs[0]}' (single recorded reference, "
            f"{len(_code_lines(text))} code lines)")


def generated_member(path: str, repo: Path | None, text: str,
                     start_line: int = 1) -> str | None:
    """Trigger G2: generated path markers, file-header prohibition, or
    generation markers in comments immediately above the member span."""
    lowered = (path or "").lower()
    if any(m in lowered for m in GENERATED_PATH_MARKERS):
        return f"generated-artifact: path marker in '{path}'"
    if repo is not None:
        try:
            lines = (repo / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            lines = []
        # Most specific first: markers attached to the member beat file
        # headers in the reason trail (either one downgrades).
        above = lines[max(0, start_line - 1 - MEMBER_LOOKBACK_LINES):start_line - 1]
        for j, line in enumerate(above, start=max(1, start_line - len(above))):
            if GENERATED_DIRECTIVE_RE.search(line):
                return (f"generated-artifact: marker above member at {path}:{j} "
                        f"({line.strip()[:80]})")
        for i, line in enumerate(lines[:HEADER_SCAN_LINES], 1):
            if GENERATED_HEADER_RE.search(line):
                return (f"generated-artifact: file header line {i} prohibits editing "
                        f"({line.strip()[:80]})")
    if GENERATED_HEADER_RE.search("\n".join((text or "").splitlines()[:3])):
        return "generated-artifact: member header carries generation marker"
    return None


def apply(rows: list[dict], refs_by_symbol: dict[str, list], verdict: dict,
          repo: Path | None) -> tuple[dict, list[str]]:
    """Downgrade actionable findings on G1/G2 triggers. Returns (verdict, reasons)."""
    if verdict.get("action") not in ACTIONABLE:
        return verdict, []
    siblings = {r.get("symbol") for r in rows if r.get("symbol")}
    reasons: list[str] = []
    for r in rows:
        g1 = pure_forwarder_to_sibling(r.get("text", ""), refs_by_symbol.get(r.get("symbol"), []),
                                       siblings - {r.get("symbol")})
        if g1 is not None:
            reasons.append(f"{r.get('symbol')}: {g1}")
        g2 = generated_member(r.get("path", ""), repo, r.get("text", ""),
                              int(r.get("start_line", 1) or 1))
        if g2 is not None:
            reasons.append(f"{r.get('symbol')}: {g2}")
    if not reasons:
        return verdict, []
    verdict = dict(verdict)
    verdict["action"] = "investigate"
    verdict["observed_differences"] = list(verdict.get("observed_differences", [])) + reasons
    validation = dict(verdict.get("validation", {}))
    validation["unresolved"] = list(validation.get("unresolved", [])) + reasons
    validation["level"] = "unresolved"
    verdict["validation"] = validation
    return verdict, reasons
