"""Unit tests for eval/semdup_signal01_report.py (no inference).

Guards the percentile/denominator semantics: per-run cost/examined is
defined only over runs with pairs_examined > 0; legacy zero-exam runs
report spend separately and never enter the distribution.
"""
import json
import sys
from pathlib import Path


def _write_run(root: Path, base: str, head: str, doc: dict) -> None:
    run_dir = root / "runs" / base / head
    run_dir.mkdir(parents=True)
    (run_dir / "semdup.json").write_text(json.dumps(doc))


def _report(root: Path, capsys) -> str:
    sys.path.insert(0, "eval")
    try:
        import semdup_signal01_report as rep
        rep.main(str(root))
    finally:
        sys.path.remove("eval")
    return capsys.readouterr().out


def test_zero_exam_runs_excluded_from_distribution(tmp_path, capsys):
    root = tmp_path / "state"
    _write_run(root, "b1", "h1", {"status": "ok", "pairs_examined": 100,
                                  "spend_usd": 1.0, "published": [1, 2],
                                  "suppressed": []})
    _write_run(root, "b2", "h2", {"status": "ok", "pairs_examined": 10,
                                  "spend_usd": 0.5, "published": [],
                                  "suppressed": [{"reason": "terra_no_lead"}]})
    # Legacy run: spend, no examinations, no denominator.
    _write_run(root, "b3", "h3", {"status": "ok", "spend_usd": 0.03,
                                  "published": [], "suppressed": []})
    # Non-ok runs never count.
    _write_run(root, "b4", "h4", {"status": "unavailable"})
    out = _report(root, capsys)
    assert "runs=3 examined=110 spend_usd=1.5300 published=2" in out
    assert "cost_per_examined=0.013909" in out
    # Distribution over the 2 examined runs only: {0.01, 0.05}.
    assert "per_run_cost_per_examined_USD_n=2 mean=0.030000" in out
    assert "zero_examined_runs=1 zero_examined_spend_usd=0.0300" in out
    assert "suppressed_by_reason={\"terra_no_lead\": 1}" in out
    # The legacy $0.03 must not leak into the per-run distribution.
    assert "max=0.050000" in out


def test_empty_state_reports_no_distribution(tmp_path, capsys):
    out = _report(tmp_path / "empty", capsys)
    assert "runs=0 examined=0" in out
    assert "per_run_cost_per_examined_USD" not in out
