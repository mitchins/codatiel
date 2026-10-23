"""M9.2 ledger: regenerate eval/M9_2_LEDGER.json from run artifacts.

Walks <root>/runs/<model>/<arm>/<case>/rep<N>/..., reads each run.json,
metrics.json, prompt_provenance.json, and model_findings_raw.jsonl, and
recomputes every number. A test fails on any difference between this output
and the committed ledger. Unknown usage stays UNKNOWN (never $0 except
local zero-cost backends); incompletes keep their partial spend.
"""

import json
import sys
from pathlib import Path

SCHEMA = "m9.2.ledger.v1"


def iter_runs(root: Path):
    for model_d in sorted((root / "runs").iterdir()) if (root / "runs").exists() else []:
        for arm_d in sorted(model_d.iterdir()):
            for case_d in sorted(arm_d.iterdir()):
                for rep_d in sorted(case_d.iterdir()):
                    for run_dir in sorted(rep_d.rglob("run.json")):
                        yield (model_d.name, arm_d.name, case_d.name,
                               rep_d.name, run_dir.parent)


def build(root: Path):
    runs, excluded = [], []
    for model, arm, case, rep, d in iter_runs(root):
        try:
            run = json.loads((d / "run.json").read_text())
            metrics = json.loads((d / "metrics.json").read_text())
        except FileNotFoundError:
            excluded.append({"dir": str(d), "reason": "missing run.json/metrics.json"})
            continue
        try:
            prov = json.loads((d / "prompt_provenance.json").read_text())
        except FileNotFoundError:
            prov = {}
        raw_p = d / "model_findings_raw.jsonl"
        n_raw = 0
        if raw_p.exists():
            n_raw = sum(1 for line in raw_p.read_text().splitlines()
                        if line.strip())
        spend = metrics.get("spend", {})
        cfg = run.get("config", {})
        rec = {"model": model, "arm": arm, "case": case, "rep": rep,
               "dir": str(d), "run_id": run.get("run_id"),
               "repo": run.get("repo"),
               "base_sha": run.get("base_sha"), "head_sha": run.get("head_sha"),
               "status": metrics.get("status", run.get("status")),
               "stage": run.get("stage"),
               "tool_calls": metrics.get("tool_calls"),
               "usd_estimate": spend.get("usd_estimate", 0.0),
               "accounting_status": spend.get("accounting_status", "UNKNOWN"),
               "instructions_id": prov.get("instructions_id")
               or cfg.get("instructions_id"),
               "inventory_strategy": prov.get("inventory_strategy")
               or cfg.get("inventory_strategy"),
               "boundary_match": prov.get("boundary_match"),
               "n_model_findings": n_raw}
        if run.get("stage") == "sha_mismatch":
            excluded.append({"dir": str(d), "reason": "sha_mismatch",
                             "record": rec})
            continue
        runs.append(rec)
    agg = {}
    for r in runs:
        key = (r["model"], r["arm"])
        a = agg.setdefault(r["arm"] + "/" + r["model"],
                           {"model": r["model"], "arm": r["arm"], "n_runs": 0,
                            "n_complete": 0, "n_incomplete": 0,
                            "n_unresolved": 0, "tool_calls": 0,
                            "usd": 0.0, "n_findings": 0,
                            "unknown_spend_runs": 0})
        a["n_runs"] += 1
        st = r["status"] or ""
        if st == "complete":
            a["n_complete"] += 1
        elif st.startswith("incomplete"):
            a["n_incomplete"] += 1
        elif st.startswith("unresolved"):
            a["n_unresolved"] += 1
        else:
            a.setdefault("n_other", 0)
            a["n_other"] += 1
        a["tool_calls"] += r["tool_calls"] or 0
        # §9: unknown stays unknown; only confirmed spend aggregates as $.
        if r["accounting_status"] not in ("CONFIRMED_USAGE",
                                          "CONFIRMED_ZERO_USAGE"):
            a["unknown_spend_runs"] += 1
        else:
            a["usd"] += r["usd_estimate"] or 0.0
        a["n_findings"] += r["n_model_findings"] or 0
    for a in agg.values():
        a["usd"] = round(a["usd"], 4)
    return {"schema_version": SCHEMA, "runs": runs, "excluded": excluded,
            "aggregate": agg}


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/m9_2")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    ledger = build(root)
    text = json.dumps(ledger, indent=1, sort_keys=True)
    if out:
        out.write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
