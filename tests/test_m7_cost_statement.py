"""M7 cost-statement regression test (M7.1 §8, §9.3).

The generator is deterministic and performs no inference: it only reads the
frozen ledger and traces. It must never modify them.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "eval"))

import m7_cost_statement as COST

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "eval" / "M7_LEDGER.json").exists(),
    reason="frozen M7 ledger is local-only and not distributed",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cost_statement_preserves_lower_bound_and_unknowns(tmp_path):
    ledger_path = REPO_ROOT / "eval" / "M7_LEDGER.json"
    before = _sha(ledger_path)
    out = COST.build()
    assert out == COST.STATEMENT_PATH
    assert _sha(ledger_path) == before
    statement = json.loads(COST.STATEMENT_PATH.read_text(encoding="utf-8"))
    assert statement["accounted_usd_lower_bound"] == 0.8687
    assert statement["accounted_is_lower_bound_not_total"] is True
    assert len(statement["rows"]) == 25
    by_class: dict[str, int] = {}
    for row in statement["rows"]:
        by_class[row["usage_class"]] = by_class.get(row["usage_class"], 0) + 1
    assert by_class["CONFIRMED_USAGE"] == 11
    assert by_class["CONFIRMED_ZERO_USAGE"] == 6
    assert by_class["UNKNOWN_USAGE"] == 8
    # The ledger's own $0.00 rows with no authoritative usage stay unknown;
    # none may be confirmed zero without authoritative support.
    assert sorted(statement["unknown_executions"]) == [1, 2, 3, 4, 8, 10, 18, 23]
    for row in statement["rows"]:
        if row["usage_class"] == "UNKNOWN_USAGE":
            assert not row["usage"]
