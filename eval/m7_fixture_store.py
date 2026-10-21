"""Build the local M7.1 frozen fixture store from immutable commits.

This helper only reads the original repositories and execution workspaces.
It never modifies `/data/coding/needletail`, `/tmp/m7/work`, or any other
original. The extracted bytes stay in the git-ignored local fixture store;
the committed companion manifest contains metadata and digests only.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACES_ROOT = REPO_ROOT / "eval" / "M7_TRACES"
STORE_ROOT = REPO_ROOT / "eval" / ".m7_fixture_store"
MANIFEST_PATH = REPO_ROOT / "eval" / "M7_FIXTURE_MANIFEST.json"
SHA_MARKER = ".codatiel_i2_sha"

# Only the 11 trace directories containing the 19 archived raw findings.
FINDING_TRACES = (
    "00_FW3_gemma_complete",
    "06_FW3_luna_complete",
    "09_F-01_haiku_complete",
    "11_F-01_luna_complete",
    "14_L-10_luna_complete",
    "15_L-01_gemma_complete",
    "16_L-01_haiku_complete",
    "17_L-01_luna_complete",
    "19_L-07_gemma_complete",
    "20_L-07_haiku_complete",
    "64_L-01_luna_complete",
)

SNAPSHOTS = (
    {
        "key": "FW3_base_956f7445",
        "repo_identity": "mitchins/frigate-watchdog",
        "claimed_sha": "956f7445fa13e93309dc5809395cd316ae4d7403",
        "role": "base",
        "archive_repo": "/tmp/m7/work/mitchins__frigate-watchdog/pr-3",
    },
    {
        "key": "FW3_head_d9b61df6",
        "repo_identity": "mitchins/frigate-watchdog",
        "claimed_sha": "d9b61df6c889a28b0cb49547979897533b873ba4",
        "role": "head",
        "archive_repo": "/tmp/m7/work/mitchins__frigate-watchdog/pr-3",
    },
    {
        "key": "F01_base_8be7e13d",
        "repo_identity": "needletail local source",
        "claimed_sha": "8be7e13d1b7e80e17f0c261d82e19d8fb7fd6990",
        "role": "base",
        "archive_repo": "/data/coding/needletail",
    },
    {
        "key": "F01_head_dd17a957",
        "repo_identity": "needletail local source",
        "claimed_sha": "dd17a957e43d7181d71891d86adf15dbaf40f1a4",
        "role": "head",
        "archive_repo": "/data/coding/needletail",
    },
    {
        "key": "L10_base_08847b4c",
        "repo_identity": "mitchins/needletail-harness",
        "claimed_sha": "08847b4cd2691c127597218dfd31457bf246145f",
        "role": "base",
        "archive_repo": "/tmp/m7/work/mitchins__needletail-harness/pr-4",
    },
    {
        "key": "L10_head_80bf493d",
        "repo_identity": "mitchins/needletail-harness",
        "claimed_sha": "80bf493d1cae6d9d40fe70894ee294be80292af2",
        "role": "head",
        "archive_repo": "/tmp/m7/work/mitchins__needletail-harness/pr-4",
    },
    {
        "key": "L01_base_f62ccdd9",
        "repo_identity": "mitchins/needletail-harness",
        "claimed_sha": "f62ccdd9de8ed31897f61bc63dbe60e30e45e8be",
        "role": "base",
        "archive_repo": "/tmp/m7/work/needletail-harness",
        "provenance": (
            "reconstructed read-only from immutable commit "
            "f62ccdd9 in the preserved M7 needletail-harness object store"
        ),
    },
    {
        "key": "L01_head_e0d405f5",
        "repo_identity": "mitchins/needletail-harness",
        "claimed_sha": "e0d405f5ae884224cd738cbc0244970eff89bb20",
        "role": "head",
        "archive_repo": "/tmp/m7/work/needletail-harness",
        "provenance": (
            "reconstructed read-only from immutable commit "
            "e0d405f5 in the preserved M7 needletail-harness object store"
        ),
    },
    {
        "key": "L01_head_46c1a890",
        "repo_identity": "mitchins/needletail-harness",
        "claimed_sha": "46c1a890b2d5b4126513711844311e8600ec278f",
        "role": "head",
        "archive_repo": "/tmp/m7/work/needletail-harness",
        "provenance": (
            "reconstructed read-only from immutable drifted commit "
            "46c1a890 in the preserved M7 needletail-harness object store"
        ),
    },
    {
        "key": "L01_head_67a02d08",
        "repo_identity": "mitchins/needletail-harness",
        "claimed_sha": "67a02d083a4b26003f6503419cd41d7dff7d3b8e",
        "role": "head",
        "archive_repo": "/tmp/m7/work/needletail-harness",
        "provenance": (
            "reconstructed read-only from immutable drifted commit "
            "67a02d08 in the preserved M7 needletail-harness object store"
        ),
    },
    {
        "key": "L07_base_aa807b86",
        "repo_identity": "Smart-Compliance-Systems/scs-infra",
        "claimed_sha": "aa807b86fda364ef0723b536672e07cca4646c6e",
        "role": "base",
        "archive_repo": "/tmp/m7/work/Smart-Compliance-Systems__scs-infra/pr-5",
    },
    {
        "key": "L07_head_db96b1df",
        "repo_identity": "Smart-Compliance-Systems/scs-infra",
        "claimed_sha": "db96b1df3066ef048c12efeae59fa522018e5c19",
        "role": "head",
        "archive_repo": "/tmp/m7/work/Smart-Compliance-Systems__scs-infra/pr-5",
    },
)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _archive_bytes(repo: str, sha: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", repo, "archive", "--format=tar", sha],
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git archive failed for {repo}@{sha[:8]}: "
            f"{proc.stderr.decode('utf-8', 'replace')[:500]}"
        )
    return proc.stdout


def _extract_tracked_source(archive: bytes, dest: Path) -> dict:
    """Extract tracked regular files only.

    Deliberately drops `.git` entries, every symlink/hardlink, and other
    special archive members. It does not follow links from the original.
    """
    files: list[dict] = []
    skipped = {"git": 0, "link": 0, "special": 0}
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tf:
        for member in tf.getmembers():
            name = member.name
            if name in (".git", "./.git") or name.startswith((".git/", "./.git/")):
                skipped["git"] += 1
                continue
            if member.issym() or member.islnk():
                skipped["link"] += 1
                continue
            if not member.isfile():
                skipped["special"] += 1
                continue
            target = dest / name
            # Tar members were validated above; fixtures are regular files in
            # the fixture root. `safe_join` behavior is duplicated locally so
            # this exporter has no product-code dependency.
            root = dest.resolve()
            candidate = (root / name).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"unsafe archive member: {name!r}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"unreadable archive member: {name!r}")
            data = extracted.read()
            target.write_bytes(data)
            files.append(
                {
                    "path": name,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "git_mode": "100755" if (member.mode & 0o111) else "100644",
                }
            )
    files.sort(key=lambda item: item["path"])
    return {"files": files, "skipped": skipped}


def _snapshot_manifest(snapshot: dict, extraction: dict) -> dict:
    canonical = json.dumps(
        extraction["files"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    provenance = snapshot.get("provenance")
    if provenance is None:
        provenance = (
            f"read-only git archive of immutable commit {snapshot['claimed_sha']} "
            f"from preserved source {snapshot.get('archive_repo', 'synthetic test fixture')}"
        )
    return {
        "snapshot": snapshot["key"],
        "repo_identity": snapshot["repo_identity"],
        "role": snapshot["role"],
        "claimed_sha": snapshot["claimed_sha"],
        "acquisition_provenance": provenance,
        "sha_marker": SHA_MARKER,
        "file_count": len(extraction["files"]),
        "skipped_members": extraction["skipped"],
        "content_manifest_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "files": extraction["files"],
    }


def _archived_references() -> dict:
    """Map snapshots to the committed archived evidence records that cite them."""
    references: dict[str, dict] = {}
    for trace in FINDING_TRACES:
        trace_dir = TRACES_ROOT / trace
        run = _read_json(trace_dir / "run.json")
        base_sha = run["base_sha"]
        head_sha = run["head_sha"]
        ledger = {}
        for line in (trace_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                ledger[record["evidence_id"]] = record
        for line in (trace_dir / "model_findings_raw.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            finding = json.loads(line)
            for evidence_id in finding.get("evidence_ids") or []:
                record = ledger.get(evidence_id)
                if record is None:
                    raise RuntimeError(f"{trace}: archived evidence {evidence_id} is absent")
                revision = record.get("revision")
                if revision == "base":
                    snapshot_shas = [base_sha]
                    endpoint = "base"
                elif revision == "head":
                    snapshot_shas = [head_sha]
                    endpoint = "head"
                elif revision == "diff":
                    # A diff record binds the whole BASE..HEAD comparison. List it
                    # under both endpoints so every fixture knows which archived
                    # diff records it must be able to replay.
                    snapshot_shas = [record.get("base_sha"), record.get("head_sha")]
                    endpoint = "diff-pair-endpoint"
                else:
                    snapshot_shas = [record.get("revision_sha", "")]
                    endpoint = "non-repository-revision"
                for snapshot_sha in snapshot_shas:
                    bucket = references.setdefault(snapshot_sha, [])
                    row = {
                        "trace": trace,
                        "evidence_id": evidence_id,
                        "kind": record.get("kind"),
                        "revision": revision,
                        "recorded_sha": record.get("revision_sha"),
                        "recorded_pair": [record.get("base_sha"), record.get("head_sha")],
                        "fixture_endpoint": endpoint,
                        "path": record.get("path"),
                    }
                    if all(existing.get("evidence_id") != evidence_id for existing in bucket):
                        bucket.append(row)
    return references


def verify_snapshot(snapshot_dir: Path, entry: dict) -> list[str]:
    """Check a fixture snapshot's SHA marker and content manifest."""
    errors: list[str] = []
    tree = Path(snapshot_dir)
    if (tree / ".git").exists():
        errors.append("fixture snapshot must not contain .git material")
    try:
        if (tree / SHA_MARKER).read_text(encoding="utf-8").strip() != entry["claimed_sha"]:
            errors.append("fixture SHA marker does not hold the claimed commit")
    except OSError as exc:
        errors.append(f"fixture SHA marker unreadable: {type(exc).__name__}")
        return errors
    expected = {item["path"]: item for item in entry.get("files", [])}
    seen: set[str] = set()
    for path in sorted(tree.rglob("*")):
        if path.name == SHA_MARKER:
            continue
        if path.is_symlink():
            errors.append(f"fixture snapshot contains a symlink: {path.name!r}")
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(tree).as_posix()
        seen.add(rel)
        data = path.read_bytes()
        want = expected.get(rel)
        if want is None:
            errors.append(f"fixture snapshot contains an unmanifested file: {rel!r}")
            continue
        if len(data) != want["bytes"] or hashlib.sha256(data).hexdigest() != want["sha256"]:
            errors.append(f"fixture file does not match its manifest: {rel!r}")
    for rel in sorted(set(expected) - seen):
        errors.append(f"fixture manifest file is missing: {rel!r}")
    if len(expected) != entry.get("file_count"):
        errors.append("fixture manifest file_count does not match its file list")
    canonical = json.dumps(
        entry.get("files", []), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != entry.get("content_manifest_sha256"):
        errors.append("fixture content manifest digest does not match its file list")
    return errors


def build() -> Path:
    references = _archived_references()
    snapshots_root = STORE_ROOT / "snapshots"
    manifest_snapshots = []
    for snapshot in SNAPSHOTS:
        dest = snapshots_root / snapshot["key"] / "tree"
        if dest.exists():
            # Never merge into a stale tree: rebuild deterministically.
            shutil.rmtree(dest)
        archive = _archive_bytes(snapshot["archive_repo"], snapshot["claimed_sha"])
        extraction = _extract_tracked_source(archive, dest)
        (dest / SHA_MARKER).write_text(snapshot["claimed_sha"] + "\n", encoding="utf-8")
        entry = _snapshot_manifest(snapshot, extraction)
        entry["archived_evidence_references"] = sorted(
            references.get(snapshot["claimed_sha"], []),
            key=lambda item: (item["trace"], item["evidence_id"]),
        )
        (snapshots_root / snapshot["key"] / "snapshot.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_snapshots.append(entry)
    manifest = {
        "schema_version": "codatiel_i2.m7_fixture_manifest.v1",
        "source": "M7_TRACES committed raw findings and evidence ledgers",
        "store": "eval/.m7_fixture_store (local, git-ignored; source bytes are not committed)",
        "snapshots": manifest_snapshots,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return MANIFEST_PATH


if __name__ == "__main__":
    print(build())
