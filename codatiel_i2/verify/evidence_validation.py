"""Deterministic evidence validation for model-directed findings.

This validator separates two questions that M7 conflated:

- authenticity: was each submitted citation actually returned by this run,
  internally consistent, bound to the pinned revisions, and replayable; and
- interpretation: whether the model's claim is true, material, or useful.

A finding can be attested only on authenticity. Attestation never decides
correctness, materiality, usefulness, publication readiness, or survival
against challenge. Substantive assessment remains explicitly unassessed.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codatiel_i2.common.paths import safe_join

VALIDATOR_VERSION = "codatiel_i2.evidence_validation.v1"

EVIDENCE_ID_RE = re.compile(r"^ev_[0-9a-f]{16}$")
SUPPORTED_EVIDENCE_SCHEMAS = ("codatiel_i2.evidence.v2",)
GIT_BLOB_ZERO = "0" * 7

EVIDENCE_CITATIONS_MISSING = "EVIDENCE_CITATIONS_MISSING"
EVIDENCE_CITATION_MALFORMED = "EVIDENCE_CITATION_MALFORMED"
EVIDENCE_NOT_RETURNED = "EVIDENCE_NOT_RETURNED"
EVIDENCE_ID_INTEGRITY = "EVIDENCE_ID_INTEGRITY"
EVIDENCE_REVISION_MISMATCH = "EVIDENCE_REVISION_MISMATCH"
EVIDENCE_SOURCE_MISMATCH = "EVIDENCE_SOURCE_MISMATCH"
EVIDENCE_SCHEMA_UNSUPPORTED = "EVIDENCE_SCHEMA_UNSUPPORTED"
EVIDENCE_REPLAY_MISMATCH = "EVIDENCE_REPLAY_MISMATCH"
ANCHOR_PATH_NOT_CHANGED = "ANCHOR_PATH_NOT_CHANGED"
ANCHOR_RANGE_INVALID = "ANCHOR_RANGE_INVALID"
SOURCE_SNAPSHOT_UNAVAILABLE = "SOURCE_SNAPSHOT_UNAVAILABLE"

# Existing non-evidence gates, preserved with stable identifiers.
DETECTOR_OUT_OF_SCOPE = "DETECTOR_OUT_OF_SCOPE"
MODEL_VERIFIED_PROOF_REJECTED = "MODEL_VERIFIED_PROOF_REJECTED"

ATTESTED = "ATTESTED"
REJECTED = "REJECTED"
BLOCKED = "BLOCKED"

_SYNTAX_RANK = 1
_NOT_RETURNED_RANK = 2
_INTEGRITY_RANK = 3
_SOURCE_RANK = 4
_REPLAY_RANK = 5
_ANCHOR_RANK = 6


@dataclass
class CitationCheck:
    index: int
    evidence_id: str
    status: str  # passed|failed|blocked
    failures: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class FindingValidation:
    state: str
    code: str | None
    citation_checks: list[CitationCheck] = field(default_factory=list)
    anchor_failures: list[tuple[str, str]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check_citation_collection(citations: Any) -> tuple[list[str], str | None]:
    """Validate cardinality/syntax without changing any submitted value."""
    if citations is None:
        return [], EVIDENCE_CITATIONS_MISSING
    if isinstance(citations, str):
        return [], (EVIDENCE_CITATIONS_MISSING if not citations.strip()
                    else EVIDENCE_CITATION_MALFORMED)
    if not isinstance(citations, list) or not citations:
        # An absent/null/empty field is missing. A non-list is malformed.
        if isinstance(citations, list):
            return [], EVIDENCE_CITATIONS_MISSING
        return [], EVIDENCE_CITATION_MALFORMED
    seen: set[str] = set()
    for value in citations:
        if not isinstance(value, str) or not value.strip():
            return [], EVIDENCE_CITATION_MALFORMED
        if value in seen or not EVIDENCE_ID_RE.fullmatch(value):
            return [], EVIDENCE_CITATION_MALFORMED
        seen.add(value)
    return list(citations), None


def _record_schema(record: dict) -> str | None:
    schema = record.get("schema_version") or ""
    return schema if schema in SUPPORTED_EVIDENCE_SCHEMAS else None


def _snapshot_problem(checkout: Path | None, expected_sha: str) -> str | None:
    from codatiel_i2.core.evidence import checkout_holds_sha

    if checkout is None or not Path(checkout).is_dir():
        return "checkout is unavailable"
    marker = Path(checkout) / ".codatiel_i2_sha"
    if not (Path(checkout) / ".git").exists():
        try:
            if marker.read_text(encoding="utf-8").strip() != expected_sha:
                return "checkout marker does not hold the pinned revision"
        except OSError as exc:
            return f"checkout marker unreadable: {type(exc).__name__}"
        return None
    try:
        if checkout_holds_sha(Path(checkout), expected_sha):
            return None
    except Exception as exc:
        return f"checkout identity check failed: {type(exc).__name__}"
    return "checkout does not hold the pinned revision"


def _read_lines(root: Path, rel: str) -> list[str] | None:
    try:
        target = safe_join(Path(root), rel)
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        return target.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _git_blob_oid(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def _parse_diff_hunks(text: str) -> tuple[list[dict], str | None]:
    header_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    hunks: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("@@"):
            match = header_re.match(line)
            if not match:
                return [], f"unparseable hunk header: {line[:120]!r}"
            base_start = int(match.group(1))
            base_count = int(match.group(2)) if match.group(2) is not None else 1
            head_start = int(match.group(3))
            head_count = int(match.group(4)) if match.group(4) is not None else 1
            current = {
                "header": line,
                "base_start": base_start,
                "base_count": base_count,
                "head_start": head_start,
                "head_count": head_count,
                "body": [],
            }
            hunks.append(current)
            continue
        if current is None:
            continue
        if line == "":
            # Git omits the leading space on blank context lines.
            current["body"].append(" ")
        elif line.startswith((" ", "-", "+")) or line.startswith("\\"):
            current["body"].append(line)
            continue
        return [], f"unsupported diff body line: {line[:120]!r}"
    if not hunks:
        return [], "diff_hunk has no parseable hunks"
    return hunks, None


def _apply_diff_hunks(base_lines: list[str], hunks: list[dict]) -> tuple[list[str] | None, str | None]:
    out: list[str] = []
    cursor = 0
    for hunk in hunks:
        start = hunk["base_start"] - 1 if hunk["base_start"] > 0 else 0
        if start < cursor:
            return None, "diff hunks overlap or are out of order"
        out.extend(base_lines[cursor:start])
        cursor = start
        for line in hunk["body"]:
            if line.startswith("\\"):
                continue
            kind, text = line[0], line[1:]
            if kind == " ":
                if cursor >= len(base_lines) or base_lines[cursor] != text:
                    return None, "diff context does not match base bytes"
                out.append(text)
                cursor += 1
            elif kind == "-":
                if cursor >= len(base_lines) or base_lines[cursor] != text:
                    return None, "diff deletion does not match base bytes"
                cursor += 1
            else:
                out.append(text)
        if cursor != start + hunk["base_count"]:
            return None, "diff hunk consumes an unexpected base range"
    out.extend(base_lines[cursor:])
    return out, None


def _hunk_head_coverage(hunks: list[dict]) -> set[int]:
    covered: set[int] = set()
    for hunk in hunks:
        covered.update(range(hunk["head_start"], hunk["head_start"] + hunk["head_count"]))
    return covered


def _canonical_no_index_body(base_file: Path, head_file: Path, context: int) -> tuple[list[str] | None, str | None]:
    """Recompute the canonical unified-diff body between two trees.

    Uses the same hermetic git settings as evidence creation. Returns the
    hunk lines (from the first `@@` header onward), or an error when the
    comparison itself cannot run.
    """
    argv = ["git", "-c", "core.quotePath=false", "-c", "diff.noprefix=false",
            "-c", "diff.mnemonicPrefix=false", "-c", "core.pager=cat",
            "-c", "diff.algorithm=myers",
            "diff", "--no-index", "--no-color", "--no-ext-diff", "--no-textconv",
            f"-U{context}", "--", str(base_file), str(head_file)]
    env = dict(os.environ)
    env.pop("GIT_EXTERNAL_DIFF", None)
    env["GIT_PAGER"] = "cat"
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"cannot recompute canonical diff: {type(exc).__name__}"
    if proc.returncode not in (0, 1):
        return None, f"cannot recompute canonical diff: {proc.stderr.decode('utf-8', 'replace')[:200]}"
    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("@@"):
            return lines[index:], None
    return [], None


def _tree_replay_diff(
    record: dict,
    *,
    base_checkout: Path,
    head_checkout: Path,
    changed_paths: set[str],
) -> list[str]:
    """Replay a v2 diff record from exported trees only.

    No git object store is required. The recorded full unified diff must
    transform the pinned base bytes into the pinned head bytes. The recorded
    abbreviated blob identifiers must prefix the blob OIDs recomputed from
    those same bytes, preventing substitution of a different file pair with
    the same textual patch shape.
    """
    from codatiel_i2.core import evidence as evidence_mod

    path = str(record.get("path") or "")
    if not path:
        return ["diff_hunk record has no path"]
    context = (record.get("tool_args") or {}).get("context_lines")
    if type(context) is not int:
        return ["diff_hunk lacks creation parameters (tool_args.context_lines as int)"]
    excerpt = str(record.get("excerpt") or "")
    if not excerpt or excerpt.strip() in ("(no diff)", ""):
        return ["diff_hunk requires the unified diff"]
    lines = excerpt.splitlines()
    if len(lines) < 5:
        return ["diff_hunk excerpt is truncated"]
    if path not in changed_paths:
        return [f"diff_hunk path {path!r} is not in the changed set"]

    base_target = safe_join(Path(base_checkout), path) if _read_lines(base_checkout, path) is not None else None
    head_target = safe_join(Path(head_checkout), path) if _read_lines(head_checkout, path) is not None else None
    # Re-read bytes (not decoded lines) for blob-OID calculation.
    try:
        base_bytes = base_target.read_bytes() if base_target is not None else None
    except OSError:
        base_bytes = None
    try:
        head_bytes = head_target.read_bytes() if head_target is not None else None
    except OSError:
        head_bytes = None
    if base_bytes is None and head_bytes is None:
        return [f"diff path {path!r} is absent from both pinned trees"]
    base_oid = _git_blob_oid(base_bytes).lower() if base_bytes is not None else None
    head_oid = _git_blob_oid(head_bytes).lower() if head_bytes is not None else None

    expected_first = f"diff --git a/{path} b/{path}"
    if lines[0] != expected_first:
        return ["diff_hunk file header does not match the cited path"]
    pos = 1
    recorded_mode: str | None = None
    if lines[pos].startswith("new file mode "):
        recorded_mode = lines[pos][len("new file mode "):].strip()
        if base_bytes is not None:
            return ["diff_hunk claims a new file already present at base"]
        pos += 1
    elif lines[pos].startswith("deleted file mode "):
        recorded_mode = lines[pos][len("deleted file mode "):].strip()
        if head_bytes is not None:
            return ["diff_hunk claims a deleted file still present at head"]
        pos += 1
    if pos >= len(lines) or not lines[pos].startswith("index "):
        return ["diff_hunk excerpt is missing its index line"]
    index_match = re.fullmatch(r"index ([0-9a-f]+)\.\.([0-9a-f]+)(?: ([0-9]+))?", lines[pos])
    if not index_match:
        return ["diff_hunk index line is malformed"]
    old_abbrev, new_abbrev, index_mode = index_match.groups()
    expected_old = "0000000" if base_bytes is None else (base_oid or "")[: len(old_abbrev)]
    expected_new = "0000000" if head_bytes is None else (head_oid or "")[: len(new_abbrev)]
    if len(old_abbrev) < 4 or old_abbrev != expected_old:
        return ["diff_hunk base blob identifier does not match pinned base bytes"]
    if len(new_abbrev) < 4 or new_abbrev != expected_new:
        return ["diff_hunk head blob identifier does not match pinned head bytes"]
    if recorded_mode is None:
        recorded_mode = index_mode
    if not recorded_mode:
        return ["diff_hunk excerpt is missing its file mode"]
    pos += 1
    expected_minus = "/dev/null" if base_bytes is None else f"a/{path}"
    expected_plus = "/dev/null" if head_bytes is None else f"b/{path}"
    if pos + 1 >= len(lines) or lines[pos] != f"--- {expected_minus}" or lines[pos + 1] != f"+++ {expected_plus}":
        return ["diff_hunk file markers do not match the pinned trees"]
    # Canonical body comparison: the recorded hunk text must equal what git
    # itself computes between the pinned trees at the recorded context width.
    # An internally consistent forgery (recomputed ID over a doctored excerpt,
    # e.g. an inflated full-rewrite hunk) fails here even though its ID and
    # blob bindings check out.
    canonical_body, canonical_error = _canonical_no_index_body(
        safe_join(Path(base_checkout), path) if base_bytes is not None else Path(os.devnull),
        safe_join(Path(head_checkout), path) if head_bytes is not None else Path(os.devnull),
        context)
    if canonical_error:
        return [canonical_error]
    archived_body = lines[pos + 2 :]
    if archived_body != canonical_body and not (
            canonical_body and canonical_body[-1] == " " and archived_body == canonical_body[:-1]):
        # The single tolerated divergence is historical storage stripping
        # git's trailing whitespace: a diff ending in a blank context line
        # loses that final blank line in every archived record.
        return ["diff_hunk does not match the canonical BASE..HEAD diff (forged or stale diff text)"]
    hunks, error = _parse_diff_hunks("\n".join(lines[pos + 2 :]))
    if error:
        return [error]
    base_lines = (base_bytes or b"").decode("utf-8", errors="replace").splitlines()
    head_lines = (head_bytes or b"").decode("utf-8", errors="replace").splitlines()
    applied, error = _apply_diff_hunks(base_lines, hunks)
    if error and hunks and hunks[-1]["body"] and hunks[-1]["body"][-1] != " ":
        # Historical storage strips git's trailing whitespace. A unified diff
        # ending in a blank context line therefore loses that final blank
        # line in every archived record. Restore exactly one such line as a
        # decoder normalization; it must still apply byte-for-byte.
        trailing = {"header": hunks[-1]["header"], "base_start": hunks[-1]["base_start"],
                    "base_count": hunks[-1]["base_count"], "head_start": hunks[-1]["head_start"],
                    "head_count": hunks[-1]["head_count"], "body": hunks[-1]["body"] + [" "]}
        applied, error = _apply_diff_hunks(base_lines, hunks[:-1] + [trailing])
        if error is None:
            hunks = hunks[:-1] + [trailing]
    if error:
        return [error]
    if applied != head_lines:
        return ["diff_hunk does not transform pinned base bytes into pinned head bytes"]

    ls, le = record.get("line_start"), record.get("line_end")
    if not isinstance(ls, int) or not isinstance(le, int) or ls < 1 or le < ls:
        return ["diff_hunk anchor range is invalid"]
    if head_bytes is not None:
        if not set(range(ls, le + 1)) <= _hunk_head_coverage(hunks):
            return [f"diff_hunk anchor {ls}-{le} lies outside the hunk coverage of {path!r}"]
        # Also run the historical anchor-range rule so accepted historical and
        # newly emitted v2 records share one observable outcome.
        anchor_errors = evidence_mod._check_diff_anchor(excerpt, ls, le, path)
        if anchor_errors:
            return anchor_errors
    # For deleted files there is no head side by construction; the record's
    # anchor range cannot lie inside head-side hunk coverage. The finding's
    # own primary anchor is still validated independently.
    return []


def _validate_source_binding(record: dict, *, base_sha: str, head_sha: str) -> list[tuple[str, str]]:
    schema = _record_schema(record)
    if schema is None:
        return [(EVIDENCE_SCHEMA_UNSUPPORTED, f"unknown evidence schema: {record.get('schema_version')!r}")]
    kind = record.get("kind")
    revision = record.get("revision")
    repository_kinds = {"source_excerpt": ("base", "head"), "file_state": ("base", "head"), "diff_hunk": ("diff",)}
    if kind not in repository_kinds and kind not in ("external_document", "witness", "analyzer_result"):
        return [(EVIDENCE_SCHEMA_UNSUPPORTED, f"unknown evidence kind: {kind!r}")]
    if kind in repository_kinds and revision not in repository_kinds[kind]:
        return [(EVIDENCE_SCHEMA_UNSUPPORTED, f"kind {kind!r} cannot use revision {revision!r}")]
    if kind in repository_kinds:
        if record.get("base_sha") != base_sha or record.get("head_sha") != head_sha:
            return [(EVIDENCE_SOURCE_MISMATCH,
                     "evidence belongs to a different BASE/HEAD pair than this review")]
        if revision in ("base", "head"):
            if record.get("revision_sha") != (base_sha if revision == "base" else head_sha):
                return [(EVIDENCE_REVISION_MISMATCH,
                         f"{revision} citation does not carry the pinned {revision} SHA")]
            return []
        # diff_hunk under its own recorded schema.
        if record.get("revision_sha") != head_sha:
            return [(EVIDENCE_REVISION_MISMATCH,
                     "diff citation does not carry the head end of its recorded BASE..HEAD range")]
        args = record.get("tool_args")
        if not isinstance(args, dict) or type(args.get("context_lines")) is not int:
            return [(EVIDENCE_SCHEMA_UNSUPPORTED,
                     "diff_hunk lacks creation parameters (tool_args.context_lines as int)")]
        return []
    if revision not in ("external", "generated"):
        return [(EVIDENCE_REVISION_MISMATCH, f"non-repository evidence has unsupported revision {revision!r}")]
    if not record.get("revision_sha"):
        return [(EVIDENCE_REVISION_MISMATCH, "non-repository evidence needs an explicit revision_sha")]
    return []


def _validate_anchor(
    finding: dict,
    *,
    base_checkout: Path | None,
    head_checkout: Path | None,
    base_sha: str,
    head_sha: str,
    changed_paths: set[str],
) -> tuple[list[tuple[str, str]], bool]:
    anchor = finding.get("changed_anchor") or {
        "path": finding.get("path", ""),
        "line_start": finding.get("line_start", 0),
        "line_end": finding.get("line_end", 0),
    }
    path = anchor.get("path", "")
    ls, le = anchor.get("line_start"), anchor.get("line_end")
    revision = finding.get("revision", "head")
    if not path or path not in changed_paths:
        return [(ANCHOR_PATH_NOT_CHANGED, f"anchor path {path!r} is not in the changed set")], False
    if revision not in ("base", "head"):
        return [(ANCHOR_RANGE_INVALID, f"anchor revision {revision!r} must be base|head")], False
    checkout = base_checkout if revision == "base" else head_checkout
    expected_sha = base_sha if revision == "base" else head_sha
    problem = _snapshot_problem(checkout, expected_sha)
    if problem:
        return [], True
    lines = _read_lines(Path(checkout), path) if checkout is not None else None
    if lines is None:
        return [(ANCHOR_RANGE_INVALID, f"anchor file not readable at {revision}: {path!r}")], False
    if not isinstance(ls, int) or not isinstance(le, int) or ls < 1 or le < ls or ls > len(lines) or le > len(lines):
        return [(ANCHOR_RANGE_INVALID, f"anchor range {ls}-{le} does not replay at {revision}")], False
    return [], False


def validate_model_finding(
    finding: dict,
    evidence_by_id: dict[str, dict],
    *,
    base_sha: str,
    head_sha: str,
    base_checkout: Path | None,
    head_checkout: Path | None,
    changed_paths: set[str],
) -> FindingValidation:
    """Validate one raw model finding's evidence and primary anchor."""
    from codatiel_i2.core import evidence as evidence_mod

    citations, collection_error = check_citation_collection(finding.get("evidence_ids"))
    checks: list[CitationCheck] = []
    failures: list[tuple[int, int, str, str]] = []
    blockers: list[str] = []
    if collection_error:
        anchor_failures, anchor_blocked = _validate_anchor(
            finding, base_checkout=base_checkout, head_checkout=head_checkout,
            base_sha=base_sha, head_sha=head_sha, changed_paths=changed_paths)
        if anchor_blocked:
            blockers.append(f"{SOURCE_SNAPSHOT_UNAVAILABLE}: anchor range could not be replayed")
        failures.extend((_ANCHOR_RANK, 0, code, message) for code, message in anchor_failures)
        code = collection_error
        state = REJECTED
        return FindingValidation(state=state, code=code, citation_checks=checks,
                                 anchor_failures=anchor_failures, blockers=blockers)

    for index, evidence_id in enumerate(citations):
        check = CitationCheck(index=index, evidence_id=evidence_id, status="passed")
        record = evidence_by_id.get(evidence_id)
        if record is None:
            check.status = "failed"
            check.failures.append((EVIDENCE_NOT_RETURNED,
                                   f"{evidence_id} is not in this execution's returned-evidence ledger"))
            failures.append((_NOT_RETURNED_RANK, index, EVIDENCE_NOT_RETURNED, check.failures[-1][1]))
            checks.append(check)
            continue
        if _record_schema(record) is not None:
            try:
                if evidence_mod.expected_evidence_id(record) != evidence_id:
                    check.status = "failed"
                    check.failures.append((EVIDENCE_ID_INTEGRITY,
                                           f"{evidence_id} does not match its recorded contents"))
                    failures.append((_INTEGRITY_RANK, index, EVIDENCE_ID_INTEGRITY, check.failures[-1][1]))
            except Exception as exc:
                check.status = "failed"
                check.failures.append((EVIDENCE_ID_INTEGRITY,
                                       f"{evidence_id} integrity recompute failed: {type(exc).__name__}"))
                failures.append((_INTEGRITY_RANK, index, EVIDENCE_ID_INTEGRITY, check.failures[-1][1]))
                checks.append(check)
                continue
        source_failures = _validate_source_binding(record, base_sha=base_sha, head_sha=head_sha)
        if source_failures:
            check.status = "failed"
            check.failures.extend(source_failures)
            failures.extend((_SOURCE_RANK, index, code, message) for code, message in source_failures)
            checks.append(check)
            continue
        # Replay every citation whose binding checks passed. Source/file
        # excerpts use the historical byte replay. Diffs from archive-style
        # checkouts (no git object store) use the tree replay over the same
        # pinned bytes; diffs from live git worktrees keep the historical
        # recomputed-diff replay.
        replay_errors: list[str] = []
        use_tree_diff = (
            record.get("kind") == "diff_hunk"
            and base_checkout is not None and head_checkout is not None
            and not (Path(base_checkout) / ".git").exists()
            and not (Path(head_checkout) / ".git").exists()
        )
        if record.get("kind") == "diff_hunk" and use_tree_diff:
            replay_errors = _tree_replay_diff(record, base_checkout=Path(base_checkout),
                                              head_checkout=Path(head_checkout),
                                              changed_paths=changed_paths)
        elif record.get("kind") not in ("source_excerpt", "file_state", "diff_hunk"):
            # Non-repository evidence (external_document, witness,
            # analyzer_result) has no pinned-tree byte replay under the
            # recorded schema. Integrity plus source binding already passed
            # above, matching the historical core behavior of a vacuous pass.
            replay_errors = []
        else:
            required = {"base": base_checkout, "head": head_checkout, "diff": None}.get(
                str(record.get("revision")))
            if record.get("revision") == "diff":
                missing = [label for label, checkout, sha in
                           (("base", base_checkout, base_sha), ("head", head_checkout, head_sha))
                           if _snapshot_problem(checkout, sha)]
                if missing:
                    check.status = "blocked"
                    blockers.append(f"{SOURCE_SNAPSHOT_UNAVAILABLE}: diff replay needs both pinned trees")
                    checks.append(check)
                    continue
            elif _snapshot_problem(required, base_sha if record.get("revision") == "base" else head_sha):
                check.status = "blocked"
                blockers.append(f"{SOURCE_SNAPSHOT_UNAVAILABLE}: cited {record.get('revision')} tree is unavailable")
                checks.append(check)
                continue
            replay_errors = evidence_mod.replay_source_evidence(
                record, base_checkout=base_checkout, head_checkout=head_checkout,
                changed_paths=changed_paths)
        if replay_errors:
            check.status = "failed"
            check.failures.append((EVIDENCE_REPLAY_MISMATCH,
                                   f"{evidence_id}: {replay_errors[0]}"))
            failures.append((_REPLAY_RANK, index, EVIDENCE_REPLAY_MISMATCH, check.failures[-1][1]))
        checks.append(check)

    anchor_failures, anchor_blocked = _validate_anchor(
        finding, base_checkout=base_checkout, head_checkout=head_checkout,
        base_sha=base_sha, head_sha=head_sha, changed_paths=changed_paths)
    if anchor_blocked:
        blockers.append(f"{SOURCE_SNAPSHOT_UNAVAILABLE}: anchor range could not be replayed")
    failures.extend((_ANCHOR_RANK, len(citations), code, message) for code, message in anchor_failures)

    if failures:
        failures.sort(key=lambda item: (item[0], item[1]))
        return FindingValidation(state=REJECTED, code=failures[0][2], citation_checks=checks,
                                 anchor_failures=anchor_failures, blockers=blockers)
    if blockers or any(check.status == "blocked" for check in checks):
        return FindingValidation(state=BLOCKED, code=SOURCE_SNAPSHOT_UNAVAILABLE,
                                 citation_checks=checks, anchor_failures=anchor_failures,
                                 blockers=blockers)
    return FindingValidation(state=ATTESTED, code=None, citation_checks=checks,
                             anchor_failures=anchor_failures, blockers=blockers)
