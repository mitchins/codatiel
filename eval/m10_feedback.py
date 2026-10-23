"""M10 P4.3: minimal human feedback CLI (no inference, no UI).

Usage:
  m10_feedback.py rate <run_id> <finding_id> <useful|low_value|wrong> [note]
  m10_feedback.py report

Ratings (owner's judgment, distinct from automated adjudication):
  useful    correct, material, worth telling the author
  low_value correct but not worth interrupting the author
  wrong     false, unsupported, or materially misleading

Appends to $M10_ROOT/feedback.jsonl (private). `report` prints counts,
cost per useful finding (from run metrics), and the unrated backlog.
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(os.environ.get("M10_ROOT", "/tmp/m10"))
FEEDBACK = ROOT / "feedback.jsonl"
VALID = ("useful", "low_value", "wrong")


def rate(run_id, finding_id, rating, note=""):
    if rating not in VALID:
        raise SystemExit(f"rating must be one of {VALID}")
    # finding must exist in the run record (no rating ghosts).
    matches = [d for d in (ROOT / "runs").rglob("run.json")
               if json.loads(d.read_text()).get("run_id") == run_id]
    if not matches:
        raise SystemExit(f"unknown run_id {run_id}")
    d = matches[0].parent
    ids = {json.loads(line).get("finding_id")
           for line in (d / "findings.jsonl").read_text().splitlines()
           if line.strip()}
    if finding_id not in ids:
        raise SystemExit(f"finding {finding_id} not in run {run_id}")
    with FEEDBACK.open("a") as h:
        h.write(json.dumps({"run_id": run_id, "finding_id": finding_id,
                            "rating": rating, "note": note}) + "\n")
    print(f"recorded {rating} for {finding_id} in {run_id}")


def report():
    rows = ([json.loads(line) for line in FEEDBACK.read_text().splitlines()
             if line.strip()] if FEEDBACK.exists() else [])
    by_rating = Counter(r["rating"] for r in rows)
    rated_ids = {(r["run_id"], r["finding_id"]) for r in rows}
    print(json.dumps({"n_rated": len(rows),
                      "by_rating": dict(by_rating)}, indent=1))
    # backlog: findings in run records without a rating
    backlog = []
    for run_json in (ROOT / "runs").rglob("run.json"):
        d = run_json.parent
        run_id = json.loads(run_json.read_text()).get("run_id")
        fp = d / "findings.jsonl"
        if not fp.exists():
            continue
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            fid = json.loads(line).get("finding_id")
            if (run_id, fid) not in rated_ids:
                backlog.append({"run_id": run_id, "finding_id": fid})
    print(json.dumps({"n_unrated": len(backlog), "backlog": backlog[:50]},
                     indent=1))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: rate ... | report")
    if sys.argv[1] == "rate":
        rate(*sys.argv[2:5], *(sys.argv[5:6] or [""]))
    elif sys.argv[1] == "report":
        report()
    else:
        raise SystemExit("usage: rate ... | report")
