"""Evidence envelope v2: every item names the exact revision supplying its bytes.

Kinds (tagged union):
- source_excerpt: lines read from base or head checkout (excerpt required)
- diff_hunk: unified diff over base..head (anchored to changed path)
- file_state: a fact about a file (empty/absent/binary) — no excerpt
- external_document: vendored/official docs (revision_sha = source version/digest)
- witness: executable witness output (revision_sha = run or commit)
- analyzer_result: native analyzer diagnostic output

`revision` is one of base|head|diff|external|generated and `revision_sha`
is the exact SHA (or source version) those bytes came from. The evidence ID
incorporates revision_sha, so reading identical unchanged bytes from base
and head yields distinct provenance records.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codatiel_i2.common.git import GitError, git, rev_parse
from codatiel_i2.common.hashing import sha256_text, short_id
from codatiel_i2.common.paths import safe_join
from codatiel_i2.core.workspace import SHA_MARKER

SCHEMA_VERSION = "codatiel_i2.evidence.v2"

KINDS = (
    "source_excerpt",
    "diff_hunk",
    "file_state",
    "external_document",
    "witness",
    "analyzer_result",
)

REVISIONS = ("base", "head", "diff", "external", "generated")

# Allowed kind × revision pairs. Repository bytes come only from base/head;
# diffs only from the compared range; external claims only from recorded
# sources; witness/analyzer output only from generated runs.
KIND_REVISIONS = {
    "source_excerpt": ("base", "head"),
    "diff_hunk": ("diff",),
    "file_state": ("base", "head"),
    "external_document": ("external",),
    "witness": ("generated",),
    "analyzer_result": ("generated",),
}

FILE_FACTS = ("empty", "absent", "binary", "present")

EMPTY_DIFF_PLACEHOLDERS = ("(no diff)", "")


@dataclass
class Evidence:
    evidence_id: str
    kind: str
    tool: str
    base_sha: str
    head_sha: str
    revision: str
    revision_sha: str
    path: str
    line_start: int
    line_end: int
    excerpt: str
    content_digest: str
    fact: str = ""
    tool_args: dict | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "tool": self.tool,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "revision": self.revision,
            "revision_sha": self.revision_sha,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "excerpt": self.excerpt,
            "content_digest": self.content_digest,
            "fact": self.fact,
            "tool_args": self.tool_args or {},
        }


def diff_git_argv(base_sha: str, head_sha: str, path: str, context_lines: int) -> list[str]:
    """Canonical git diff invocation. Creation and replay MUST use this.

    Pinned hermetically: external diff drivers, textconv, prefix styles and
    pagers come from the caller's environment/config and would otherwise let
    GIT_EXTERNAL_DIFF forge replay output or silently reformat bytes.
    """
    if type(context_lines) is not int or not (0 <= context_lines <= 100):
        raise ValueError(f"context_lines must be an int in 0..100, got {context_lines!r}")
    return ["-c", "core.quotePath=false", "-c", "diff.noprefix=false",
            "-c", "diff.mnemonicPrefix=false", "-c", "core.pager=cat",
            "diff", "--no-ext-diff", "--no-textconv", f"-U{context_lines}",
            f"{base_sha}..{head_sha}", "--", path]


def make_evidence(*, kind: str = "source_excerpt", tool: str, base_sha: str,
                  head_sha: str, revision: str, path: str, line_start: int,
                  line_end: int, excerpt: str = "", fact: str = "",
                  revision_sha: str = "", tool_args: dict | None = None) -> Evidence:
    if kind not in KINDS:
        raise ValueError(f"unknown evidence kind: {kind!r}")
    if revision not in REVISIONS:
        raise ValueError(f"unknown revision: {revision!r}")
    if revision not in KIND_REVISIONS[kind]:
        raise ValueError(f"kind {kind!r} cannot use revision {revision!r}")
    if line_start < 1 or line_end < line_start:
        raise ValueError(f"invalid line range: {line_start}-{line_end}")
    if not revision_sha:
        if revision == "base":
            revision_sha = base_sha
        elif revision == "head":
            revision_sha = head_sha
        elif revision == "diff":
            revision_sha = head_sha  # head end of the compared base..head range
    if not revision_sha:
        # No anonymous provenance: external/generated callers must name the
        # source version or run that supplied the bytes.
        raise ValueError(f"revision {revision!r} requires an explicit revision_sha")
    if kind == "source_excerpt":
        if not excerpt:
            raise ValueError("source_excerpt requires a non-empty excerpt (empty files use file_state)")
    elif kind == "file_state":
        if fact not in FILE_FACTS:
            raise ValueError(f"file_state requires fact in {FILE_FACTS}")
        if excerpt:
            raise ValueError("file_state carries a fact, not an excerpt")
    elif kind == "diff_hunk":
        if not excerpt or excerpt.strip() in EMPTY_DIFF_PLACEHOLDERS:
            raise ValueError("diff_hunk requires the actual unified diff, not a placeholder")
    elif not excerpt and not fact:
        raise ValueError(f"{kind} requires an excerpt or fact to bind the digest to")
    digest = _digest_for(kind, excerpt, fact)
    from codatiel_i2.common.jsonio import canonical_json

    tool_args = tool_args or {}
    tool_args_digest = sha256_text(canonical_json(tool_args))
    eid = "ev_" + short_id(kind, tool, base_sha, head_sha, revision, revision_sha,
                            path, str(line_start), str(line_end), digest,
                            tool_args_digest)
    return Evidence(eid, kind, tool, base_sha, head_sha, revision, revision_sha,
                    path, line_start, line_end, excerpt, digest, fact, tool_args or {})


def expected_evidence_id(row: dict) -> str:
    """Recompute the ID from the row's own fields. Forged IDs fail comparison."""
    kind = row.get("kind", "")
    digest = _digest_for(str(row.get("kind") or ""), str(row.get("excerpt") or ""),
                         str(row.get("fact") or ""))
    from codatiel_i2.common.jsonio import canonical_json

    tool_args = row.get("tool_args") or {}
    tool_args_digest = sha256_text(canonical_json(tool_args))
    return "ev_" + short_id(kind, str(row.get("tool") or ""), str(row.get("base_sha") or ""),
                            str(row.get("head_sha") or ""), str(row.get("revision") or ""),
                            str(row.get("revision_sha") or ""), str(row.get("path") or ""),
                            str(row.get("line_start") or ""), str(row.get("line_end") or ""),
                            digest, tool_args_digest)


