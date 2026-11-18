"""SEMDUP-SIGNAL-01 cost/classification report (read-only, no inference).

Reads run dirs (semdup.json + semdup_pairs.jsonl + rate_snapshot.json)
and emits the C1-C4 distributions. Historical semdup.json rows predate
per-pair telemetry: run-level aggregates are reported exactly, and
per-pair splits are marked unavailable rather than estimated.

Usage:
  python eval/semdup_signal01_report.py [devstate_dir]
"""

from __future__ import annotations

import glob
import json
import os
import statistics
import sys


def _load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _pct(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(pct / 100 * len(ordered)))
    return ordered[idx]


def main(devstate):
    runs = []
    for path in sorted(glob.glob(os.path.join(
            devstate, "runs", "*", "*", "semdup.json"))):
        doc = _load(path)
        if isinstance(doc, dict) and doc.get("status") == "ok":
            runs.append((path, doc))
    pair_rows = []
    for path in sorted(glob.glob(os.path.join(
            devstate, "runs", "*", "*", "semdup_pairs.jsonl"))):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        row = json.loads(line)
                        if isinstance(row, dict):
                            pair_rows.append(row)
        except Exception:
            continue

    examined = sum(int(d.get("pairs_examined") or 0) for _, d in runs)
    spend = sum(float(d.get("spend_usd") or 0.0) for _, d in runs
                if isinstance(d.get("spend_usd"), (int, float)))
    published = sum(len(d.get("published") or []) for _, d in runs)
    reasons: dict[str, int] = {}
    for _, d in runs:
        for row in d.get("suppressed") or []:
            if isinstance(row, dict):
                key = str(row.get("reason") or "unspecified")
                reasons[key] = reasons.get(key, 0) + 1
    refusals: dict[str, int] = {}
    for _, d in runs:
        summary = d.get("budget_refusals") or {}
        for status, entry in (summary.get("by_status") or {}).items():
            if isinstance(entry, dict):
                refusals[status] = (refusals.get(status, 0)
                                    + int(entry.get("count") or 0))

    print(f"runs={len(runs)} examined={examined} "
          f"spend_usd={spend:.4f} published={published}")
    if examined:
        print(f"cost_per_examined={spend / examined:.6f}")
    if published:
        print(f"cost_per_published={spend / published:.6f}")
    print(f"suppressed_by_reason={json.dumps(reasons, sort_keys=True)}")
    print(f"budget_refusals={json.dumps(refusals, sort_keys=True)} "
          f"(empty = pre-SIGNAL-01 runs without refusal states)")
    print(f"pair_telemetry_rows={len(pair_rows)}")
    if pair_rows:
        terra_n = sum(1 for r in pair_rows
                      if (r.get("terra") or {}).get("responses"))
        multi = sum(1 for r in pair_rows
                    if (r.get("terra") or {}).get("responses", 0) >= 3)
        tools = sum(1 for r in pair_rows
                    if (r.get("terra") or {}).get("tool_calls"))
        print(f"pairs_with_terra_requests={terra_n} "
              f"three_plus_requests={multi} pairs_with_tools={tools}")
        cats: dict[str, int] = {}
        for r in pair_rows:
            cls = (r.get("classification") or {})
            key = (str(cls.get("primary_category") or "unknown") + "/"
                   + str(cls.get("divergence_impact") or "unknown"))
            cats[key] = cats.get(key, 0) + 1
        print(f"classification_mix={json.dumps(cats, sort_keys=True)}")
    else:
        print("per-pair C1/C2/C3 splits unavailable: no semdup_pairs.jsonl "
              "rows recorded yet (post-merge natural traffic populates).")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else "/data/coding/codatiel-i2/semdup-08/devstate")
