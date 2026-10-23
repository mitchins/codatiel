"""M9.2 blinding: strip arm/model/rep labels from proposed comments.

Reads run dirs (committed layout), emits:
- eval/M9_2_BLIND_ITEMS.json: opaque-ID items with case + SHAs + finding
  text + evidence IDs (no arm/model/rep/run_dir).
- eval/M9_2_BLIND_MAP.json: sealed opaque-ID -> (arm, model, rep, run_dir).

Adjudication verdicts are recorded against opaque IDs in a separate
committed file; unblinding joins on the sealed map.
"""

import json
import sys
from pathlib import Path


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/m9_2")
    out_items = Path("eval/M9_2_BLIND_ITEMS.json")
    out_map = Path("eval/M9_2_BLIND_MAP.json")
    items, mapping = [], {}
    n = 0
    for model_d in sorted((root / "runs").iterdir()):
        for arm_d in sorted(model_d.iterdir()):
            for case_d in sorted(arm_d.iterdir()):
                for rep_d in sorted(case_d.iterdir()):
                    for run_json in sorted(rep_d.rglob("run.json")):
                        d = run_json.parent
                        try:
                            run = json.loads(run_json.read_text())
                            metrics = json.loads(
                                (d / "metrics.json").read_text())
                        except FileNotFoundError:
                            continue
                        raw_p = d / "model_findings_raw.jsonl"
                        raws = []
                        if raw_p.exists():
                            raws = [json.loads(line) for line in
                                    raw_p.read_text().splitlines()
                                    if line.strip()]
                        sil = None
                        try:
                            sh = json.loads(
                                (d / "shadow_result.json").read_text())
                            sil = (sh.get("silence") or {}).get("summary")
                        except FileNotFoundError:
                            pass
                        if not raws and not sil:
                            continue
                        n += 1
                        oid = f"M92-{n:03d}"
                        items.append({
                            "id": oid,
                            "case": case_d.name,
                            "repo": run.get("repo"),
                            "base_sha": run.get("base_sha"),
                            "head_sha": run.get("head_sha"),
                            "status": metrics.get("status"),
                            "tool_calls": metrics.get("tool_calls"),
                            "silence_summary": sil,
                            "findings": raws})
                        mapping[oid] = {
                            "arm": arm_d.name, "model": model_d.name,
                            "rep": rep_d.name, "run_dir": str(d)}
    out_items.write_text(json.dumps(
        {"schema_version": "m9.2.blind.v1", "items": items}, indent=1))
    out_map.write_text(json.dumps(
        {"schema_version": "m9.2.blindmap.v1", "map": mapping}, indent=1))
    print(f"{len(items)} blind items, {len(mapping)} mapped")


if __name__ == "__main__":
    main()
