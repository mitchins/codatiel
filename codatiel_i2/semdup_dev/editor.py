"""Sceptical review-comment editor (SEMDUP-06, DEV-only, report-only).

Two-stage reviewer, replacing semantic-verdict reconciliation as the
publication authority:

- Stage 1 (Terra, unchanged v0.2 output): proposes a review comment
  identifying new/existing implementations, shared functionality and a
  reuse opportunity. Permissive; a candidate comment, not a finding.
- Stage 2 (Luna, this module): decides whether the comment is worth
  bothering a developer with. KEEP / REVISE (narrow) / DROP.
  DROP needs no proof of incompatibility — insufficient evidence of
  usefulness is sufficient.

The editor does not depend on model tool calls. The evidence package is
assembled deterministically: both implementations with bounded file
context (above/below windows plus file head) at the reviewed revision,
with exact source locations. If the package is insufficient, the
reviewer is instructed to DROP as inadequately supported.

Publication authority = editor decision + anchor validation + the
deterministic wrapper pre-filter. The SEMDUP-03/04/05 classify and
reconcile paths are preserved untouched for baseline comparison.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable

EDITOR_INSTRUCTIONS_VERSION = "semdup-comment-editor-0.1"

EDITOR_INSTRUCTIONS = """You are the second reviewer deciding whether a proposed code-review comment about possible code duplication is worth sending to a developer. Be sceptical: similar algorithms, patterns and general responsibilities are NOT sufficient to publish. Publish only comments that identify a concrete opportunity to reuse existing implementation or extract a concrete shared component.

You receive a proposed comment (new and existing implementations, claimed shared functionality, reuse opportunity, known differences) plus the source evidence: both implementations in file context at the reviewed revision.

Decide exactly one of:
- KEEP: the comment identifies a concrete, useful reuse or extraction opportunity, and its differences and risks are accurate as stated. Replacing one implementation with an equivalent existing one counts as reuse; narrowing to an extracted part is for partial overlaps, not a requirement for every comment.
- REVISE: a narrower useful observation exists (for example shared normalization logic, not wholesale replacement). Rewrite the comment to only that narrowed claim, precisely.
- DROP: the comment is too generic (a shared abstraction with no concrete reusable piece), misleading, unsupported by the delivered evidence, or otherwise not worth the developer's attention.

Rules:
- Differences that prevent direct replacement do not automatically invalidate a useful shared-part observation — but a shared abstraction with no concrete reusable component is noise: drop it.
- Do not publish a suggestion merely because the two implementations are related.
- If the delivered evidence is insufficient to judge (missing code or data, unverified values), DROP as inadequately supported. Do not guess.
- The revised comment must describe only code present in the delivered evidence; introduce no new file locations or claims about unseen code.

Respond with exactly one JSON object, no prose:
{"decision": "KEEP|REVISE|DROP",
 "revised_comment": "the comment to publish (KEEP may restate the proposal briefly; REVISE narrows it; DROP uses \\"\\")",
 "reason": "why, citing the delivered evidence"}"""

EDITOR_DECISIONS = ("KEEP", "REVISE", "DROP")

# Bounded deterministic context windows (lines).
CONTEXT_ABOVE = 120
CONTEXT_BELOW = 30
FILE_HEAD_LINES = 30

_LOCATION_RE = re.compile(
    r"(?P<path>[\w./-]+\.(?:py|php|swift|js|ts|java|go|rb))::?"
    r"(?P<sym>[\w$#@][\w$#.@]*)?(?::(?P<lines>\d+(?:-\d+)?))?")


def editor_instructions_id() -> str:
    return (f"semdup-comment-editor-0.1@"
            f"{hashlib.sha256(EDITOR_INSTRUCTIONS.encode()).hexdigest()[:12]}")


# --------------------------------------------------------------------------
# Deterministic evidence package (no inference).
# --------------------------------------------------------------------------

def _file_text_at_rev(repo_path: Path, rev: str, path: str) -> dict:
    """Read one file at a pinned revision. Never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "show", f"{rev}:{path}"],
            capture_output=True, check=False, timeout=120)
    except Exception as exc:  # noqa: BLE001 - unreadable => explicit miss
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False,
                "error": proc.stderr.decode(errors="replace")[:200]}
    return {"ok": True, "text": proc.stdout.decode(errors="replace")}


