"""SCAFFOLD-01 historical replay (deterministic, hermetic).

Replays the shipped scaffold_preskip_decision over recorded
pre-inference inputs (unit texts + sent file context at the reviewed
heads) and asserts the audited outcome per case: 55 cases total —
16 useful findings, all fail-open (gate 1, prod code); 2 prod-gate
controls; 37 audit/adversarial cases (17 scaffolding skips, 20
fail-opens on shared assertions/waits, unrecognized verdicts, or
test-only gate). The audit found useful findings never suppressed
and scaffolding partially suppressed; the fixture pins both.
"""

from __future__ import annotations

import json
from pathlib import Path

from codatiel_i2.semdup_dev import editorial_shadow as ES

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures"
     / "scaffold01_replay.json").read_text())


def test_fixture_meta_matches_shipped_rule():
    meta = _FIXTURE["meta"]
    assert meta["rule"] == ES.SCAFFOLD_RULE
    assert set(ES.SCAFFOLD_ASSERT_TOKENS) <= set(meta["tokens"])


def test_historical_replay():
    cases = _FIXTURE["cases"]
    assert len(cases) == 55, "replay corpus must stay comprehensive"
    skips = fails = 0
    for case in cases:
        t = case["target"]
        c = case["counterpart"]
        dec = ES.scaffold_preskip_decision(
            {"path": t["path"], "text": t.get("text"),
             "context": t.get("context")},
            {"path": c["path"], "text": c.get("text"),
             "context": c.get("context")})
        assert dec is not None
        if case["expect_skip"]:
            assert dec["skip"] is True, case["id"]
            assert case["expect_reason"] in dec["detail"], case["id"]
            skips += 1
        else:
            assert dec["skip"] is False, case["id"]
            assert case["expect_reason"] in dec["detail"], case["id"]
            fails += 1
    # The audit found useful findings never suppressed and scaffolding
    # partially suppressed; the fixture must reflect both.
    assert skips > 0 and fails > skips
    useful = [c for c in cases if c["id"].startswith("useful-")]
    assert useful and all(not c["expect_skip"] for c in useful)
    scaff = [c for c in cases if c["id"].startswith("scaf-")]
    assert any(c["expect_skip"] for c in scaff)
    assert any(not c["expect_skip"] for c in scaff)
