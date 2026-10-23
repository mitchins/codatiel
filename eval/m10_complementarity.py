"""M10 P1.3: cheap-model complementarity — union vs repeat.

Declared pairing (no post-hoc best-rep selection):
  single(M, case)      = mean over reps 1..3 of confirmed defects
  cross(case)          = mean over k=1..3 of |Luna repk UNION Haiku repk|
  repeat(M, case)      = mean over the 3 unordered rep pairs of |union|
Compares one ordinary Luna run + one ordinary Haiku run against two
ordinary same-model runs. Arm A (retained minimal prompt) only.
Defects deduplicated by adjudicated label (ref_item/novel, normalized).
CONFIRMED primary; CONFIRMED-LOW reported separately, included in union.
"""

import json
import statistics as st
import sys
from itertools import combinations
from pathlib import Path

NORMALIZE = {"novel:L06-readme-sub": "L06-readme-sub"}


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/m9_2")
    bmap = json.loads(
        Path("eval/M9_2_BLIND_MAP.json").read_text())["map"]
    verdicts = json.loads(
        Path("eval/M9_2_ADJUDICATION.json").read_text())["verdicts"]

    # Enumerate arm-A run dirs from the generated ledger (blind map omits
    # finding-less runs; those are zero-finding cells, not missing data).
    ledger = json.loads(
        Path("eval/M9_2_LEDGER.json").read_text())["runs"]
    dir2oid = {}
    for oid, m in bmap.items():
        dir2oid[m["run_dir"]] = oid
    # (model, case, rep) -> {confirmed:set, low:set, partial:int,
    #                        status:str, usd:float}
    cell = {}
    for r in ledger:
        if r.get("arm") != "A" or r.get("model") not in ("luna", "haiku"):
            continue
        rep_raw = str(r.get("rep"))
        rep_n = int(rep_raw.replace("rep", ""))
        if rep_n not in (1, 2, 3):
            continue
        d = Path(r["dir"])
        try:
            metrics = json.loads((d / "metrics.json").read_text())
        except FileNotFoundError:
            continue
        if metrics.get("status") == "failed":
            continue
        parts = d.relative_to(root / "runs").parts
        model, case, rep = parts[0], parts[2], f"rep{rep_n}"
        key = (model, case, rep)
        oid = dir2oid.get(str(d))
        c = cell.setdefault(key, {"confirmed": set(), "low": set(),
                                  "partial": 0, "status": metrics.get(
                                      "status"),
                                  "usd": (metrics.get("spend") or {}).get(
                                      "usd_estimate", 0) or 0})
        raws = (d / "model_findings_raw.jsonl")
        n_raw = sum(1 for line in raws.read_text().splitlines()
                    if line.strip()) if raws.exists() else 0
        for k in range(n_raw):
            v = verdicts.get(f"{oid}#{k}", {}) if oid else {}
            lab = v.get("ref_item") or ("novel:" + str(v.get("novel")))
            lab = NORMALIZE.get(lab, lab)
            if v.get("verdict") == "CONFIRMED":
                c["confirmed"].add(lab)
            elif v.get("verdict") == "CONFIRMED-LOW":
                c["low"].add(lab)
            elif v.get("verdict") == "PARTIAL":
                c["partial"] += 1

    cases = sorted({k[1] for k in cell})
    rows = []
    for case in cases:
        L = [cell.get(("luna", case, f"rep{k}")) for k in (1, 2, 3)]
        H = [cell.get(("haiku", case, f"rep{k}")) for k in (1, 2, 3)]
        L = [c for c in L if c]
        H = [c for c in H if c]
        if len(L) < 3 or len(H) < 3:
            print(f"SKIP {case}: luna={len(L)} haiku={len(H)}",
                  file=sys.stderr)
            continue
        sL = [len(c["confirmed"]) for c in L]
        sH = [len(c["confirmed"]) for c in H]
        cross = [len((L[k]["confirmed"] | L[k]["low"] |
                       H[k]["confirmed"] | H[k]["low"])) for k in range(3)]
        repL = [len((a["confirmed"] | a["low"] |
                      b["confirmed"] | b["low"]))
                for a, b in combinations(L, 2)]
        repH = [len((a["confirmed"] | a["low"] |
                      b["confirmed"] | b["low"]))
                for a, b in combinations(H, 2)]
        # marginal gain of 2nd run: union - single, averaged
        marg_cross = [cross[k] - len(L[k]["confirmed"] | L[k]["low"])
                      for k in range(3)]
        marg_repL = [(len((a["confirmed"] | a["low"] | b["confirmed"] |
                            b["low"])) - len(a["confirmed"] | a["low"]))
                     for a, b in combinations(L, 2)]
        marg_repH = [(len((a["confirmed"] | a["low"] | b["confirmed"] |
                            b["low"])) - len(a["confirmed"] | a["low"]))
                     for a, b in combinations(H, 2)]
        cost_cross = st.mean([L[k]["usd"] + H[k]["usd"] for k in range(3)])
        cost_repL = st.mean([a["usd"] + b["usd"]
                             for a, b in combinations(L, 2)])
        cost_repH = st.mean([a["usd"] + b["usd"]
                             for a, b in combinations(H, 2)])
        comp_cross = sum(1 for k in range(3)
                         if L[k]["status"] == "complete"
                         and H[k]["status"] == "complete")
        rows.append({
            "case": case,
            "single_luna": round(st.mean(sL), 2),
            "single_haiku": round(st.mean(sH), 2),
            "cross_union": round(st.mean(cross), 2),
            "repeat_luna": round(st.mean(repL), 2),
            "repeat_haiku": round(st.mean(repH), 2),
            "marginal_2nd_cross": round(st.mean(marg_cross), 2),
            "marginal_2nd_luna": round(st.mean(marg_repL), 2),
            "marginal_2nd_haiku": round(st.mean(marg_repH), 2),
            "usd_cross": round(cost_cross, 4),
            "usd_repeat_luna": round(cost_repL, 4),
            "usd_repeat_haiku": round(cost_repH, 4),
            "pairs_both_complete_cross": f"{comp_cross}/3",
            "partial_low_value_notes": sum(
                c["partial"] for c in L + H),
        })
    agg = {}
    for k in ["single_luna", "single_haiku", "cross_union", "repeat_luna",
              "repeat_haiku", "marginal_2nd_cross", "marginal_2nd_luna",
              "marginal_2nd_haiku", "usd_cross", "usd_repeat_luna",
              "usd_repeat_haiku"]:
        agg[k] = round(st.mean(r[k] for r in rows), 3)
    print(json.dumps({"per_case": rows, "mean_over_cases": agg}, indent=1))


if __name__ == "__main__":
    main()
