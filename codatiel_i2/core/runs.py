"""Immutable run directories (lifted concepts from github_tail/pr_harvest).

Layout: runs/<scan_key>/<run_id>/

- scan_key = hash(repo + base + head + detector/model/index config):
  the same experiment arm always lands in the same scan directory.
- run_id = unique execution ID: rerunning never mutates a previous
  execution. Repeat runs are byte-comparable, never overwritten.

Every scan writes:
run.json, pr.json, changes.json, file_classification.json,
detector_coverage.json, candidates/*.jsonl, evidence.jsonl,
verifications.jsonl, findings.jsonl, publish_preview.json, metrics.json,
tool_calls.jsonl, model_calls.jsonl, dashboard.md

No artifact is reconstructed from prose.

Determinism scope: same scan_key + same code yields identical content
artifacts. Excluded: wall-clock run.created_at, metrics.runtime_seconds,
absolute worktree dirs. Phase A.5 assumes one writer per run dir (no lock).
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from codatiel_i2.common.hashing import sha256_text, short_id
from codatiel_i2.common.jsonio import canonical_json, write_json, write_jsonl
from codatiel_i2.common.time import utc_now_iso
from codatiel_i2.core.findings import validate_finding

SCHEMA_VERSION = "codatiel_i2.run.v1"

CANDIDATE_FILES = ("docs_code", "contracts", "authorization", "operating_constraints", "semantic_twins")


def fingerprint_config(config: dict) -> dict:
    """Stable fingerprint over the FULL config: unknown keys change the arm.

    Nothing is silently dropped — any future knob (temperature, threshold,
    prompt version) lands in extra_digest and yields a distinct scan_key.
    """
    known = {
        "detectors": sorted(config.get("detectors") or []),
        "models": config.get("models", "none"),
        "index": config.get("index", "none"),
        "codatiel_i2_version": config.get("codatiel_i2_version", "0.1.0"),
    }
    extra = {k: v for k, v in config.items() if k not in known}
    return {
        "schema_version": "codatiel_i2.scan_config.v1",
        **known,
        "extra_digest": sha256_text(canonical_json(extra)),
    }


def scan_key_for(repo: str, base_sha: str, head_sha: str, config: dict) -> str:
    fingerprinted = fingerprint_config(config)
    return short_id(repo, base_sha, head_sha, canonical_json(fingerprinted))


def new_run_id() -> str:
    candidate = secrets.token_hex(8)
    if not all(c in "0123456789abcdef" for c in candidate):
        raise RuntimeError("run_id generator produced non-hex output")
    return candidate


def new_run_dir(runs_root: Path, scan_key: str, run_id: str) -> Path:
    return Path(runs_root) / scan_key / run_id


def write_run(*, run_dir: Path, scan_key: str, run_id: str, repo: str,
              base_sha: str, head_sha: str, config: dict,
              workspace: dict, changes: list[dict], classification: dict,
              coverage: dict, candidates: dict[str, list[dict]],
              evidence: list[dict], verifications: list[dict],
              findings: list[dict], metrics: dict,
              tool_calls: list[dict] | None = None,
              model_calls: list[dict] | None = None) -> Path:
    from codatiel_i2.dashboard import build as dashboard_mod

    run_dir = Path(run_dir)
    # Bind the directory to its scan identity: recompute, never trust.
    if scan_key_for(repo, base_sha, head_sha, config) != scan_key:
        raise ValueError(f"scan_key does not match repo/base/head/config for {run_dir}")
    expected_dir = Path(run_dir)
    if expected_dir.exists() and any(expected_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite immutable run dir: {run_dir}")
    (run_dir / "candidates").mkdir(parents=True, exist_ok=True)
    fingerprinted = fingerprint_config(config)
    pr_payload = {"repo": repo, "base_sha": base_sha, "head_sha": head_sha}
    write_json(run_dir / "run.json", {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "scan_key": scan_key,
        "config": config,
        "config_fingerprint": fingerprinted,
        "repo": repo,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "created_at": utc_now_iso(),
        "workspace": workspace,
    })
    write_json(run_dir / "pr.json", pr_payload)
    write_json(run_dir / "changes.json", {"schema_version": "codatiel_i2.changes.v1", "changes": sorted(changes, key=lambda r: r["path"])})
    write_json(run_dir / "file_classification.json", classification)
    write_json(run_dir / "detector_coverage.json", coverage)
    for detector in CANDIDATE_FILES:
        write_jsonl(run_dir / "candidates" / f"{detector}.jsonl", candidates.get(detector, []))
    write_jsonl(run_dir / "evidence.jsonl", evidence)
    write_jsonl(run_dir / "verifications.jsonl", verifications)
    write_jsonl(run_dir / "findings.jsonl", findings)
    write_jsonl(run_dir / "tool_calls.jsonl", tool_calls or [])
    write_jsonl(run_dir / "model_calls.jsonl", model_calls or [])
    publishable = [f for f in findings if f.get("publish_eligible") and not validate_finding(f)]
    write_json(run_dir / "publish_preview.json", {
        "schema_version": "codatiel_i2.publish_preview.v1",
        "validation": "syntactic_only_never_publish",
        # Fail closed: without a full trust context nothing is listed as
        # publishable. Syntactic rows are preview candidates only.
        "publishable": [],
        "publishable_count": 0,
        "syntactic_candidates": publishable[:5],
        "syntactic_candidates_truncated": len(publishable) > 5,
        "mode": "report_only",
    })
    write_json(run_dir / "metrics.json", metrics)
    dashboard_mod.write_dashboard(
        run_dir,
        dashboard_mod.render_markdown(repo=repo, base_sha=base_sha, head_sha=head_sha,
                                      coverage=coverage, findings=findings),
    )
    return run_dir


def required_artifacts(run_dir: Path) -> list[str]:
    expected = ["run.json", "pr.json", "changes.json", "file_classification.json",
                "detector_coverage.json", "evidence.jsonl", "verifications.jsonl",
                "findings.jsonl", "publish_preview.json", "metrics.json",
                "tool_calls.jsonl", "model_calls.jsonl", "dashboard.md"]
    expected += [f"candidates/{d}.jsonl" for d in CANDIDATE_FILES]
    return expected