def build_evidence_package(merged_db: Path, repo_path: Path, rev: str,
                           target: dict, counterpart: dict) -> dict:
    """Assemble the editor's evidence package deterministically.

    Both implementations resolved exactly in the pinned merged DB, plus
    bounded file context (above/below windows and file head) read at the
    pinned revision. Module-level data above a unit (e.g. lookup tables)
    is included whenever it falls inside the above-window.
    """
    from codatiel_i2.semdup_dev import advisory as AV
    sides = {}
    for label, unit in (("target", target), ("counterpart", counterpart)):
        path, symbol = unit.get("path"), unit.get("symbol")
        if not path or not symbol:
            return {"ok": False, "code": f"{label}_unresolved",
                    "detail": "missing path/symbol"}
        if "#region" in symbol:
            return {"ok": False, "code": "fragment_source",
                    "detail": f"{label} is a region fragment"}
        anchor = AV.validate_anchor(merged_db, path, symbol)
        if not anchor.get("ok"):
            return {"ok": False, "code": f"{label}_unresolved",
                    "detail": anchor.get("reason", "")}
        text = anchor["text"]
        if not (text or "").strip():
            return {"ok": False, "code": f"{label}_unresolved",
                    "detail": "empty unit text"}
        sides[label] = {
            "path": anchor["path"], "symbol": anchor["symbol"],
            "start_line": anchor["start_line"], "end_line": anchor["end_line"],
            "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    files: dict[str, dict] = {}
    for label in ("target", "counterpart"):
        side = sides[label]
        path = side["path"]
        if path in files:
            continue
        got = _file_text_at_rev(repo_path, rev, path)
        if not got["ok"]:
            return {"ok": False, "code": "file_unreadable",
                    "detail": f"{path}: {got['error']}"}
        lines = got["text"].splitlines()
        total = len(lines)
        s, e = side["start_line"], side["end_line"]
        above_lo = max(1, s - CONTEXT_ABOVE)
        below_hi = min(total, e + CONTEXT_BELOW)
        head = lines[:FILE_HEAD_LINES] \
            if s - 1 > FILE_HEAD_LINES else []
        files[path] = {
            "total_lines": total,
            "head_lines": head,
            "window_start": above_lo,
            "window_end": below_hi,
            "window_lines": lines[above_lo - 1:below_hi],
            "complete": above_lo == 1 and below_hi == total,
        }
    return {"ok": True, "rev": rev, "target": sides["target"],
            "counterpart": sides["counterpart"], "files": files}


def render_editor_input(package: dict, proposed_comment: str) -> str:
    """Assemble the exact model input: proposal + full evidence package."""
    t, c = package["target"], package["counterpart"]
    parts = ["## Proposed review comment under editorial review",
             proposed_comment, "",
             "## Source evidence (pinned revision "
             f"{package['rev']}; both implementations complete below)"]
    for label, side in (("New/changed", t), ("Existing", c)):
        parts += [f"### {label}: {side['path']}::{side['symbol']} "
                  f"(lines {side['start_line']}-{side['end_line']})",
                  "```", side["text"], "```", ""]
    for path, f in package["files"].items():
        parts += [f"### File context: {path} "
                  f"({f['total_lines']} lines"
                  f"{', complete' if f['complete'] else ', windowed'})"]
        if f["head_lines"] and (f["window_start"] > FILE_HEAD_LINES + 1):
            parts += [f"--- file head (lines 1-{len(f['head_lines'])}) ---",
                      "```", "\n".join(f["head_lines"]), "```"]
        parts += [f"--- window (lines {f['window_start']}-"
                  f"{f['window_end']}) ---",
                  "```", "\n".join(f["window_lines"]), "```", ""]
    parts += ["Decide KEEP, REVISE or DROP per your instructions. "
              "The windowed file context above is the complete evidence; "
              "do not assume unseen code."]
    return "\n".join(parts)


def propose_comment(terra: dict, evidence: dict) -> str:
    """Deterministically assemble Terra's candidate comment."""
    t, c = evidence["target"], evidence["counterpart"]
    opp = terra.get("opportunity", "none")
    opp_text = {"direct_reuse": "direct reuse of the existing implementation",
                "partial_extraction": "extracting the shared component",
                "none": "no concrete reuse proposed"}.get(opp, opp)
    lines = [
        f"New code `{t['path']}::{t['symbol']}` "
        f"(lines {t['start_line']}-{t['end_line']}) may overlap with existing "
        f"`{c['path']}::{c['symbol']}` "
        f"(lines {c['start_line']}-{c['end_line']}).",
        f"Shared functionality: {terra.get('shared_component') or terra.get('shared_behavior', '')}",
        f"Reuse opportunity: {opp_text}.",
    ]
    diffs = [str(x) for x in (terra.get("differences") or [])]
    if diffs:
        lines += ["Known differences:"] + [f"- {x}" for x in diffs]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Editor output validation + decision mapping.
# --------------------------------------------------------------------------

def validate_editor_output(value: dict) -> list[str]:
    """Schema + coherence checks on a Luna edit decision."""
    if not isinstance(value, dict):
        return ["not an object"]
    errors = []
    for field in ("decision", "revised_comment", "reason"):
        if field not in value:
            errors.append(f"missing field: {field}")
    if errors:
        return errors
    if value.get("decision") not in EDITOR_DECISIONS:
        errors.append(f"bad decision: {value.get('decision')!r}")
    if value.get("decision") in ("KEEP", "REVISE"):
        if not str(value.get("revised_comment", "")).strip():
            errors.append(f"{value['decision']} requires revised_comment")
        if not str(value.get("reason", "")).strip():
            errors.append(f"{value['decision']} requires reason")
    else:
        if not str(value.get("reason", "")).strip():
            errors.append("DROP requires reason")
    return errors


def check_comment_locations(revised_comment: str,
                            evidence_files: list[str]) -> list[str]:
    """Every code location mentioned must resolve to delivered evidence.

    Returns violation descriptions (empty = clean). A violation means
    the revised comment introduces unseen code and cannot be published.
    """
    violations = []
    for match in _LOCATION_RE.finditer(revised_comment or ""):
        path = match.group("path")
        if path not in (evidence_files or []):
            violations.append(f"unseen location referenced: {path}")
    return violations


def edit_pair(*, package: dict, proposed_comment: str, tools: list,
              model: Any, model_settings: Any, ledger: Any, ctx: Any = None,
              call: Callable[..., Any] | None = None,
              max_turns: int = 4) -> dict:
    """Run Luna's editorial pass on one proposed comment. Never raises."""
    from codatiel_i2 import runtime_sdk as R
    fn = call or R.run_review
    user_input = render_editor_input(package, proposed_comment)
    try:
        result = fn(instructions=EDITOR_INSTRUCTIONS, user_input=user_input,
                    tools=tools, model=model, max_turns=max_turns,
                    model_settings=model_settings, usage_ledger=ledger,
                    session=None)
        if isinstance(result, dict):
            ok = bool(result.get("ok", True))
            final = str(result.get("final_text", "") or "")
            wall = result.get("wall_s", 0.0)
            tool_log = result.get("tool_calls", [])
            error = str(result.get("error", "") or "")
        else:
            ok, final = True, str(getattr(result, "final_output", "") or "")
            wall, error = 0.0, ""
            tool_log = list((ctx.tool_log if ctx is not None else []) or [])
    except Exception as exc:  # noqa: BLE001 - failure is a recorded outcome
        ok, final, error = False, "", f"{type(exc).__name__}: {exc}"
        wall = 0.0
        tool_log = list((ctx.tool_log if ctx is not None else []) or [])
    transcript = {"instructions_id": editor_instructions_id(),
                  "user_input": user_input,
                  "tool_calls": tool_log,
                  "final_text": final[:4000],
                  "wall_s": wall}
    if not ok:
        transcript.update({"status": "editor_failed", "error": error})
        return {"ok": False, "transcript": transcript, "error": error,
                "editor": None}
    import re as _re
    try:
        body = final[final.find("{"):final.rfind("}") + 1]
        value = json.loads(body)
    except (ValueError, TypeError):
        try:
            cleaned = _re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]", "", final)
            body = cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]
            value = json.loads(body)
        except (ValueError, TypeError) as exc:
            transcript.update({"status": "editor_unreadable"})
            return {"ok": False, "transcript": transcript,
                    "error": f"unparseable editor output: {exc}",
                    "editor": None}
    errors = validate_editor_output(value)
    if errors:
        transcript.update({"status": "editor_invalid", "errors": errors})
        return {"ok": False, "transcript": transcript,
                "error": "invalid editor output: " + "; ".join(errors),
                "editor": None}
    paths = [package["target"]["path"], package["counterpart"]["path"]]
    violations = check_comment_locations(value.get("revised_comment", ""),
                                         paths)
    if violations and value.get("decision") in ("KEEP", "REVISE"):
        transcript.update({"status": "editor_unseen_reference",
                           "violations": violations})
        return {"ok": False, "transcript": transcript,
                "error": "revised comment references unseen code: "
                         + "; ".join(violations),
                "editor": None}
    transcript.update({"status": "edited",
                       "decision": value.get("decision")})
    return {"ok": True, "transcript": transcript, "editor": value,
            "error": ""}


def wrapper_prefilter(target_text: str, counterpart_symbol: str) -> dict:
    """Deterministic delegation check: target calls the counterpart.

    A wrapper call is not an independent re-implementation; such pairs
    never reach the editor (no model spend).
    """
    sym = (counterpart_symbol or "").split("#")[0].split("@")[0].lstrip("$")
    hit = bool(sym) and f"{sym}(" in (target_text or "")
    return {"wrapper": hit,
            "detail": f"target calls {sym}()" if hit else ""}