def _digest_for(kind: str, excerpt: str, fact: str) -> str:
    if kind == "file_state":
        return sha256_text(fact)
    return sha256_text(excerpt or fact)


def validate_envelope(row: dict) -> list[str]:
    errors: list[str] = []
    for key in ("evidence_id", "kind", "tool", "base_sha", "head_sha", "revision",
                "revision_sha", "path", "line_start", "line_end", "excerpt", "content_digest"):
        if key not in row:
            errors.append(f"missing field: {key}")
    if row.get("kind") not in KINDS:
        errors.append(f"unknown kind: {row.get('kind')!r}")
    if row.get("revision") not in REVISIONS:
        errors.append(f"unknown revision: {row.get('revision')!r}")
    if row.get("kind") in KIND_REVISIONS and row.get("revision") not in KIND_REVISIONS[row["kind"]]:
        errors.append(f"kind {row.get('kind')!r} cannot use revision {row.get('revision')!r}")
    if not row.get("revision_sha"):
        errors.append("revision_sha is required (no anonymous provenance)")
    if row.get("revision") == "base" and row.get("revision_sha") != row.get("base_sha"):
        errors.append("base evidence must carry revision_sha == base_sha")
    if row.get("revision") == "head" and row.get("revision_sha") != row.get("head_sha"):
        errors.append("head evidence must carry revision_sha == head_sha")
    ls, le = row.get("line_start"), row.get("line_end")
    if isinstance(ls, int) and isinstance(le, int) and (ls < 1 or le < ls):
        errors.append(f"invalid line range: {ls}-{le}")
    if row.get("kind") == "source_excerpt" and not row.get("excerpt"):
        errors.append("source_excerpt requires a non-empty excerpt")
    if row.get("kind") == "file_state" and row.get("fact") not in FILE_FACTS:
        errors.append(f"file_state requires fact in {FILE_FACTS}")
    if row.get("kind") == "diff_hunk" and (not row.get("excerpt") or str(row.get("excerpt")).strip() in EMPTY_DIFF_PLACEHOLDERS):
        errors.append("diff_hunk requires the actual unified diff")
    if row.get("kind") == "diff_hunk":
        args = row.get("tool_args")
        if not isinstance(args, dict) or type(args.get("context_lines")) is not int:
            errors.append("diff_hunk requires tool_args.context_lines as an int (creation parameters)")
    # Digest + ID recompute for every kind: forged digests/IDs fail here.
    if isinstance(row.get("excerpt"), str) and isinstance(row.get("content_digest"), str):
        if _digest_for(str(row.get("kind") or ""), row.get("excerpt") or "",
                       str(row.get("fact") or "")) != row["content_digest"]:
            errors.append("content_digest mismatch")
    if isinstance(row.get("evidence_id"), str) and row.get("kind") in KINDS:
        try:
            if expected_evidence_id(row) != row["evidence_id"]:
                errors.append("evidence_id does not match row contents (forged or corrupted)")
        except Exception:
            errors.append("evidence_id recompute failed")
    return errors


