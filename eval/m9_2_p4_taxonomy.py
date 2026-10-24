"""M9.2 P4 taxonomy shift: recompute P2 classes on P4 Luna traces.

Reuses eval/m9_2_taxonomy.py (same REF set, same deterministic rules).
Reads run dirs under $M9_2_ROOT/runs/luna (default /tmp/m9_2).
Output: eval/M9_2_P4_TAXONOMY.json.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m9_2_taxonomy as T


def main():
    root = Path(os.environ.get("M9_2_ROOT", "/tmp/m9_2"))
    runs = []
    for rd in sorted((root / "runs" / "luna").rglob("run.json")):
        try:
            r = json.loads(rd.read_text())
            m = json.loads((rd.parent / "metrics.json").read_text())
        except FileNotFoundError:
            continue
        runs.append({"trace_id": "P4-luna/" + str(rd.parent).split("/rep")[0].split("/")[-3] + "/" + rd.parent.parent.name + "/" + rd.parent.name,
                     "dir": rd.parent,
                     "repo": T._normalize_repo(r.get("repo")),
                     "status": m.get("status"), "variant": None,
                     "model": "luna", "tool_calls": m.get("tool_calls"),
                     "shas": [(r.get("base_sha") or "")[:12],
                              (r.get("head_sha") or "")[:12]]})
    rows, false_rows = [], []
    for run in runs:
        for item in T.REF:
            if run.get("repo") != item["repo"]:
                continue
            rows.append(T.classify(run, item))
    found_ids = {(row["trace"], t) for row in rows
                 if row["class"] == "FOUND" for t in row.get("finding_titles", [])}
    for run in runs:
        for f in T.model_findings(run["dir"]):
            if (run["trace_id"], f.get("title")) in found_ids:
                continue
            cls, why = T.classify_false_finding(f, T.REF)
            false_rows.append({"trace": run["trace_id"],
                               "title": f.get("title"),
                               "path": f.get("path"),
                               "class": cls, "judgement": why})
    Path("/tmp/codatiel-public/eval/M9_2_P4_TAXONOMY.json").write_text(
        json.dumps({"schema_version": "m9.2.p4tax.v1", "rows": rows,
                    "false_findings": false_rows}, indent=1, sort_keys=True))
    from collections import Counter
    print(Counter(r["class"] for r in rows), len(rows))


if __name__ == "__main__":
    main()
