"""DIGEST-01 §4: feedback rating correctness (no inference).

Covers: invalid indices (#0, negative, non-numeric, out-of-range),
duplicate submissions (repeat rating updates judgment; backlog counts
the finding once), rating updates (latest wins, history preserved),
missing findings (unknown run / unknown ID rejected), and accurate
backlog reporting (distinct-rated completion, never raw event counts).
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import m10_feedback as FB  # noqa: E402


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("M10_ROOT", str(tmp_path))
    import importlib
    importlib.reload(FB)
    run = tmp_path / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": "r1"}))
    (run / "findings.jsonl").write_text(
        "\n".join(json.dumps({"finding_id": f"finding_{i:03d}"})
                   for i in range(1, 4)) + "\n")
    (run / "model_findings_raw.jsonl").write_text(
        "\n".join(json.dumps({"title": f"claim {i}"}) for i in (1, 2)) + "\n")
    return tmp_path


def _report():
    buf = io.StringIO()
    with redirect_stdout(buf):
        FB.report()
    lines = buf.getvalue().strip().splitlines()
    split = next(i for i in range(1, len(lines)) if lines[i] == "{")
    return (json.loads("\n".join(lines[:split])),
            json.loads("\n".join(lines[split:])))


def test_hash_zero_rejected_not_last_finding(root: Path):
    with pytest.raises(SystemExit, match="no submitted finding #0"):
        FB.rate("r1", "#0", "useful")
    assert not (root / "feedback.jsonl").exists()


def test_hash_negative_rejected(root: Path):
    with pytest.raises(SystemExit, match="no submitted finding #-2"):
        FB.rate("r1", "#-2", "useful")


def test_hash_nonnumeric_rejected(root: Path):
    with pytest.raises(SystemExit, match="not a 1-based index"):
        FB.rate("r1", "#x", "useful")


def test_hash_out_of_range_rejected(root: Path):
    with pytest.raises(SystemExit, match="valid: #1..#2"):
        FB.rate("r1", "#3", "useful")


def test_unknown_run_and_id_rejected(root: Path):
    with pytest.raises(SystemExit, match="unknown run_id"):
        FB.rate("nope", "#1", "useful")
    with pytest.raises(SystemExit, match="not in run"):
        FB.rate("r1", "finding_999", "useful")
    with pytest.raises(SystemExit, match="rating must be one of"):
        FB.rate("r1", "#1", "maybe")


def test_valid_refs_rate_and_backlog_clears(root: Path):
    FB.rate("r1", "#1", "useful")
    FB.rate("r1", "#2", "wrong", "looks off")
    FB.rate("r1", "finding_001", "low_value")
    counts, backlog = _report()
    assert counts["n_rating_events"] == 3
    assert counts["n_rated_findings"] == 3
    assert backlog["n_runs_with_unrated"] == 0


def test_duplicate_rating_updates_backlog_counts_once(root: Path):
    FB.rate("r1", "#1", "useful")
    FB.rate("r1", "#1", "wrong", "on reflection: false positive")
    rows = (root / "feedback.jsonl").read_text().strip().splitlines()
    assert len(rows) == 2  # history preserved verbatim
    counts, backlog = _report()
    assert counts["n_rating_events"] == 2
    assert counts["n_rated_findings"] == 1
    assert counts["by_rating"] == {"wrong": 1}  # latest wins
    assert backlog["n_runs_with_unrated"] == 1
    entry = backlog["backlog"][0]
    assert entry["submitted"] == 2 and entry["rated"] == 1
    assert entry["unrated"] == ["#2"]


def test_repeat_rating_cannot_exhaust_backlog(root: Path):
    """The reported bug: rating one finding N times hid the others."""
    for _ in range(5):
        FB.rate("r1", "#1", "useful")
    _, backlog = _report()
    assert backlog["n_runs_with_unrated"] == 1
    assert backlog["backlog"][0]["unrated"] == ["#2"]


def test_invalid_rating_rejected(root: Path):
    with pytest.raises(SystemExit):
        FB.rate("r1", "#1", "bogus")
