"""Live GitHub contract tests (read-only; no inference, no writes).

Gated on CODATIEL_LIVE_CONTRACT=1 with ambient `gh` auth. Verifies the
real gh/App response contract against known cases:
  mitchins/codatiel#4, #5 : merged/closed negative controls
  mitchins/codatiel#6     : historical open PR, excluded
  mitchins/codatiel#8     : historical open PR, excluded
"""

from __future__ import annotations

import os
import subprocess

import pytest

from codatiel_i2 import watcher_eligibility as E

LIVE = os.environ.get("CODATIEL_LIVE_CONTRACT") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="live contract gate")


def gh_pr(repo: str, number: int) -> dict:
    proc = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json",
         E.PR_FIELDS],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:500]
    import json
    return json.loads(proc.stdout)


def test_merged_prs_rejected():
    for number in (4, 5):
        raw = gh_pr("mitchins/codatiel", number)
        vpr, reason = E.validate_pr_metadata("mitchins/codatiel", raw)
        assert vpr is None, f"#{number} should not validate: {vpr}"
        assert reason, "rejection needs a reason"
    # mergedAt present and state closed on the raw contract
    raw4 = gh_pr("mitchins/codatiel", 4)
    assert raw4.get("mergedAt")
    assert raw4.get("state") != "OPEN"


def test_historical_open_prs_excluded_but_wellformed():
    for number in (6, 8):
        raw = gh_pr("mitchins/codatiel", number)
        vpr, reason = E.validate_pr_metadata("mitchins/codatiel", raw)
        assert vpr is not None, f"#{number} should validate: {reason}"
        assert vpr.state == "OPEN"
        assert not vpr.merged_at
        assert E.FULL_SHA.match(vpr.base_sha)
        assert E.FULL_SHA.match(vpr.head_sha)
        ok, why = E.check_eligible(vpr, "2026-10-24T00:00:00Z")
        assert not ok
        assert "historical" in why, (number, why)


def test_field_contract_shape():
    raw = gh_pr("mitchins/codatiel", 8)
    for field in ("number", "url", "createdAt", "state", "isDraft",
                  "baseRefOid", "headRefOid", "baseRefName", "headRefName"):
        assert field in raw, f"contract field missing: {field}"
    assert isinstance(raw["isDraft"], bool)
    assert isinstance(raw["number"], int)
    assert raw["url"].endswith("/pull/8")
