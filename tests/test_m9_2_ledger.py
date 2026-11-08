"""M9.2 §9: the committed ledger must equal a fresh recomputation."""

import json
import os
from pathlib import Path

import pytest

from eval.m9_2_ledger import build

_M9_2_ROOT = Path(os.environ.get("M9_2_ROOT", "/tmp/m9_2"))


def _fixture_present() -> bool:
    return _M9_2_ROOT.is_dir() and any(_M9_2_ROOT.rglob("run.json"))


pytestmark = pytest.mark.skipif(
    not _fixture_present(),
    reason="local M9.2 run archive is absent (set M9_2_ROOT to recompute; "
    "regenerate with `python eval/m9_2_ledger.py $M9_2_ROOT "
    "eval/M9_2_LEDGER.json`)",
)


def test_committed_ledger_matches_artifacts():
    root = _M9_2_ROOT
    committed = Path("eval/M9_2_LEDGER.json")
    assert committed.exists(), "eval/M9_2_LEDGER.json must be committed"
    expected = json.loads(committed.read_text())
    actual = build(root)
    assert actual == expected, (
        "ledger drift: regenerate with "
        "`python eval/m9_2_ledger.py $M9_2_ROOT eval/M9_2_LEDGER.json'")
