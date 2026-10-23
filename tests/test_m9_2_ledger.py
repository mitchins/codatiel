"""M9.2 §9: the committed ledger must equal a fresh recomputation."""

import json
import os
from pathlib import Path

from eval.m9_2_ledger import build


def test_committed_ledger_matches_artifacts():
    root = Path(os.environ.get("M9_2_ROOT", "/tmp/m9_2"))
    committed = Path("eval/M9_2_LEDGER.json")
    assert committed.exists(), "eval/M9_2_LEDGER.json must be committed"
    expected = json.loads(committed.read_text())
    actual = build(root)
    assert actual == expected, (
        "ledger drift: regenerate with "
        "`python eval/m9_2_ledger.py $M9_2_ROOT eval/M9_2_LEDGER.json'")