def _replay_recomputed_diff(evidence: dict, excerpt: str, context: int,
                            base_checkout: Path | None, head_checkout: Path | None) -> list[str]:
    """Recompute the exact per-file diff from the object store and compare.

    A diff_hunk is mechanically strong evidence only if the repository
    reproduces it byte-for-byte. Self-signed diff text is rejected.
    """
    base_sha = str(evidence.get("base_sha") or "")
    head_sha = str(evidence.get("head_sha") or "")
    path = str(evidence.get("path") or "")
    last_error = ""
    for checkout in (head_checkout, base_checkout):
        if checkout is None or not (Path(checkout) / ".git").exists():
            continue
        try:
            recomputed = git(Path(checkout), *diff_git_argv(base_sha, head_sha, path, context))
        except GitError as exc:
            last_error = str(exc)
            continue
        # Exact compare (git() strips both sides identically at birth and
        # replay): padded re-mints ("honest" + "\n\n\n") are rejected.
        if recomputed == excerpt:
            anchor_errs = _check_diff_anchor(excerpt, int(evidence.get("line_start") or 0),
                                            int(evidence.get("line_end") or 0), path)
            return anchor_errs
        return [f"diff_hunk for {path!r} does not match the recomputed "
                f"{base_sha[:8]}..{head_sha[:8]} diff (forged or stale diff text)"]
    return [f"cannot recompute diff_hunk for {path!r}: no git object store in checkouts"
            + (f" ({last_error[:200]})" if last_error else "")]


def _check_diff_anchor(diff_text: str, line_start: int, line_end: int, path: str) -> list[str]:
    """The anchor range must lie inside the hunk's head-side line coverage."""
    import re

    covered: set[int] = set()
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff_text, re.M):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        covered.update(range(start, start + max(count, 0)))
    if not covered:
        return [f"diff_hunk for {path!r} has no parseable hunks"]
    if not set(range(line_start, line_end + 1)) <= covered:
        return [f"diff_hunk anchor {line_start}-{line_end} lies outside the hunk coverage of {path!r}"]
    return []


def checkout_holds_sha(checkout_dir: Path, sha: str) -> bool:
    checkout_dir = Path(checkout_dir)
    marker = checkout_dir / SHA_MARKER
    if marker.exists() and not (checkout_dir / ".git").exists():
        return marker.read_text(encoding="utf-8").strip() == sha
    try:
        return rev_parse(checkout_dir, "HEAD") == sha
    except GitError:
        return False


