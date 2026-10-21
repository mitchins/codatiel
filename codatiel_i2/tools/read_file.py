"""read_file tool with evidence envelope."""

from __future__ import annotations

from pathlib import Path

from codatiel_i2.common.hashing import sha256_bytes, sha256_text
from codatiel_i2.common.paths import safe_join
from codatiel_i2.core.calls import traced
from codatiel_i2.core.evidence import checkout_holds_sha, make_evidence

MAX_BYTES = 50_000


@traced("read_file")
def read_file(repo_root: Path, path: str, *, base_sha: str, head_sha: str,
              revision: str = "head",
              start_line: int = 1, end_line: int | None = None) -> dict:
    """Read exact source lines from a materialised checkout.

    `repo_root` must be the checkout holding `revision` (base_dir or
    head_dir); the revision is recorded in the evidence envelope so base
    and head reads are always distinguishable.
    """
    if revision not in ("base", "head"):
        raise ValueError("read_file revision must be 'base' or 'head'")
    root = Path(repo_root).resolve()
    expected = base_sha if revision == "base" else head_sha
    if not checkout_holds_sha(root, expected):
        raise ValueError(f"checkout does not hold claimed {revision} SHA {expected[:8]}")
    target = safe_join(root, path)
    if not target.exists():
        raise FileNotFoundError(f"file not found: {path!r}")
    if not target.is_file():
        raise ValueError(f"not a file: {path!r}")
    raw = target.read_bytes()
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError(f"file too large for read_file ({len(raw)} bytes): {path!r}")
    # Single binary rule everywhere: NUL within the first MiB means binary.
    if b"\x00" in raw[:1_048_576]:
        ev = make_evidence(kind="file_state", tool="read_file", base_sha=base_sha,
                           head_sha=head_sha, revision=revision, path=path,
                           line_start=1, line_end=1, fact="binary")
        return {"evidence": ev.to_dict(), "total_lines": 0, "binary": True,
                "truncated": False, "sha256": sha256_bytes(raw)}
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        ev = make_evidence(kind="file_state", tool="read_file", base_sha=base_sha,
                           head_sha=head_sha, revision=revision, path=path,
                           line_start=1, line_end=1, fact="empty")
        return {"evidence": ev.to_dict(), "total_lines": 0,
                "truncated": False, "sha256": sha256_text(text)}
    if start_line < 1:
        raise ValueError("start_line must be >= 1")
    end = end_line or total
    if end < start_line:
        raise ValueError("end_line < start_line")
    if start_line > total and total > 0:
        raise ValueError(f"start_line {start_line} beyond EOF ({total} lines)")
    requested = lines[start_line - 1:min(end, total)]
    # Byte-budget truncation on COMPLETE line boundaries: the envelope's
    # line_end always names the last fully-included line, so replay hashes
    # exactly these bytes. Never emit a mid-line prefix as a full range.
    kept: list[str] = []
    used = 0
    for ln in requested:
        cost = len(ln.encode("utf-8")) + 1  # + newline join
        if kept and used + cost > MAX_BYTES:
            break
        kept.append(ln)
        used += cost
    if not kept:
        # Single first line exceeds the budget: take it whole (budget is
        # advisory; integrity beats size) and flag truncation.
        kept = requested[:1]
        used = len(kept[0].encode("utf-8")) + 1
    excerpt = "\n".join(kept)
    actual_end = start_line + len(kept) - 1
    truncated = actual_end < min(end, total)
    ev = make_evidence(kind="source_excerpt", tool="read_file", base_sha=base_sha,
                       head_sha=head_sha, revision=revision,
                       path=path, line_start=start_line,
                       line_end=actual_end,
                       excerpt=excerpt)
    return {"evidence": ev.to_dict(), "total_lines": total,
            "truncated": truncated,
            "sha256": sha256_text(text)}
