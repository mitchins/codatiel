"""REVIEW-CONTRACT-01C: live matched comparison runner.

Neutral opaque case IDs. Oracle labels (expected kinds, mechanism
notes) live ONLY in this file and the decision report — never in
model-visible input. The investigator sees: opaque ID, changed files,
and whatever it pulls through read-only tools.

Usage:
  .venv/bin/python eval/live_01c.py --only CASE-A:P-GUIDEBOOK
  .venv/bin/python eval/live_01c.py --only CASE-A:P-SHORT,CASE-B:P-GUIDEBOOK
  .venv/bin/python eval/live_01c.py   # everything in LIVE_CASES
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/data/coding/codatiel/codatiel-i2")
from eval.live_model import LiveModel
from eval.review_harness import ReviewHarness

BASE_URL = "http://192.168.1.14:8000/v1"
MODEL = "reinforce20001/gemma4-26b-a4b-it-qat-w4a16-ct"
TEMPERATURE = 0.0
MAX_TOKENS = 768
TOKEN_BUDGET = 200000
TOOL_BUDGET = 15
TIMEOUT_S = 900

AUTH1 = Path("/data/coding/codatiel/codatiel-i2/eval/auth1/corpus")
SNAP = Path("/data/coding/codatiel/org-snapshots")

# opaque_id -> (base, head, expected_kind, private_notes)
# expected_kind: proposal | silence | internal (post-hoc scoring only)
LIVE_CASES: dict[str, tuple[str, str, str, str]] = {
    "CASE-A": (str(AUTH1 / "fam-005" / "base"),
               str(AUTH1 / "fam-005" / "broken"),
               "proposal", "fam-005/broken: OWNER_ONLY set widening"),
    "CASE-B": (str(AUTH1 / "fam-005" / "base"),
               str(AUTH1 / "fam-005" / "safe"),
               "silence", "fam-005/safe: guard intact on both revisions"),
    "CASE-C": (str(AUTH1 / "fam-052" / "base"),
               str(AUTH1 / "fam-052" / "broken"),
               "proposal", "fam-052/broken: checked-vs-used divergence"),
    "CASE-D": (str(AUTH1 / "fam-051" / "base"),
               str(AUTH1 / "fam-051" / "broken"),
               "proposal", "fam-051/broken: checked-vs-used divergence"),
    "CASE-E": (str(AUTH1 / "fam-003" / "base"),
               str(AUTH1 / "fam-003" / "safe"),
               "silence", "fam-003/safe: relocated guard, enforcement kept"),
    "CASE-F": (str(SNAP / "ORG-A11" / "base"),
               str(SNAP / "ORG-A11" / "head"),
               "internal", "ORG-A11: deny replaced by sanitized tier"),
}

PROMPTS = Path("/data/coding/codatiel/codatiel-i2/eval/LIVE_01C_PROMPTS")
PROMPT_VERSION = "v1.1 (P-SHORT output-schema fix 2026-10-18: added " \
    "material_concern / remaining_uncertainty / recommended_action " \
    "fields omitted in v1.0; re-ran affected P-SHORT cases)"
OUT = Path("/data/coding/codatiel/codatiel-i2/eval/LIVE_01C_TRACES")


def changed_files(base: str, head: str) -> list[str]:
    h = ReviewHarness(Path(base), Path(head))
    return h.get_changed_files()


def run_one(case: str, arm: str) -> dict:
    base, head, expected, _notes = LIVE_CASES[case]
    system = (PROMPTS / f"{arm}.md").read_text()
    t0 = time.time()
    h = ReviewHarness(Path(base), Path(head), tool_budget=TOOL_BUDGET)
    files = changed_files(base, head)
    model = LiveModel(BASE_URL, MODEL, system, arm=arm,
                      temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
                      token_budget=TOKEN_BUDGET, timeout_seconds=TIMEOUT_S)
    trace = h.investigate({"case_id": case, "changed_files": files}, model)
    wall = round(time.time() - t0, 1)
    record = {"case_id": case, "arm": arm, "expected_kind": expected,
              "model": MODEL, "base_url": BASE_URL,
              "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS,
              "token_budget": TOKEN_BUDGET, "tool_budget": TOOL_BUDGET,
              "changed_files": files, "trace": trace,
              "model_calls": model.calls,
              "input_tokens": model.input_tokens,
              "output_tokens": model.output_tokens,
              "invalid_actions": model.invalid_count,
              "wall_s": wall}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{case}_{arm}.json").write_text(json.dumps(record, indent=1))
    out = trace.get("outcome", {})
    verdict = (out.get("publish_verdict") or {}).get("eligible")
    print(f"{case} {arm}: termination={trace['termination']} "
          f"eligible={verdict} proposed={bool(out.get('proposed_comment'))} "
          f"silence={bool(out.get('silence'))} "
          f"tools={len(trace['model_tool_calls'])} "
          f"in={model.input_tokens} out={model.output_tokens} "
          f"invalid={model.invalid_count} wall={wall}s", flush=True)
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="",
                    help="comma-separated CASE:ARM subset")
    args = ap.parse_args()
    if args.only:
        jobs = []
        for item in args.only.split(","):
            c, a = item.strip().split(":")
            jobs.append((c, a))
    else:
        jobs = [(c, a) for c in LIVE_CASES
                for a in ("P-SHORT", "P-GUIDEBOOK")]
    for case, arm in jobs:
        assert case in LIVE_CASES, case
        assert arm in ("P-SHORT", "P-GUIDEBOOK"), arm
    for case, arm in jobs:
        run_one(case, arm)


if __name__ == "__main__":
    main()