def replay_source_evidence(evidence: dict, *, base_checkout: Path | None = None,
                           head_checkout: Path | None = None,
                           changed_paths: set[str] | None = None) -> list[str]:
    """Mechanically replay one evidence item against exact revision bytes.

    Returns a list of discrepancies (empty = replays cleanly).
    """
    kind = evidence.get("kind")
    revision = evidence.get("revision")
    path = evidence.get("path", "")
    if kind == "source_excerpt":
        checkout = base_checkout if revision == "base" else head_checkout if revision == "head" else None
        if checkout is None:
            return [f"no {revision} checkout supplied for replay"]
        if not checkout_holds_sha(checkout, str(evidence.get("revision_sha") or "")):
            return [f"{revision} checkout does not hold revision_sha {str(evidence.get('revision_sha'))[:8]}"]
        try:
            target = safe_join(Path(checkout), path)
        except ValueError:
            return [f"evidence path escapes repo: {path!r}"]
        if not target.is_file():
            return [f"evidence file not found at {revision}: {path!r}"]
        try:
            # Same lossy decode as the read tools: byte-identical comparison
            # happens on the digest, so decoding must match exactly.
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return [f"evidence file unreadable at {revision}: {path!r}"]
        ls, le = evidence.get("line_start"), evidence.get("line_end")
        if not isinstance(ls, int) or not isinstance(le, int) or ls < 1 or le < ls or le > len(lines):
            return [f"evidence line range {ls}-{le} out of bounds ({len(lines)} lines)"]
        actual = "\n".join(lines[ls - 1:le])
        if sha256_text(actual) != evidence.get("content_digest"):
            return [f"excerpt does not match {revision} bytes at {path}:{ls}-{le} (base/head mismatch or stale excerpt)"]
        return []
    if kind == "file_state":
        fact = evidence.get("fact")
        checkout = base_checkout if revision == "base" else head_checkout if revision == "head" else None
        if checkout is None:
            return [f"no {revision} checkout supplied for replay"]
        if not checkout_holds_sha(checkout, str(evidence.get("revision_sha") or "")):
            return [f"{revision} checkout does not hold revision_sha {str(evidence.get('revision_sha'))[:8]}"]
        try:
            target = safe_join(Path(checkout), path)
        except ValueError:
            return [f"evidence path escapes repo: {path!r}"]
        if fact == "absent":
            return [] if not target.exists() else [f"{path!r} exists at {revision}, claimed absent"]
        if not target.is_file():
            return [f"evidence file not found at {revision}: {path!r}"]
        if fact == "empty":
            n = len(target.read_bytes().decode("utf-8", "replace").splitlines())
            return [] if n == 0 else [f"{path!r} has {n} lines at {revision}, claimed empty"]
        if fact == "binary":
            raw = target.read_bytes()[:1_048_576]
            return [] if b"\x00" in raw else [f"{path!r} has no NUL bytes at {revision}, claimed binary"]
        if fact == "present":
            return []
        return [f"unknown file fact: {fact!r}"]
    if kind == "diff_hunk":
        if changed_paths is None:
            return ["diff_hunk replay requires the changed path set (refusing vacuous pass)"]
        if path not in changed_paths:
            return [f"diff_hunk path {path!r} is not in the changed set"]
        excerpt = str(evidence.get("excerpt") or "")
        if not excerpt or excerpt.strip() in EMPTY_DIFF_PLACEHOLDERS:
            return ["diff_hunk requires the unified diff as excerpt"]
        args = evidence.get("tool_args")
        context = args.get("context_lines") if isinstance(args, dict) else None
        if type(context) is not int:
            return ["diff_hunk lacks creation parameters (tool_args.context_lines as int)"]
        return _replay_recomputed_diff(evidence, excerpt, context, base_checkout, head_checkout)
    # external_document / witness / analyzer_result: no byte replay here;
    # the trust validator checks their recorded artifacts instead.
    return []
