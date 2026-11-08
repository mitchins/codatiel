"""EMAIL-FINAL-01: delivered email must distinguish completed-clean,
finalized-under-budget and inconclusive outcomes.

Regression guard built from the recorded L06 forced-final examples
(FINAL-01 42-run block): a zero-finding `finalized_under_budget` review
must never contain an unqualified completed-investigation claim.
No provider inference; synthetic run dirs only.
"""

import json
from pathlib import Path


def _write_run(tmp_path: Path, name: str, findings: list[dict],
               status: str = "complete",
               metrics_extra: dict | None = None,
               shadow_extra: dict | None = None):
    run_dir = tmp_path / name
    run_dir.mkdir()
    base, head = "a" * 40, "b" * 40
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": name, "repo": "o/r", "pr_number": 1,
        "pr_url": "https://github.com/o/r/pull/1",
        "base_sha": base, "head_sha": head,
        "config": {"model": "luna:t"}}))
    metrics = {"status": status, "model": "luna:t", "tool_calls": 35,
               "spend": {"usd_estimate": 0.0198,
                         "accounting_status": "UNKNOWN_USAGE"}}
    metrics.update(metrics_extra or {})
    (run_dir / "metrics.json").write_text(json.dumps(metrics))
    shadow = {"status": status, "advisory": [],
              "counts": {"advisory_grounded": 0, "observed_not_material": 0,
                         "refuted_unsupported": 0, "rejected_invalid": 0,
                         "unresolved": 0, "verified_proof": 0}}
    shadow.update(shadow_extra or {})
    (run_dir / "shadow_result.json").write_text(json.dumps(shadow))
    (run_dir / "findings.jsonl").write_text(
        "".join(json.dumps(f) + "\n" for f in findings))
    (run_dir / "evidence.jsonl").write_text("")
    ident = {"repo": "o/r", "number": 1,
             "url": "https://github.com/o/r/pull/1",
             "base": base, "head": head}
    return run_dir, ident


_FINAL = {"final_tag": "finalized_under_budget",
          "finalized_under_budget": True,
          "stage": "budget_exhausted_final",
          "error": "turn budget exhausted after 11 turns; "
                   "final disposition: silence"}


def _verified(i: int) -> dict:
    return {"finding_id": f"finding_{i:04d}", "detector": "docs_code",
            "disposition": "verified_proof", "confidence": 0.9,
            "bad_outcome": f"bad {i}", "trigger": "t", "head_state": "h",
            "base_state": "b", "issue_key": f"route|GET|/p{i}",
            "changed_anchor": {"path": "d.md", "line_start": 1,
                               "line_end": 2},
            "evidence_ids": []}


def test_forced_final_zero_findings_never_claims_clean(tmp_path):
    """Recorded L06 shape: complete + finalized + 0 findings."""
    from codatiel_i2 import watcher_manager as M
    run_dir, ident = _write_run(tmp_path, "l06", [], status="complete",
                                metrics_extra=dict(_FINAL))
    subj, body = M.build_email(ident, Path(run_dir))
    assert "(finalized_under_budget)" in subj
    assert "FINALISE" in body
    assert ("NO MATERIAL FINDINGS after a completed investigation."
            not in body)
    assert "NOT a clean" in body or "NOT evidence of a clean" in body


def test_forced_final_with_findings_still_flagged(tmp_path):
    from codatiel_i2 import watcher_manager as M
    run_dir, ident = _write_run(tmp_path, "fin1", [_verified(1)],
                                status="complete",
                                metrics_extra=dict(_FINAL))
    subj, body = M.build_email(ident, Path(run_dir))
    assert "(finalized_under_budget)" in subj
    assert "FINALISE" in body
    assert "--- finding 1" in body  # finding still surfaced
    assert ("NO MATERIAL FINDINGS after a completed investigation."
            not in body)


def test_inconclusive_finalized_flagged(tmp_path):
    from codatiel_i2 import watcher_manager as M
    run_dir, ident = _write_run(tmp_path, "inc", [], status="inconclusive",
                                metrics_extra=dict(_FINAL))
    subj, body = M.build_email(ident, Path(run_dir))
    assert "inconclusive" in subj
    assert "FINALISE" in body
    assert "INCOMPLETE INVESTIGATION" in body
    assert ("NO MATERIAL FINDINGS after a completed investigation."
            not in body)


def test_completed_clean_keeps_clean_claim(tmp_path):
    """No final_tag: the legitimate-silence path is unchanged."""
    from codatiel_i2 import watcher_manager as M
    run_dir, ident = _write_run(tmp_path, "clean", [], status="complete")
    subj, body = M.build_email(ident, Path(run_dir))
    assert "finalized" not in subj
    assert "completed-clean" in body
    assert "NO MATERIAL FINDINGS after a completed investigation." in body
