"""M10 P1.2: tool-utility + cost analysis from existing M9.2 P4 traces.

No inference. Reads run dirs (default /tmp/m9_2), joins:
  tool_calls.jsonl (tool -> evidence_ids) x model_findings_raw.jsonl
  (finding -> detector, evidence_ids) x blind map + adjudication verdicts.

Emits aggregate JSON to stdout. Per-finding detail is NOT printed
(excerpts may quote private sources); write the public report separately.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

KEY_FILES = ["run.json", "metrics.json", "findings.jsonl",
             "prompt_provenance.json", "model_calls.jsonl"]


def load_jsonl(p):
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()
            if line.strip()]


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/m9_2")
    repo = Path("eval")
    bmap = json.loads((repo / "M9_2_BLIND_MAP.json").read_text())["map"]
    verdicts = json.loads(
        (repo / "M9_2_ADJUDICATION.json").read_text())["verdicts"]
    dir2oid = {v["run_dir"]: oid for oid, v in bmap.items()}

    runs = []
    tool_freq = Counter()          # tool -> total calls (executed runs)
    tool_runs = Counter()          # tool -> runs using it >=1
    ev_tool = {}                   # evidence_id -> (run_dir, tool)
    det_verdict = Counter()        # (detector, verdict) -> n findings
    tool_conf_ev = Counter()       # tool -> evidence refs cited by CONFIRMED
    tool_part_ev = Counter()       # tool -> evidence refs cited by PARTIAL
    det_conf = Counter()           # detector -> confirmed findings
    docs_cand_total = 0
    docs_cand_used = Counter()     # candidate_id -> findings.jsonl uses
    costs, costs_complete = defaultdict(list), defaultdict(list)
    walls = defaultdict(list)
    tokens = defaultdict(lambda: [0, 0])
    status_ct = Counter()
    n_executed = 0

    for run_json in sorted((root / "runs").rglob("run.json")):
        d = run_json.parent
        try:
            run = json.loads(run_json.read_text())
            metrics = json.loads((d / "metrics.json").read_text())
        except FileNotFoundError:
            continue
        if metrics.get("status") == "failed":
            continue  # billing-blocked, never executed
        n_executed += 1
        rel = str(d)
        # arm/model/rep from blind map (authoritative, sealed during P4)
        oid = dir2oid.get(rel, dir2oid.get(rel.replace("/tmp/m9_2", str(root))))
        arm = model = rep = None
        if oid:
            m = bmap[oid]
            arm, model, rep = m["arm"], m["model"], m["rep"]
        parts = d.relative_to(root / "runs").parts
        case = parts[2] if len(parts) > 2 else "?"
        if oid is None:
            # fall back to path layout runs/<model>/<arm>/<case>/<rep>/...
            model, arm, rep = parts[0], parts[1], parts[3]
        status = metrics.get("status", "?")
        status_ct[(model, arm, status)] += 1
        spend = metrics.get("spend", {})
        usd = spend.get("usd_estimate", 0) or 0
        key = f"{model}/{arm}"
        costs[key].append(usd)
        if status == "complete":
            costs_complete[key].append(usd)
        walls[key].append(metrics.get("wall_seconds", 0) or 0)
        u = spend.get("usage", {}) or {}
        tokens[key][0] += u.get("input_tokens", 0) or 0
        tokens[key][1] += u.get("output_tokens", 0) or 0

        seen_tools = set()
        for tc in load_jsonl(d / "tool_calls.jsonl"):
            t = tc.get("tool", "?")
            tool_freq[(key, t)] += 1
            seen_tools.add((key, t))
            for ev in ((tc.get("summary") or {}).get("evidence_ids") or []):
                ev_tool[(rel, ev)] = t
        for kt in seen_tools:
            tool_runs[kt] += 1

        # docs_code deterministic candidates
        for c in load_jsonl(d / "candidates" / "docs_code.jsonl"):
            docs_cand_total += 1
        for f in load_jsonl(d / "findings.jsonl"):
            cid = f.get("candidate_id")
            if cid:
                docs_cand_used[cid] += 1

        raws = load_jsonl(d / "model_findings_raw.jsonl")
        for k, raw in enumerate(raws):
            det = raw.get("detector", "?")
            v = verdicts.get(f"{oid}#{k}", {}).get("verdict", "UNVERDICTED") \
                if oid else "UNMAPPED"
            det_verdict[(det, v)] += 1
            if v == "CONFIRMED" or v == "CONFIRMED-LOW":
                det_conf[det] += 1
            for ev in raw.get("evidence_ids") or []:
                t = ev_tool.get((rel, ev), "?unmapped")
                if v in ("CONFIRMED", "CONFIRMED-LOW"):
                    tool_conf_ev[(key, t)] += 1
                elif v == "PARTIAL":
                    tool_part_ev[(key, t)] += 1

    # distinct defects vs reps (from adjudication ref_item/novel)
    defects = defaultdict(set)  # defect -> set of (model, arm, rep, case)
    for vid, v in verdicts.items():
        if v["verdict"] not in ("CONFIRMED", "CONFIRMED-LOW"):
            continue
        oid = vid.split("#")[0]
        m = bmap.get(oid)
        if not m:
            continue
        defect = v.get("ref_item") or ("novel:" + str(v.get("novel")))
        case = Path(m["run_dir"]).relative_to(root / "runs").parts[2]
        defects[defect].add((m["model"], m["arm"], case))

    import statistics as st
    out = {
        "n_executed_runs": n_executed,
        "status": {f"{m}/{a}/{s}": n for (m, a, s), n in status_ct.items()},
        "tool_calls_total": {f"{k[0]}/{k[1]}": n
                             for k, n in tool_freq.items()},
        "tool_runs_using": {f"{k[0]}/{k[1]}": n for k, n in tool_runs.items()},
        "tool_confirmed_evidence_refs":
            {f"{k[0]}/{k[1]}": n for k, n in tool_conf_ev.items()},
        "tool_partial_evidence_refs":
            {f"{k[0]}/{k[1]}": n for k, n in tool_part_ev.items()},
        "detector_x_verdict":
            {f"{d}/{v}": n for (d, v), n in det_verdict.items()},
        "docs_code_candidates_total": docs_cand_total,
        "docs_code_candidates_used_in_findings": len(docs_cand_used),
        "cost_per_run_mean": {k: round(st.mean(v), 4)
                              for k, v in costs.items()},
        "cost_per_complete_median": {k: round(st.median(v), 4)
                                     for k, v in costs_complete.items()
                                     if v},
        "wall_seconds_median": {k: round(st.median(v), 1)
                                for k, v in walls.items() if v},
        "tokens_in_out": {k: v for k, v in tokens.items()},
        "distinct_confirmed_defects": len(defects),
        "confirmed_defect_reps": sum(len(s) for s in defects.values()),
        "defect_rep_counts": {d: len(s) for d, s in sorted(
            defects.items(), key=lambda x: -len(x[1]))},
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
