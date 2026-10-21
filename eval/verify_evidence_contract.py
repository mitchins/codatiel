"""One local verification command for the M7.1 evidence contract.

Runs the offline historical replay first (so the historical suite never reads
a stale report), then the shareable pytest suite, then the cost statement.
Local only: there is no CI in this repository. Prints exactly one of PASS,
FAIL, or BLOCKED.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_ROOT = REPO_ROOT / "eval" / ".m7_fixture_store"


def _ensure_test_interpreter() -> None:
    """Run under an interpreter that has the test dependencies.

    `make verify-evidence-contract` must work without manual virtualenv
    activation. If this interpreter lacks pytest, re-exec under an explicit
    override (`CODATIEL_I2_PYTHON`) or a known repository virtualenv.
    """
    try:
        import pytest  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("CODATIEL_I2_BOOTSTRAPPED"):
        return
    candidates = []
    override = os.environ.get("CODATIEL_I2_PYTHON")
    if override:
        candidates.append(Path(override))
    candidates += [
        Path("/data/coding/codatiel/.venv-codatiel-agents/bin/python"),
        REPO_ROOT / ".venv" / "bin" / "python",
        REPO_ROOT.parent / ".venv-codatiel-agents" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file() and str(candidate) != sys.executable:
            os.environ["CODATIEL_I2_BOOTSTRAPPED"] = "1"
            os.execv(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])


_ensure_test_interpreter()
MANIFEST_PATH = REPO_ROOT / "eval" / "M7_FIXTURE_MANIFEST.json"
REPLAY_SCRIPT = REPO_ROOT / "eval" / "m7_evidence_replay.py"
REPLAY_REPORT = REPO_ROOT / "eval" / "M7_EVIDENCE_REPLAY.json"
COST_SCRIPT = REPO_ROOT / "eval" / "m7_cost_statement.py"
COST_STATEMENT = REPO_ROOT / "eval" / "M7_COST_STATEMENT.json"

SHAREABLE_TESTS = [
    "tests/test_evidence_validation_contract.py",
    "tests/test_fixture_store.py",
    "tests/test_spend_accounting.py",
    "tests/test_canonical_service.py",
    "tests/test_operational_integration.py",
    "tests/test_disposition_route.py",
    "tests/test_trust_gate.py",
    "tests/test_evidence_anchor.py",
    "tests/test_m7_evidence_replay.py",
    "tests/test_m7_cost_statement.py",
]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1200)


def main() -> int:
    if not STORE_ROOT.exists() or not MANIFEST_PATH.exists():
        print("M7.1 evidence contract: BLOCKED (local fixture store or manifest is absent)")
        return 2
    replay_proc = _run([sys.executable, str(REPLAY_SCRIPT)])
    if replay_proc.returncode == 2:
        print("M7.1 evidence contract: BLOCKED (a required fixture snapshot is unavailable)")
        print(replay_proc.stdout[-2000:])
        return 2
    if replay_proc.returncode != 0:
        print("M7.1 evidence contract: FAIL (historical replay reported a discrepancy)")
        print(replay_proc.stdout[-4000:])
        return 1
    report = json.loads(REPLAY_REPORT.read_text(encoding="utf-8"))
    if report.get("status") != "complete" or report.get("finding_count") != 19 \
            or report.get("discrepancies"):
        print("M7.1 evidence contract: FAIL (historical replay report is not clean)")
        return 1
    pytest_proc = _run([sys.executable, "-m", "pytest", *SHAREABLE_TESTS, "-q"])
    if pytest_proc.returncode != 0:
        print("M7.1 evidence contract: FAIL (shareable suite failed)")
        print(pytest_proc.stdout[-4000:])
        print(pytest_proc.stderr[-2000:])
        return 1
    cost_proc = _run([sys.executable, str(COST_SCRIPT)])
    if cost_proc.returncode != 0:
        print("M7.1 evidence contract: FAIL (cost statement failed)")
        print(cost_proc.stdout[-2000:])
        return 1
    statement = json.loads(COST_STATEMENT.read_text(encoding="utf-8"))
    if statement.get("accounted_usd_lower_bound") != 0.8687:
        print("M7.1 evidence contract: FAIL (accounted lower bound moved)")
        return 1
    unknown = len(statement.get("unknown_executions", []))
    print("M7.1 evidence contract: PASS "
          f"(historical findings replayed: {report.get('finding_count')}; "
          f"discrepancies: {len(report.get('discrepancies', []))}; "
          f"accounted lower bound: ${statement.get('accounted_usd_lower_bound')}; "
          f"unknown-cost executions: {unknown})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
