"""Acceptance: no legacy/CodeQuokka dependency in the new package."""

from __future__ import annotations

from pathlib import Path

BANNED = (
    "code_quokka",
    "agents_sdk_spike",
    "phase_a_sanity",
    "phase_b_auto_explorer",
    "tool_intent",
    "semantic_judge",
    "base_review_prompt",
    "noise_pack_replay",
    "finalizer",
    "reviewed_clean",
)

ROOT = Path(__file__).resolve().parent.parent / "codatiel_i2"
EVAL_ROOT = Path(__file__).resolve().parent.parent / "eval" / "docs_code"


def test_no_legacy_imports():
    offenders: list[str] = []
    for root in (ROOT, EVAL_ROOT):
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in BANNED:
                if token in text:
                    offenders.append(f"{path.relative_to(root.parent.parent)}: {token}")
    assert offenders == [], f"legacy dependencies found: {offenders}"
