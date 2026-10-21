"""Frozen evidence-validation contract tests (M7.1 §9.1, shareable part).

Synthetic repositories and frozen tool transcripts only: no live models, no
network, no credentials, no private fixtures.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from codatiel_i2.core import diff as diff_mod
from codatiel_i2.core import workspace as ws_mod
from codatiel_i2.core.evidence import expected_evidence_id, make_evidence
from codatiel_i2.tools import diff_window as diff_tool
from codatiel_i2.tools import read_file as read_tool
from codatiel_i2.verify import evidence_validation as V


BASE_FILES = {
    "app.py": "".join(f"line {n}\n" for n in range(1, 31)),
    "other.py": "VALUE = 1\n",
    "notes.md": "# notes\n\nBase note.\n",
}
HEAD_FILES = {
    "app.py": "".join(
        ("changed five\n" if n == 5 else "changed twenty-five\n" if n == 25 else f"line {n}\n")
        for n in range(1, 31)),
    "other.py": "VALUE = 1\n",
    "notes.md": "# notes\n\nHead note.\n",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                         text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture()
def pair(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "evidence@test")
    _git(root, "config", "user.name", "evidence")
    for name, text in BASE_FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    for name, text in HEAD_FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "commit", "-qam", "head")
    head = _git(root, "rev-parse", "HEAD")
    work = ws_mod.prepare_local_workspace(root, base, head, tmp_path / "work")
    assert work.status == "ready"
    changes = diff_mod.changed_files(root, base, head)
    return {"repo": root, "base": base, "head": head, "work": work,
            "changed": {c["path"] for c in changes}}


def _read(work, path, revision, start=1, end=1):
    checkout = work.base_dir if revision == "base" else work.head_dir
    pair_shas = {"base_sha": work.base_sha, "head_sha": work.head_sha}
    return read_tool.read_file(Path(checkout), path, revision=revision,
                               start_line=start, end_line=end, **pair_shas)["evidence"]


def _diff(pair, path, context_lines=0):
    return diff_tool.diff_window(pair["repo"], pair["base"], pair["head"], path,
                                 context_lines=context_lines)["evidence"]


def _finding(citations, path="app.py", revision="head", start=1, end=1, detector="contracts"):
    return {"detector": detector, "path": path, "revision": revision,
            "line_start": start, "line_end": end, "claim": "test claim",
            "evidence_ids": citations}


def _validate(pair, finding, ledger):
    return V.validate_model_finding(
        finding, ledger, base_sha=pair["base"], head_sha=pair["head"],
        base_checkout=Path(pair["work"].base_dir), head_checkout=Path(pair["work"].head_dir),
        changed_paths=set(pair["changed"]))


def test_same_file_citation_is_attested(pair):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    ledger = {ev["evidence_id"]: ev}
    result = _validate(pair, _finding([ev["evidence_id"]], start=5, end=5), ledger)
    assert result.state == V.ATTESTED
    assert result.code is None


def test_cross_file_citation_is_attested(pair):
    anchor = _read(pair["work"], "app.py", "head", 5, 5)
    support = _read(pair["work"], "other.py", "head", 1, 1)
    ledger = {anchor["evidence_id"]: anchor, support["evidence_id"]: support}
    result = _validate(pair, _finding([anchor["evidence_id"], support["evidence_id"]], start=5, end=5), ledger)
    assert result.state == V.ATTESTED


def test_base_head_and_diff_citations_together_are_attested(pair):
    base = _read(pair["work"], "app.py", "base", 5, 5)
    head = _read(pair["work"], "app.py", "head", 5, 5)
    diff = _diff(pair, "app.py")
    ledger = {item["evidence_id"]: item for item in (base, head, diff)}
    # The recorded diff range is the first hunk only; the finding anchor may
    # be in the second hunk. Both must be accepted without substitution.
    second_hunk_line = 25
    result = _validate(pair, _finding([base["evidence_id"], head["evidence_id"], diff["evidence_id"]],
                                      start=second_hunk_line, end=second_hunk_line), ledger)
    assert result.state == V.ATTESTED


def test_anchor_outside_modified_lines_is_attested(pair):
    ev = _read(pair["work"], "app.py", "head", 10, 10)
    ledger = {ev["evidence_id"]: ev}
    result = _validate(pair, _finding([ev["evidence_id"]], start=10, end=10), ledger)
    assert result.state == V.ATTESTED


def test_historical_schema_is_supported_and_future_schema_is_not(pair):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    assert ev["schema_version"] == "codatiel_i2.evidence.v2"
    ledger = {ev["evidence_id"]: ev}
    assert _validate(pair, _finding([ev["evidence_id"]], start=5, end=5), ledger).state == V.ATTESTED
    future = dict(ev, schema_version="codatiel_i2.evidence.v99")
    ledger = {ev["evidence_id"]: future}
    result = _validate(pair, _finding([ev["evidence_id"]], start=5, end=5), ledger)
    assert result.state == V.REJECTED
    assert result.code == V.EVIDENCE_SCHEMA_UNSUPPORTED


def test_missing_malformed_and_duplicate_citations(pair):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    ledger = {ev["evidence_id"]: ev}
    missing = _validate(pair, _finding([], start=5, end=5), ledger)
    assert (missing.state, missing.code) == (V.REJECTED, V.EVIDENCE_CITATIONS_MISSING)
    for bad in ("not-a-list", [""], [" ev_" + "0" * 16], ["ev_SHORT"], [ev["evidence_id"]] * 2):
        result = _validate(pair, _finding(bad, start=5, end=5), ledger)
        assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_CITATION_MALFORMED), bad


def test_nine_valid_citations_attest(pair):
    records = [_read(pair["work"], "app.py", "head", n, n) for n in range(1, 10)]
    ledger = {item["evidence_id"]: item for item in records}
    result = _validate(pair, _finding([item["evidence_id"] for item in records], start=1, end=1), ledger)
    assert result.state == V.ATTESTED
    assert len(result.citation_checks) == 9


def test_eight_valid_plus_fabricated_is_rejected_without_substitution(pair):
    records = [_read(pair["work"], "app.py", "head", n, n) for n in range(1, 9)]
    ledger = {item["evidence_id"]: item for item in records}
    fabricated = "ev_" + "f" * 16
    submitted = [item["evidence_id"] for item in records] + [fabricated]
    result = _validate(pair, _finding(submitted, start=1, end=1), ledger)
    assert result.state == V.REJECTED
    assert result.code == V.EVIDENCE_NOT_RETURNED
    assert len(result.citation_checks) == 9
    assert result.citation_checks[-1].status == "failed"


def test_wrong_revision_citation_is_rejected_with_all_citations_kept(pair):
    records = [_read(pair["work"], "app.py", "head", n, n) for n in range(1, 9)]
    ledger = {item["evidence_id"]: item for item in records}
    wrong = make_evidence(kind="source_excerpt", tool="read_file", base_sha=pair["base"],
                          head_sha=pair["head"], revision="head", path="app.py",
                          line_start=9, line_end=9, revision_sha=pair["base"],
                          excerpt="line 9").to_dict()
    assert wrong["evidence_id"] not in ledger
    ledger[wrong["evidence_id"]] = wrong
    submitted = [item["evidence_id"] for item in records] + [wrong["evidence_id"]]
    finding = _finding(submitted, start=1, end=1)
    before = json.loads(json.dumps(finding))
    result = _validate(pair, finding, ledger)
    assert result.state == V.REJECTED
    assert result.code == V.EVIDENCE_REVISION_MISMATCH
    assert finding == before
    assert len(finding["evidence_ids"]) == 9


def test_no_citations_but_valid_anchor_is_rejected(pair):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    result = _validate(pair, _finding([], start=5, end=5), {ev["evidence_id"]: ev})
    assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_CITATIONS_MISSING)


def test_run_membership_is_per_execution(pair):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    assert _validate(pair, _finding([ev["evidence_id"]], start=5, end=5),
                     {ev["evidence_id"]: ev}).state == V.ATTESTED
    assert _validate(pair, _finding([ev["evidence_id"]], start=5, end=5), {}).code == V.EVIDENCE_NOT_RETURNED


def test_genuine_evidence_behind_false_or_immaterial_claims_still_attests(pair):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    ledger = {ev["evidence_id"]: ev}
    false_claim = _finding([ev["evidence_id"]], start=5, end=5)
    false_claim["claim"] = "line 5 still says the base value although the bytes say it changed"
    immaterial = _finding([ev["evidence_id"]], start=5, end=5)
    immaterial["claim"] = "line 5 ends with a newline"
    assert _validate(pair, false_claim, ledger).state == V.ATTESTED
    assert _validate(pair, immaterial, ledger).state == V.ATTESTED


def test_invalid_anchors(pair):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    ledger = {ev["evidence_id"]: ev}
    outside = _validate(pair, _finding([ev["evidence_id"]], path="missing.py"), ledger)
    assert (outside.state, outside.code) == (V.REJECTED, V.ANCHOR_PATH_NOT_CHANGED)
    bad_range = _validate(pair, _finding([ev["evidence_id"]], start=9999, end=9999), ledger)
    assert (bad_range.state, bad_range.code) == (V.REJECTED, V.ANCHOR_RANGE_INVALID)


def test_form_a_mutations_fail_integrity(pair):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    for field, value in (("excerpt", "forged bytes"), ("revision_sha", pair["base"]),
                         ("path", "other.py"), ("line_start", 6)):
        mutated = dict(ev, **{field: value})
        assert expected_evidence_id(mutated) != ev["evidence_id"]
        # The tampered record is what the ledger returns for the submitted
        # original ID, so the integrity layer must reject it.
        result = _validate(pair, _finding([ev["evidence_id"]], start=5, end=5),
                           {ev["evidence_id"]: mutated})
        assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_ID_INTEGRITY), field
    forged_id = "ev_" + "1" * 16
    assert forged_id != ev["evidence_id"]
    result = _validate(pair, _finding([forged_id], start=5, end=5), {ev["evidence_id"]: ev})
    assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_NOT_RETURNED)


def _archive_checkout(repo: Path, sha: str, dest: Path) -> None:
    """Materialize an archive-style checkout: tracked files only, no .git,
    plus the SHA marker (mirrors the production archive fallback)."""
    from codatiel_i2.core.workspace import SHA_MARKER

    proc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha],
                          capture_output=True, check=True, timeout=60)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r") as tf:
            tf.extractall(path=str(dest), filter="fully_trusted")
    except tarfile.ReadError:
        # `git archive` of an empty tree emits only a pax global header,
        # which is not a parseable tar: confirm the tree is really empty.
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha],
            capture_output=True, check=True, timeout=60).stdout.strip()
        assert not tracked, f"unparseable non-empty archive for {sha[:8]}"
    # An empty tree archives to header-only bytes: the checkout is just the marker.
    (dest / SHA_MARKER).write_text(sha + "\n", encoding="utf-8")


@pytest.fixture()
def archive_pair(pair, tmp_path: Path) -> dict:
    base_dir = tmp_path / "archive-base"
    head_dir = tmp_path / "archive-head"
    _archive_checkout(pair["repo"], pair["base"], base_dir)
    _archive_checkout(pair["repo"], pair["head"], head_dir)
    return {"base": pair["base"], "head": pair["head"],
            "base_dir": base_dir, "head_dir": head_dir,
            "changed": set(pair["changed"])}


def _validate_archive(archive, finding, ledger):
    return V.validate_model_finding(
        finding, ledger, base_sha=archive["base"], head_sha=archive["head"],
        base_checkout=archive["base_dir"], head_checkout=archive["head_dir"],
        changed_paths=set(archive["changed"]))


def _archived_diff_evidence(archive, path, context_lines=0):
    """Build a v2 diff record from the archived trees exactly as the tool would."""
    import subprocess as sp

    base = archive["base_dir"] / path
    head = archive["head_dir"] / path
    argv = ["git", "-c", "core.quotePath=false", "-c", "diff.noprefix=false",
            "-c", "diff.mnemonicPrefix=false", "-c", "core.pager=cat",
            "diff", "--no-index", "--no-color", "--no-ext-diff", "--no-textconv",
            f"-U{context_lines}", "--",
            str(base) if base.exists() else "/dev/null",
            str(head) if head.exists() else "/dev/null"]
    env = dict(os.environ)
    env.pop("GIT_EXTERNAL_DIFF", None)
    env["GIT_PAGER"] = "cat"
    proc = sp.run(argv, capture_output=True, timeout=60, env=env)
    assert proc.returncode in (0, 1), proc.stderr.decode()[:200]
    text = proc.stdout.decode("utf-8", errors="replace")
    # Rewrite temp paths to repo-relative a/ b/ form, as the tool emits.
    fixed = []
    for line in text.splitlines():
        if line.startswith("diff --git"):
            fixed.append(f"diff --git a/{path} b/{path}")
        elif line.startswith("--- "):
            fixed.append("--- " + ("/dev/null" if not base.exists() else f"a/{path}"))
        elif line.startswith("+++ "):
            fixed.append("+++" + " " + ("/dev/null" if not head.exists() else f"b/{path}"))
        else:
            fixed.append(line)
    excerpt = "\n".join(fixed)
    line_start = line_end = 1
    for line in excerpt.splitlines():
        if line.startswith("@@"):
            plus = line.split("+")[1].split(" ")[0]
            line_start = line_end = max(1, int(plus.split(",")[0]))
            break
    return make_evidence(kind="diff_hunk", tool="diff_window", base_sha=archive["base"],
                         head_sha=archive["head"], revision="diff", path=path,
                         line_start=line_start, line_end=line_end,
                         excerpt=excerpt, tool_args={"context_lines": context_lines}).to_dict()


def test_archive_style_checkouts_attest_through_tree_replay(archive_pair):
    ev = _archived_diff_evidence(archive_pair, "app.py")
    ledger = {ev["evidence_id"]: ev}
    result = _validate_archive(archive_pair, _finding([ev["evidence_id"]], start=25, end=25), ledger)
    assert (result.state, result.code) == (V.ATTESTED, None)


def test_tree_replay_rejects_inflated_forgery(archive_pair):
    """An internally consistent forgery (recomputed ID over a doctored full-
    rewrite excerpt) must fail at replay, not integrity."""
    import subprocess as sp

    head_text = (archive_pair["head_dir"] / "app.py").read_text(encoding="utf-8")
    forged_lines = ["diff --git a/app.py b/app.py",
                    "index 0000000..1111111 100644",
                    "--- a/app.py", "+++ b/app.py",
                    f"@@ -1,30 +1,30 @@"]
    forged_lines += ["-" + line for line in head_text.splitlines()]
    forged_lines += ["+" + line for line in head_text.splitlines()]
    forged = make_evidence(kind="diff_hunk", tool="diff_window", base_sha=archive_pair["base"],
                           head_sha=archive_pair["head"], revision="diff", path="app.py",
                           line_start=1, line_end=30,
                           excerpt="\n".join(forged_lines),
                           tool_args={"context_lines": 0}).to_dict()
    assert expected_evidence_id(forged) == forged["evidence_id"]
    ledger = {forged["evidence_id"]: forged}
    result = _validate_archive(archive_pair, _finding([forged["evidence_id"]], start=1, end=1), ledger)
    assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_REPLAY_MISMATCH)
    assert result.citation_checks[0].status == "failed"


def test_tree_replay_rejects_unchanged_path_self_rewrite(archive_pair):
    head_text = (archive_pair["head_dir"] / "other.py").read_text(encoding="utf-8")
    lines = head_text.splitlines()
    forged = make_evidence(kind="diff_hunk", tool="diff_window", base_sha=archive_pair["base"],
                           head_sha=archive_pair["head"], revision="diff", path="other.py",
                           line_start=1, line_end=len(lines),
                           excerpt="\n".join(
                               ["diff --git a/other.py b/other.py",
                                "index 0000000..2222222 100644",
                                "--- a/other.py", "+++ b/other.py",
                                f"@@ -1,{len(lines)} +1,{len(lines)} @@"]
                               + [" " + line for line in lines]),
                           tool_args={"context_lines": 0}).to_dict()
    result = _validate_archive(archive_pair, _finding([forged["evidence_id"]], start=1, end=1,
                                                      path="other.py"),
                               {forged["evidence_id"]: forged})
    assert result.state == V.REJECTED
    assert result.code == V.EVIDENCE_REPLAY_MISMATCH


def test_deleted_file_diff_attests_in_tree_replay(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "evidence@test")
    _git(root, "config", "user.name", "evidence")
    (root / "gone.py").write_text("old\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "gone.py").unlink()
    _git(root, "commit", "-qam", "head")
    head = _git(root, "rev-parse", "HEAD")
    base_dir, head_dir = tmp_path / "b", tmp_path / "h"
    _archive_checkout(root, base, base_dir)
    _archive_checkout(root, head, head_dir)
    archive = {"base": base, "head": head, "base_dir": base_dir, "head_dir": head_dir,
               "changed": {"gone.py"}}
    ev = _archived_diff_evidence(archive, "gone.py")
    assert "deleted file mode" in ev["excerpt"]
    result = _validate_archive(
        archive, {"detector": "contracts", "path": "gone.py", "revision": "base",
                  "line_start": 1, "line_end": 1, "claim": "x",
                  "evidence_ids": [ev["evidence_id"]]},
        {ev["evidence_id"]: ev})
    # The diff itself is genuine; the anchor replays at BASE where the file
    # still exists. A HEAD anchor for a deleted file would be rejected.
    assert result.state == V.ATTESTED
    assert result.citation_checks[0].status == "passed"


def test_missing_snapshot_is_blocked(pair, tmp_path: Path):
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    result = V.validate_model_finding(
        _finding([ev["evidence_id"]], start=5, end=5), {ev["evidence_id"]: ev},
        base_sha=pair["base"], head_sha=pair["head"],
        base_checkout=pair["work"].base_dir,
        head_checkout=tmp_path / "no-such-checkout",
        changed_paths=set(pair["changed"]))
    assert (result.state, result.code) == (V.BLOCKED, V.SOURCE_SNAPSHOT_UNAVAILABLE)


def test_citation_failure_outranks_anchor_failure(pair):
    wrong = make_evidence(kind="source_excerpt", tool="read_file", base_sha=pair["base"],
                          head_sha=pair["head"], revision="head", path="app.py",
                          line_start=5, line_end=5, revision_sha=pair["base"],
                          excerpt="changed five").to_dict()
    result = _validate(pair, _finding([wrong["evidence_id"]], path="missing.py"),
                       {wrong["evidence_id"]: wrong})
    assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_REVISION_MISMATCH)


def test_external_evidence_attests_without_byte_replay(pair):
    ev = make_evidence(kind="external_document", tool="lookup_docs", base_sha=pair["base"],
                       head_sha=pair["head"], revision="external",
                       revision_sha="vendor-docs-v3",
                       path="vendor-cache://docs/api.md", line_start=1, line_end=10,
                       excerpt="Rate limits are per key.").to_dict()
    result = _validate(pair, _finding([ev["evidence_id"]], start=5, end=5),
                       {ev["evidence_id"]: ev})
    assert (result.state, result.code) == (V.ATTESTED, None)


def test_form_b_forgery_reaches_the_targeted_layer(pair):
    work = pair["work"]
    # Absent ledger: lookup fails even though the forgery is internally consistent.
    absent = make_evidence(kind="source_excerpt", tool="read_file", base_sha=pair["base"],
                           head_sha=pair["head"], revision="head", path="app.py",
                           line_start=5, line_end=5, excerpt="line 5").to_dict()
    assert expected_evidence_id(absent) == absent["evidence_id"]
    result = _validate(pair, _finding([absent["evidence_id"]], start=5, end=5), {})
    assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_NOT_RETURNED)

    # Present ledger, stale bytes: integrity passes, replay fails.
    stale = make_evidence(kind="source_excerpt", tool="read_file", base_sha=pair["base"],
                          head_sha=pair["head"], revision="head", path="app.py",
                          line_start=5, line_end=5, excerpt="stale bytes").to_dict()
    result = _validate(pair, _finding([stale["evidence_id"]], start=5, end=5),
                       {stale["evidence_id"]: stale})
    assert expected_evidence_id(stale) == stale["evidence_id"]
    assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_REPLAY_MISMATCH)

    # Internally consistent wrong revision binding: source layer rejects.
    wrong_revision = make_evidence(kind="source_excerpt", tool="read_file", base_sha=pair["base"],
                                   head_sha=pair["head"], revision="head", path="app.py",
                                   line_start=5, line_end=5, revision_sha=pair["base"],
                                   excerpt="changed five").to_dict()
    result = _validate(pair, _finding([wrong_revision["evidence_id"]], start=5, end=5),
                       {wrong_revision["evidence_id"]: wrong_revision})
    assert expected_evidence_id(wrong_revision) == wrong_revision["evidence_id"]
    assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_REVISION_MISMATCH)

    # Valid citations but an invalid anchor: the anchor layer rejects.
    ev = _read(work, "app.py", "head", 5, 5)
    result = _validate(pair, _finding([ev["evidence_id"]], path="missing.py"),
                       {ev["evidence_id"]: ev})
    assert (result.state, result.code) == (V.REJECTED, V.ANCHOR_PATH_NOT_CHANGED)

    # Unchanged control for this mutation family still passes.
    assert _validate(pair, _finding([ev["evidence_id"]], start=5, end=5),
                     {ev["evidence_id"]: ev}).state == V.ATTESTED


def test_forged_excerpt_with_original_digest_fails_integrity(pair):
    """Form B+ : forged excerpt + ORIGINAL digest + recomputed ID must fail
    at integrity (digest-vs-excerpt binding), not slip through to replay."""
    ev = _read(pair["work"], "app.py", "head", 5, 5)
    forged = dict(ev, excerpt="forged bytes")
    assert forged["content_digest"] == ev["content_digest"]
    forged["evidence_id"] = expected_evidence_id(forged)
    assert forged["evidence_id"] != ev["evidence_id"]
    result = _validate(pair, _finding([forged["evidence_id"]], start=5, end=5),
                       {forged["evidence_id"]: forged})
    assert (result.state, result.code) == (V.REJECTED, V.EVIDENCE_ID_INTEGRITY)
