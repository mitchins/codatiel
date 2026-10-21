"""diff_window tool: exact diff-adjacent source with evidence."""

from __future__ import annotations

from pathlib import Path

from codatiel_i2.common.git import git
from codatiel_i2.core.calls import traced
from codatiel_i2.core.evidence import checkout_holds_sha, diff_git_argv, make_evidence

MAX_DIFF_BYTES = 200_000


@traced("diff_window")
def diff_window(repo: Path, base_sha: str, head_sha: str, path: str,
                context_lines: int = 40) -> dict:
    """Return unified diff for path plus evidence anchor on changed lines.

    The full per-file diff is stored (no truncation: the digest and ID bind
    the complete bytes). Diffs above MAX_DIFF_BYTES are refused.
    """
    for sha in (base_sha, head_sha):
        try:
            git(Path(repo), "cat-file", "-e", sha)
        except Exception:
            raise ValueError(f"diff endpoint not present in repo: {sha[:8]}")
    # Canonical argv shared with replay: creation and trust verification
    # MUST produce byte-identical output.
    text = git(Path(repo), *diff_git_argv(base_sha, head_sha, path, context_lines))
    # Anchor: first changed hunk line if present, else line 1 of file.
    line_start = line_end = 1
    for line in text.splitlines():
        if line.startswith("@@"):
            try:
                # @@ -a,b +c,d @@
                plus = line.split("+")[1].split(" ")[0]
                start = plus.split(",")[0]
                line_start = line_end = max(1, int(start))
                break
            except Exception:
                pass
    if len(text.encode("utf-8")) > MAX_DIFF_BYTES:
        raise ValueError(f"diff too large for evidence ({len(text)} chars): {path!r}")
    if not text.strip() or text.strip() == "(no diff)":
        raise ValueError(f"no diff for path: {path!r}")
    ev = make_evidence(kind="diff_hunk", tool="diff_window", base_sha=base_sha, head_sha=head_sha,
                       revision="diff",
                       path=path, line_start=line_start, line_end=line_end,
                       excerpt=text, tool_args={"context_lines": context_lines})
    return {"evidence": ev.to_dict(), "diff": text}
