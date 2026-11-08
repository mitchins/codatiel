"""CONTEXT-01: prepared change bundle + citable diff evidence.

Feature A (bundle): a deterministic, bounded, cacheable summary of a
base..head change placed in the review's first message. Neutral path
order, no ranking, no steering. Tests, generated, vendored, lockfile,
binary, snapshot and migration files are listed by name and size and
never quoted as diff text. Anything omitted by the size cap is listed
by name with its size and stays retrievable with the existing tools.

Feature B (citable lines): evidence IDs for real changed lines. Added
runs bind to head, removed runs to base, with true line numbers derived
from unified-diff hunk headers. Every excerpt is verified against the
actual checkout bytes at mint time; a mismatch skips the run
(fail-closed). Envelopes are standard evidence.v2, so M7.1 anchoring,
source verification and run-scoped citation apply unchanged.

Deterministic: identical inputs produce byte-identical bundles. No LLM,
no network, no timestamps in output.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from codatiel_i2.common.git import git
from codatiel_i2.core.evidence import diff_git_argv, make_evidence

BUNDLE_SCHEMA = "codatiel_i2.change_bundle.v1"
BUNDLE_MAX_CHARS = 100_000
PER_FILE_HUNK_CHARS = 20_000
MAX_LINE_RUNS_PER_FILE = 40
HUNK_CONTEXT_LINES = 3

_LIST_ONLY_CATEGORIES = frozenset({
    "test", "generated", "snapshot", "vendored", "lockfile", "binary",
    "migration",
})

_ENV_KEY_RE = re.compile(
    r"\b(?:os\.environ(?:\.get)?\(|os\.getenv\()[\"']([A-Z][A-Z0-9_]{2,})[\"']"
    r"|\bos\.environ\[[\"']([A-Z][A-Z0-9_]{2,})[\"']\]")
_ROUTE_RE = re.compile(
    r"(@app\.(?:get|post|put|delete|patch|route)\b|@router\.(?:get|post|put)"
    r"|@router\.(?:delete|patch|route)\b|@blueprint\.|add_url_rule\("
    r"|@(?:app|router)\.route\(|path\(\s*[\"']|re_path\(|\.route\()")
_COMPOSE_KEY_RE = re.compile(r"^\s*-?\s*([A-Z][A-Z0-9_]{2,})\s*[=:]")
_CONFIG_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\s*[:=]")


@dataclass
class LineRun:
    revision: str          # "head" for added, "base" for removed
    path: str
    line_start: int
    line_end: int
    lines: list[str] = field(default_factory=list)


@dataclass
class Bundle:
    text: str
    # Only evidence rendered into the text (i.e. actually returned to the
    # model in this run) — this is what the citation ledger may merge.
    evidence: list[dict] = field(default_factory=list)
    evidence_total: int = 0
    evidence_omitted: int = 0
    omitted_files: list[dict] = field(default_factory=list)
    omitted_runs: int = 0
    truncated: bool = False


def _changed_lines(diff_text: str) -> dict[str, list[tuple[str, int, str]]]:
    """Parse one unified patch into per-file (tag, lineno, text) rows.

    Tags: "+", "-", " ". Line numbers are true head-side (+ and space)
    and base-side (-) numbers computed from hunk headers.
    """
    out: dict[str, list[tuple[str, int, str]]] = {}
    path = ""
    old = new = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            out.setdefault(path, [])
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                old, new = int(m.group(1)), int(m.group(2))
            continue
        if not path:
            continue
        if line.startswith("+"):
            out[path].append(("+", new, line[1:]))
            new += 1
        elif line.startswith("-"):
            out[path].append(("-", old, line[1:]))
            old += 1
        elif line.startswith(" "):
            out[path].append((" ", new, line[1:]))
            old += 1
            new += 1
    return out


def _line_runs(rows: list[tuple[str, int, str]]) -> list[LineRun]:
    """Collapse changed rows into contiguous runs (added -> head,
    removed -> base). Context rows break runs but are never minted."""
    runs: list[LineRun] = []
    current: LineRun | None = None
    for tag, lineno, text in rows:
        if tag == "+":
            if current is not None and current.revision == "head" \
                    and current.line_end + 1 == lineno:
                current.line_end = lineno
                current.lines.append(text)
            else:
                current = LineRun("head", "", lineno, lineno, [text])
                runs.append(current)
        elif tag == "-":
            if current is not None and current.revision == "base" \
                    and current.line_end + 1 == lineno:
                current.line_end = lineno
                current.lines.append(text)
            else:
                current = LineRun("base", "", lineno, lineno, [text])
                runs.append(current)
        else:
            current = None
    for run in runs:
        run.path = ""  # set by caller per file
    return runs


def _checkout_lines(checkout: Path, rel: str, start: int,
                    end: int) -> list[str] | None:
    try:
        text = (Path(checkout) / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    lines = text.split("\n")
    if start < 1 or end > len(lines):
        return None
    return lines[start - 1:end]


def bundle_line_evidence(*, diff_repo: Path, base_sha: str, head_sha: str,
                         changes: list[dict],
                         base_dir: Path, head_dir: Path,
                         max_runs_per_file: int = MAX_LINE_RUNS_PER_FILE,
                         ) -> tuple[list[dict], int]:
    """Mint evidence for changed lines, verified against checkouts.

    Returns (evidence_dicts, skipped_runs). Evidence IDs are standard
    evidence.v2 digests; each excerpt's bytes were read from the pinned
    checkout at mint time, so M7.1 source verification replays them.
    """
    evidence: list[dict] = []
    skipped = 0
    for row in sorted(changes, key=lambda c: c.get("path", "")):
        path = row.get("path", "")
        if not path or row.get("is_binary"):
            continue
        try:
            diff_text = git(diff_repo, *diff_git_argv(
                base_sha, head_sha, path, HUNK_CONTEXT_LINES))
        except Exception:
            skipped += 1
            continue
        rows = _changed_lines(diff_text).get(path)
        if not rows:
            continue
        minted = 0
        for run in _line_runs(rows):
            if minted >= max_runs_per_file:
                skipped += 1
                continue
            if not any(run.lines):
                # A run of blank lines carries nothing citable.
                skipped += 1
                continue
            checkout = head_dir if run.revision == "head" else base_dir
            actual = _checkout_lines(checkout, path, run.line_start,
                                     run.line_end)
            if actual is None or actual != run.lines:
                # Diff text and checkout disagree: never mint a line
                # evidence that would not byte-replay.
                skipped += 1
                continue
            ev = make_evidence(
                kind="source_excerpt", tool="change_bundle",
                base_sha=base_sha, head_sha=head_sha,
                revision=run.revision, path=path,
                line_start=run.line_start, line_end=run.line_end,
                excerpt="\n".join(run.lines),
                tool_args={"context_lines": HUNK_CONTEXT_LINES,
                           "origin": "change_bundle"})
            evidence.append(ev.to_dict())
            minted += 1
    return evidence, skipped


def _python_symbols(head_dir: Path, path: str,
                    base_dir: Path) -> tuple[list[str], list[str]]:
    """Top-level defs/classes of a changed .py file (head), marking the
    ones absent from the base revision. Parse failures degrade to []."""
    def names(root: Path) -> list[str]:
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            return []
        out = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "def"
                out.append(f"{kind} {node.name}")
        return out

    head_names = names(head_dir)
    if not head_names:
        return [], []
    base_names = set(names(base_dir))
    return head_names, sorted(n for n in head_names if n not in base_names)


def _scan_keys(rows: list[tuple[str, int, str]],
               path: str) -> tuple[set[str], set[str]]:
    """Env/config keys and routes mentioned on changed lines of one file.
    Conservative regex inventory, deterministic order at the caller."""
    env: set[str] = set()
    routes: set[str] = set()
    lower = path.lower()
    is_composeish = lower.endswith(("compose.yml", "compose.yaml", "dockerfile")) \
        or lower.endswith(".env") or "/.env." in lower
    for _tag, _lineno, text in rows:
        for m in _ENV_KEY_RE.finditer(text):
            env.add(m.group(1) or m.group(2))
        if _ROUTE_RE.search(text):
            routes.add(_ROUTE_RE.search(text).group(0).strip())
        if is_composeish:
            m = _COMPOSE_KEY_RE.match(text)
            if m:
                env.add(m.group(1))
    return env, routes


def build_change_bundle(*, diff_repo: Path, base_sha: str, head_sha: str,
                        changes: list[dict], classification: dict,
                        base_dir: Path, head_dir: Path,
                        max_chars: int = BUNDLE_MAX_CHARS,
                        with_evidence: bool = False,
                        ) -> Bundle:
    """Deterministic change bundle (Feature A) plus optional citable
    line evidence (Feature B). Path order everywhere; nothing ranked."""
    by_path = {c.get("path", ""): c for c in classification.get("files", [])}
    evidence: list[dict] = []
    evidence_total = 0
    omitted_runs = 0
    if with_evidence:
        evidence, omitted_runs = bundle_line_evidence(
            diff_repo=diff_repo, base_sha=base_sha, head_sha=head_sha,
            changes=changes, base_dir=base_dir, head_dir=head_dir)
        evidence_total = len(evidence)

    list_only: list[str] = []
    quoted: list[dict] = []
    for row in sorted(changes, key=lambda c: c.get("path", "")):
        path = row.get("path", "")
        cat = by_path.get(path, {}).get("category", "unknown")
        entry = {"path": path, "category": cat,
                 "additions": row.get("additions", 0),
                 "deletions": row.get("deletions", 0)}
        if cat in _LIST_ONLY_CATEGORIES:
            list_only.append(
                f"- {path} [{cat}] +{entry['additions']}/-{entry['deletions']}"
                " (diff text not shown)")
        else:
            quoted.append(entry)

    env_keys: set[str] = set()
    route_hits: set[str] = set()
    symbols: list[str] = []
    new_symbols: list[str] = []
    for entry in quoted:
        path = entry["path"]
        if path.endswith(".py"):
            head_names, fresh = _python_symbols(head_dir, path, base_dir)
            if head_names:
                symbols.extend(f"{path}: {n}" for n in head_names)
                new_symbols.extend(f"{path}: {n}" for n in fresh)
        try:
            diff_text = git(diff_repo, *diff_git_argv(
                base_sha, head_sha, path, HUNK_CONTEXT_LINES))
        except Exception:
            diff_text = ""
        rows = _changed_lines(diff_text).get(path, [])
        env, routes = _scan_keys(rows, path)
        env_keys.update(env)
        route_hits.update(routes)

    lines: list[str] = [
        f"CHANGE BUNDLE {BUNDLE_SCHEMA}",
        f"base {base_sha}", f"head {head_sha}",
        f"{len(changes)} changed files.",
        "",
        "## Changed files (path order)",
    ]
    lines.extend(
        f"- {e['path']} [{e['category']}] +{e['additions']}/-{e['deletions']}"
        for e in quoted)
    if list_only:
        lines.append("")
        lines.append("## Tests, generated and non-source changes (listed, "
                     "not quoted)")
        lines.extend(list_only)
    if symbols:
        lines.append("")
        lines.append("## Top-level definitions in changed python files")
        lines.extend(f"- {s}" for s in symbols[:200])
    if new_symbols:
        lines.append("")
        lines.append("## New top-level definitions (absent in base)")
        lines.extend(f"- {s}" for s in new_symbols[:100])
    if route_hits:
        lines.append("")
        lines.append("## Route decorators on changed lines")
        lines.extend(f"- {r}" for r in sorted(route_hits)[:60])
    if env_keys:
        lines.append("")
        lines.append("## Configuration keys on changed lines")
        lines.extend(f"- {k}" for k in sorted(env_keys)[:60])

    budget = max_chars - sum(len(x) + 1 for x in lines)
    if with_evidence:
        # Reserve room for the citation section: hunks may use at most
        # 65% of the cap when citable lines are included.
        budget = min(budget, int(max_chars * 0.65)
                     - sum(len(x) + 1 for x in lines))
    hunk_lines: list[str] = []
    omitted: list[dict] = []
    truncated = budget <= 0
    for entry in quoted:
        if truncated:
            omitted.append({"path": entry["path"], "chars": None,
                            "reason": "bundle cap"})
            continue
        path = entry["path"]
        try:
            diff_text = git(diff_repo, *diff_git_argv(
                base_sha, head_sha, path, HUNK_CONTEXT_LINES))
        except Exception as exc:
            hunk_lines.append(f"### {path}")
            hunk_lines.append(f"(diff unavailable: {type(exc).__name__})")
            continue
        if not diff_text.strip() or diff_text.strip() == "(no diff)":
            continue
        block = f"### {path}\n{diff_text.rstrip()}"
        if len(block) > PER_FILE_HUNK_CHARS:
            block = block[:PER_FILE_HUNK_CHARS]
            cut = block.rfind("\n@@")
            if cut > 0:
                block = block[:cut]
            block += (f"\n... [omitted: rest of {path} diff "
                      f"(retrievable via diff_window)]")
            omitted.append({"path": path, "chars": PER_FILE_HUNK_CHARS,
                            "reason": "per-file cap"})
        if len(block) + 1 > budget:
            omitted.append({"path": path, "chars": len(block),
                            "reason": "bundle cap"})
            truncated = True
            continue
        hunk_lines.append(block)
        budget -= len(block) + 1
    if hunk_lines:
        lines.append("")
        lines.append("## Diff hunks (-U3, path order)")
        lines.extend(hunk_lines)
    if omitted or truncated:
        lines.append("")
        lines.append("## Omissions (complete list; retrievable with the "
                     "source tools)")
        for o in omitted:
            size = "unknown" if o["chars"] is None else f"{o['chars']} chars"
            lines.append(f"- {o['path']} ({size}; {o['reason']})")
        if omitted_runs:
            lines.append(f"- line-evidence runs beyond the per-file cap: "
                         f"{omitted_runs}")
    if with_evidence:
        # One line per citable run: ID, path, true line range, revision.
        # No excerpt text — the bytes are already in the diff hunks
        # above; duplicating source here would double the payload (the
        # PAYLOAD-01 lesson). Evidence not rendered is not returned to
        # the model and never enters the citation ledger.
        lines.append("")
        lines.append("## Citable changed lines (evidence IDs bind to the "
                     "named revision; added lines are head, removed lines "
                     "are base)")
        used = sum(len(x) + 1 for x in lines)
        rendered = 0
        for ev in evidence:
            entry = (f"[{ev['evidence_id']}] {ev['path']}:"
                     f"{ev['line_start']}-{ev['line_end']} ({ev['revision']})")
            if used + len(entry) + 1 > max_chars:
                break
            lines.append(entry)
            used += len(entry) + 1
            rendered += 1
        evidence = evidence[:rendered]
        if rendered < evidence_total:
            lines.append(f"- ... {evidence_total - rendered} "
                         f"further changed-line runs beyond the bundle cap "
                         f"(retrievable via read_file at the named "
                         f"revision)")
    text = "\n".join(lines)
    return Bundle(text=text, evidence=evidence,
                  evidence_total=evidence_total,
                  evidence_omitted=evidence_total - len(evidence),
                  omitted_files=omitted, omitted_runs=omitted_runs,
                  truncated=truncated or bool(omitted))
