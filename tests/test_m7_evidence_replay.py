"""Local M7.1 historical replay (M7.1 §9.2).

Uses the private frozen fixture store created by eval/m7_fixture_store.py.
Skips when that local store is absent; the verification command reports that
state as blocked rather than green.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "eval"))

STORE_ROOT = REPO_ROOT / "eval" / ".m7_fixture_store"
REPORT_PATH = REPO_ROOT / "eval" / "M7_EVIDENCE_REPLAY.json"

pytestmark = pytest.mark.skipif(not STORE_ROOT.exists(), reason="local M7.1 fixture store is absent")


def _report() -> dict:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_replay_report_covers_all_19_findings():
    report = _report()
    assert report["status"] == "complete", report.get("discrepancies")
    assert report["finding_count"] == 19
    assert report["discrepancies"] == []


def test_mandatory_positive_controls_are_attested():
    rows = {(row["trace"], row["finding_index"]): row for row in _report()["rows"]}
    for key in (("06_FW3_luna_complete", 0), ("06_FW3_luna_complete", 1),
                ("09_F-01_haiku_complete", 0), ("11_F-01_luna_complete", 0)):
        assert rows[key]["new_state"] == "ATTESTED", key


def test_partial_fixture_store_is_blocked_not_green(tmp_path, monkeypatch):
    import shutil
    import sys

    sys.path.insert(0, str(REPO_ROOT / "eval"))
    import m7_evidence_replay as replay

    partial = tmp_path / "partial-store"
    shutil.copytree(STORE_ROOT, partial, symlinks=True)
    shutil.rmtree(partial / "snapshots" / "L01_head_46c1a890" / "tree")
    monkeypatch.setattr(replay, "STORE_ROOT", partial)
    monkeypatch.setattr(replay, "REPORT_PATH", tmp_path / "partial-report.json")
    assert replay.main() == 2
    report = json.loads((tmp_path / "partial-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert any(row["new_code"] == "SOURCE_SNAPSHOT_UNAVAILABLE" for row in report["rows"])


def test_known_anchor_rejection_and_reconstructed_snapshot():
    rows = {(row["trace"], row["finding_index"]): row for row in _report()["rows"]}
    first = rows[("16_L-01_haiku_complete", 0)]
    assert (first["new_state"], first["new_code"]) == ("REJECTED", "ANCHOR_PATH_NOT_CHANGED")
    second = rows[("16_L-01_haiku_complete", 1)]
    assert second["new_state"] == "ATTESTED"
    assert "reconstructed read-only" in (second["environmental_limitation"] or "")


def test_historical_adjudication_annotations_are_annotation_only():
    # Evaluation annotations only: they never enter production rendering or
    # disposition. L-01 Luna batch fan-out is PARTIAL; the three L-07
    # refuted claims are NOISE.
    rows = {(row["trace"], row["finding_index"]): row for row in _report()["rows"]}
    assert rows[("17_L-01_luna_complete", 0)]["independent_adjudication"].startswith("PARTIAL")
    assert rows[("19_L-07_gemma_complete", 0)]["independent_adjudication"] == "NOISE"
    assert rows[("19_L-07_gemma_complete", 1)]["independent_adjudication"] == "NOISE"
    assert rows[("20_L-07_haiku_complete", 0)]["independent_adjudication"] == "NOISE"
    for key in (("17_L-01_luna_complete", 0), ("19_L-07_gemma_complete", 0),
                ("19_L-07_gemma_complete", 1), ("20_L-07_haiku_complete", 0)):
        assert rows[key]["new_state"] == "ATTESTED"
        assert rows[key]["expected_live_classification"] == "advisory_grounded + UNASSESSED"
