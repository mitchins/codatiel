"""SEMDUP-SIGNAL-01 cost/classification report (read-only, no inference).

Reads run dirs (semdup.json + semdup_pairs.jsonl + rate_snapshot.json)
and emits the C1-C4 distributions. Historical semdup.json rows predate
per-pair telemetry: run-level aggregates are reported exactly, and
per-pair splits are marked unavailable rather than estimated.
rate_snapshot.json files contribute the pricing-identity mix
(post-SIGNAL-01 runs only).

Run layouts: both <state>/runs/<base>/<head>/ (devstate) and
<state>/semdup/runs/<slug>/<pr>/<head12>/ (worker) are scanned.

Usage:
  python eval/semdup_signal01_report.py [devstate_dir]
"""

from __future__ import annotations

import glob
import json
import os
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
    semdup_paths = sorted(glob.glob(os.path.join(
        devstate, "runs", "*", "*", "semdup.json")))
    semdup_paths += sorted(glob.glob(os.path.join(
        devstate, "semdup", "runs", "*", "*", "*", "semdup.json")))
    pair_paths = sorted(glob.glob(os.path.join(
        devstate, "runs", "*", "*", "semdup_pairs.jsonl")))
    pair_paths += sorted(glob.glob(os.path.join(
        devstate, "semdup", "runs", "*", "*", "*", "semdup_pairs.jsonl")))
    snap_paths = sorted(glob.glob(os.path.join(
        devstate, "runs", "*", "*", "rate_snapshot.json")))
    snap_paths += sorted(glob.glob(os.path.join(
        devstate, "semdup", "runs", "*", "*", "*", "rate_snapshot.json")))
    runs = []
    for path in semdup_paths:
        doc = _load(path)
        if isinstance(doc, dict) and doc.get("status") == "ok":
            runs.append((path, doc))
    # Dedup by pair_id, keep last: re-drives into the same run dir
    # append follow-up rows for re-examined pairs.
    pair_by_id: dict[str, dict] = {}
    for path in pair_paths:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        row = json.loads(line)
                        if isinstance(row, dict) and row.get("pair_id"):
                            pair_by_id[str(row["pair_id"])] = row
        except Exception:
            continue
    pair_rows = sorted(pair_by_id.values(),
                       key=lambda r: str(r.get("pair_id")))
    rate_mix: dict[str, int] = {}
    for path in snap_paths:
        snap = _load(path)
        if not isinstance(snap, dict):
            continue
        for side in ("terra", "luna"):
            info = snap.get(side) or {}
            key = (f"{side}:{info.get('model')}:"
                   f"{info.get('rate_status')}")
            rate_mix[key] = rate_mix.get(key, 0) + 1

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
    per_run_cost = [float(d.get("spend_usd") or 0.0)
                    / max(1, int(d.get("pairs_examined") or 0))
                    for _, d in runs
                    if isinstance(d.get("spend_usd"), (int, float))]
    if per_run_cost:
        print(f"per_run_cost_per_examined_p50={_pct(per_run_cost, 50):.6f} "
              f"p95={_pct(per_run_cost, 95):.6f}")
    if rate_mix:
        print(f"rate_snapshot_mix={json.dumps(rate_mix, sort_keys=True)}")
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
