"""Offline forensic replay of the 19 archived M7 model findings.

This script never modifies original repositories, workspaces, historical
traces, ledgers, or verdicts. It replays committed raw findings against
disposable copies of the local frozen fixtures and compares the deterministic
result with the auditor's frozen expectations.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "eval"))

from codatiel_i2.verify import evidence_validation as V
from m7_fixture_store import STORE_ROOT, verify_snapshot

TRACES_ROOT = REPO_ROOT / "eval" / "M7_TRACES"
MANIFEST_PATH = REPO_ROOT / "eval" / "M7_FIXTURE_MANIFEST.json"
REPORT_PATH = REPO_ROOT / "eval" / "M7_EVIDENCE_REPLAY.json"

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

# Heads reached only by live-head drift during M7; their fixtures are
# legitimate reconstructions from preserved immutable commits.
RECONSTRUCTED_HEADS = {
    "46c1a890b2d5b4126513711844311e8600ec278f",
    "67a02d083a4b26003f6503419cd41d7dff7d3b8e",
}
EXPECTED = {
    ("16_L-01_haiku_complete", 0): ("REJECTED", "ANCHOR_PATH_NOT_CHANGED"),
    ("16_L-01_haiku_complete", 1): ("ATTESTED", None),
}

ADJUDICATIONS = {
    ("06_FW3_luna_complete", 0): "CONFIRMED: report counts reservations as reboots sent",
    ("06_FW3_luna_complete", 1): "CONFIRMED: restart misclassifies rebooted outages",
    ("09_F-01_haiku_complete", 0): "CONFIRMED: writer-identity fencing break",
    ("11_F-01_luna_complete", 0): "CONFIRMED: writer-identity fencing break",
    ("11_F-01_luna_complete", 1): "PARTIAL: true SQLite atomicity gap, overstated",
    ("14_L-10_luna_complete", 0): "PARTIAL: true scope/coverage observation on a test-pinned control",
    ("14_L-10_luna_complete", 1): "PARTIAL: true generic crash-probe observation on a test-pinned control",
    ("17_L-01_luna_complete", 0): "PARTIAL: true batch fan-out, speculative risk",
    ("17_L-01_luna_complete", 1): "REFUTED: contradicts the documented accountability/fencing model",
    ("19_L-07_gemma_complete", 0): "NOISE",
    ("19_L-07_gemma_complete", 1): "NOISE",
    ("20_L-07_haiku_complete", 0): "NOISE",
}


def _snapshot_pair(run: dict) -> tuple[str, str]:
    repo = run["repo"]
    base_sha = run["base_sha"]
    head_sha = run["head_sha"]
    if repo == "mitchins/frigate-watchdog":
        return "FW3_base_956f7445", "FW3_head_d9b61df6"
    if repo == "/data/coding/needletail":
        return "F01_base_8be7e13d", "F01_head_dd17a957"
    if repo == "mitchins/needletail-harness" and base_sha.startswith("08847b4c"):
        return "L10_base_08847b4c", "L10_head_80bf493d"
    if repo in {"mitchins/needletail-harness", "/tmp/m7/work/needletail-harness"}:
        return "L01_base_f62ccdd9", f"L01_head_{head_sha[:8]}"
    if repo == "Smart-Compliance-Systems/scs-infra":
        return "L07_base_aa807b86", "L07_head_db96b1df"
    raise ValueError(f"no frozen snapshot pair for repository {repo!r}")


def main() -> int:
    if not STORE_ROOT.exists():
        report = {"status": "blocked", "reason": "local fixture store is absent"}
        REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = {entry["snapshot"]: entry for entry in manifest["snapshots"]}
    rows = []
    discrepancies = []
    with tempfile.TemporaryDirectory(prefix="m7-evidence-replay-") as tmp:
        tmp_path = Path(tmp)
        for trace in FINDING_TRACES:
            trace_dir = TRACES_ROOT / trace
            run = json.loads((trace_dir / "run.json").read_text(encoding="utf-8"))
            ledger = {}
            for line in (trace_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    ledger[record["evidence_id"]] = record
            changed = {item["path"] for item in
                       json.loads((trace_dir / "changes.json").read_text(encoding="utf-8"))["changes"]}
            prior = [json.loads(line) for line in
                     (trace_dir / "findings.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            prior_agents = {}
            for item in prior:
                fid = str(item.get("finding_id", ""))
                try:
                    prior_agents[int(fid.rsplit("-", 1)[1])] = item
                except (ValueError, IndexError):
                    continue
            base_key, head_key = _snapshot_pair(run)
            missing = [key for key in (base_key, head_key)
                       if not (STORE_ROOT / "snapshots" / key / "tree").is_dir()]
            if missing:
                for number, line in enumerate((trace_dir / "model_findings_raw.jsonl").read_text(
                        encoding="utf-8").splitlines()):
                    if not line.strip():
                        continue
                    rows.append({
                        "trace": trace, "finding_index": number,
                        "submitted_ids": json.loads(line).get("evidence_ids"),
                        "citation_results": [], "anchor_failures": [],
                        "previous_disposition": None, "previous_reason": None,
                        "new_state": "BLOCKED", "new_code": "SOURCE_SNAPSHOT_UNAVAILABLE",
                        "blockers": [f"frozen snapshot unavailable: {', '.join(missing)}"],
                        "environmental_limitation": "required fixture snapshot is missing",
                        "independent_adjudication": ADJUDICATIONS.get((trace, number)),
                        "expected_live_classification": "unresolved",
                    })
                continue
            copies = {}
            for key in (base_key, head_key):
                source = STORE_ROOT / "snapshots" / key / "tree"
                target = tmp_path / trace / key
                shutil.copytree(source, target, symlinks=False)
                errors = verify_snapshot(target, entries[key])
                if errors:
                    raise RuntimeError(f"fixture copy failed verification for {key}: {errors[0]}")
                copies[key] = target
            for number, line in enumerate((trace_dir / "model_findings_raw.jsonl").read_text(
                    encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                finding = json.loads(line)
                validation = V.validate_model_finding(
                    finding, ledger, base_sha=run["base_sha"], head_sha=run["head_sha"],
                    base_checkout=copies[base_key], head_checkout=copies[head_key],
                    changed_paths=changed)
                expected = EXPECTED.get((trace, number), ("ATTESTED", None))
                row = {
                    "trace": trace,
                    "finding_index": number,
                    "submitted_ids": finding.get("evidence_ids"),
                    "citation_results": [{
                        "index": check.index, "evidence_id": check.evidence_id,
                        "status": check.status, "failures": [list(item) for item in check.failures]}
                        for check in validation.citation_checks],
                    "anchor_failures": [list(item) for item in validation.anchor_failures],
                    "previous_disposition": (prior_agents[number].get("disposition")
                                                 if number in prior_agents else None),
                    "previous_reason": (prior_agents[number].get("reason")
                                        if number in prior_agents else None),
                    "new_state": validation.state,
                    "new_code": validation.code,
                    "blockers": list(validation.blockers),
                    "environmental_limitation": (
                        "pinned L-01 HEAD reconstructed read-only from the preserved "
                        "immutable commit; verified against archived evidence"
                        if run["head_sha"] in RECONSTRUCTED_HEADS else None),
                    "independent_adjudication": ADJUDICATIONS.get((trace, number)),
                    "expected_live_classification": (
                        "advisory_grounded + UNASSESSED" if validation.state == "ATTESTED" else
                        "rejected_invalid" if validation.state == "REJECTED" else "unresolved"),
                }
                if (validation.state, validation.code) != expected:
                    discrepancies.append({"trace": trace, "finding_index": number,
                                          "expected": list(expected),
                                          "actual": [validation.state, validation.code]})
                rows.append(row)
    blocked = [row for row in rows if row["new_code"] == "SOURCE_SNAPSHOT_UNAVAILABLE"
               and row["environmental_limitation"] == "required fixture snapshot is missing"]
    if blocked:
        status, exit_code = "blocked", 2
    else:
        status, exit_code = ("complete", 0) if not discrepancies else ("discrepant", 1)
    report = {"schema_version": "codatiel_i2.m7_evidence_replay.v1",
              "status": status,
              "finding_count": len(rows),
              "positive_controls": [
                  "FW3 Luna report-counts finding attested",
                  "FW3 Luna restart-misclassification finding attested",
                  "F-01 Haiku fencing-break finding attested",
                  "F-01 Luna fencing-break finding attested",
              ],
              "rows": rows, "discrepancies": discrepancies}
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(f"replayed {len(rows)} findings: {report['status']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
