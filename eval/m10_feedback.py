"""M10 P4.3: minimal human feedback CLI (no inference, no UI).

Usage:
  m10_feedback.py rate <run_id> <#k|finding_id> <useful|low_value|wrong> [note]
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

ROOT = Path(os.environ.get("M10_ROOT", "/tmp/m10"))  # NOSONAR - research-only eval script, never shipped; env-overridable scratch dir
FEEDBACK = ROOT / "feedback.jsonl"
VALID = ("useful", "low_value", "wrong")


def _run_dir(run_id):
    matches = [d for d in (ROOT / "runs").rglob("run.json")
               if json.loads(d.read_text()).get("run_id") == run_id]
    if not matches:
        raise SystemExit(f"unknown run_id {run_id}")
    return matches[0].parent


def _verified_ids(d):
    return {json.loads(line).get("finding_id")
            for line in (d / "findings.jsonl").read_text().splitlines()
            if line.strip()}


def _submitted(d):
    fp = d / "model_findings_raw.jsonl"
    if not fp.exists():
        return []
    return [json.loads(line) for line in fp.read_text().splitlines()
            if line.strip()]


def _identity_for_ref(d, finding_ref):
    """Resolve a user-supplied reference to a stable finding identity.

    Accepts a verified `finding_xxx` ID (validated against the run
    record: no rating ghosts) or a submitted `#k` index, 1-based as
    shown in the digest. Indices are validated BEFORE indexing: `#0`,
    negatives, non-numerics and out-of-range values are rejected instead
    of silently resolving (Python negative indexing would otherwise map
    `#0` onto the last finding).
    """
    ids = _verified_ids(d)
    raws = _submitted(d)
    if finding_ref.startswith("#"):
        try:
            k = int(finding_ref[1:])
        except ValueError:
            raise SystemExit(f"no submitted finding {finding_ref} "
                             f"(not a 1-based index)")
        if not 1 <= k <= len(raws):
            raise SystemExit(f"no submitted finding {finding_ref}: "
                             f"run has {len(raws)} submitted findings "
                             f"(valid: #1..#{len(raws)})")
        raw = raws[k - 1]
        return f"submitted#{k - 1}:" + str(raw.get("title", "?"))[:80]
    if finding_ref in ids:
        return finding_ref
    raise SystemExit(f"finding {finding_ref} not in run "
                     f"(need a verified finding ID or #1..#{len(raws)})")


def _canonical_key(run_id, finding_id):
    """Stable bookkeeping identity for one rating.

    Submitted references keep the run's own `submitted#k:title` scheme so
    existing history rows remain comparable; verified IDs are used as-is.
    """
    return (run_id, finding_id)


def rate(run_id, finding_ref, rating, note=""):
    if rating not in VALID:
        raise SystemExit(f"rating must be one of {VALID}")
    d = _run_dir(run_id)
    finding_id = _identity_for_ref(d, finding_ref)
    with FEEDBACK.open("a") as h:
        h.write(json.dumps({"run_id": run_id, "finding_id": finding_id,
                            "rating": rating, "note": note}) + "\n")
    print(f"recorded {rating} for {finding_id} in {run_id}")


def report():
    rows = ([json.loads(line) for line in FEEDBACK.read_text().splitlines()
             if line.strip()] if FEEDBACK.exists() else [])
    # Bookkeeping is by DISTINCT finding, keyed on the stable identity.
    # History rows are preserved verbatim (every rating event stays in
    # feedback.jsonl); a repeated rating of the same finding UPDATES the
    # owner's judgment (latest wins) instead of counting a second vote.
    # Backlog completion is therefore computed from distinct rated
    # findings, never from raw rating-event counts.
    latest: dict[tuple, dict] = {}
    for r in rows:
        key = _canonical_key(r.get("run_id"), r.get("finding_id"))
        latest[key] = r
    by_rating = Counter(r["rating"] for r in latest.values()
                        if r.get("rating") in VALID)
    print(json.dumps({"n_rating_events": len(rows),
                      "n_rated_findings": len(latest),
                      "by_rating": dict(by_rating)}, indent=1))
    # backlog: submitted findings in run records without a rating.
    # A submitted finding counts as rated when ANY history row resolves
    # to its identity — including verified-ID rows for findings that
    # were submitted but never correspond to a verified candidate.
    rated_identities = {(r.get("run_id"), r.get("finding_id"))
                        for r in rows}
    backlog = []
    for run_json in (ROOT / "runs").rglob("run.json"):
        d = run_json.parent
        run_id = json.loads(run_json.read_text()).get("run_id")
        raws = _submitted(d)
        if not raws and not (d / "findings.jsonl").exists():
            continue
        unrated = []
        for i, raw in enumerate(raws):
            ident = f"submitted#{i}:" + str(raw.get("title", "?"))[:80]
            if (run_id, ident) not in rated_identities:
                unrated.append(f"#{i + 1}")
        if unrated:
            backlog.append({"run_id": run_id,
                            "submitted": len(raws),
                            "rated": len(raws) - len(unrated),
                            "unrated": unrated,
                            "case": d.parts[-4] if len(d.parts) > 4 else "?"})
    print(json.dumps({"n_runs_with_unrated": len(backlog),
                      "backlog": backlog}, indent=1))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: rate ... | report")
    if sys.argv[1] == "rate":
        rate(*sys.argv[2:5], *(sys.argv[5:6] or [""]))
    elif sys.argv[1] == "report":
        report()
    else:
        raise SystemExit("usage: rate ... | report")
