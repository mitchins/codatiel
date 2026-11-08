"""M9 paired-review runner: baseline vs challenger, same SDK path.

Enforces per-run ceiling, no retries, no static fallback. Supports variant
selection so baseline and challenger can be compared on identical PR revisions
with identical model and ceilings, only instructions differing.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/codatiel-public")  # NOSONAR - fixed source path for M9 evaluation, not user input
from codatiel_i2 import providers as P  # NOSONAR
from codatiel_i2 import service as S  # NOSONAR

ROSTER = json.load(open("/tmp/codatiel-public/eval/M9_ROSTER.json"))  # NOSONAR - fixed evaluation roster path, not user input
RUNS = Path("/tmp/m9/runs")  # NOSONAR - fixed private temp dir for M9, not publicly writable in CI context
WORK = Path("/tmp/m9/work")  # NOSONAR
LEDGER = Path("/tmp/m9/ledger.json")  # NOSONAR
CEILING = float(os.environ.get("M9_CEILING_USD", "5.0"))


def ledger():  # NOSONAR - fixed ledger path, not user input
    if not LEDGER.exists():  # NOSONAR
        LEDGER.parent.mkdir(parents=True, exist_ok=True)  # NOSONAR
        LEDGER.write_text(json.dumps({"ceiling_usd": CEILING, "spent_usd": 0.0, "runs": []}))  # NOSONAR
    return json.loads(LEDGER.read_text())  # NOSONAR


def save(lg):  # NOSONAR
    LEDGER.write_text(json.dumps(lg, indent=1))  # NOSONAR


def main(case_id: str, model_name: str, variant: str = "baseline"):
    case = next(c for c in ROSTER["cases"] if c["id"] == case_id)
    lg = ledger()
    if lg["spent_usd"] >= CEILING:
        print("CEILING EXHAUSTED — refusing")
        return 2
    resolved = P.resolve_backend(model_name)
    model, settings = P.build_model(model_name)
    if model_name == "luna":
        from agents.model_settings import ModelSettings
        settings = ModelSettings(store=False, max_tokens=3000)
    kw = dict(model=model, model_name=f"{model_name}:{resolved['model']}",
              runs_root=RUNS / variant, work_root=WORK / variant,
              max_turns=int(os.environ.get("M9_TURNS", "12")),
              model_settings=settings, rate=resolved["rate_usd_per_mtok"],
              rate_status=resolved["rate_status"],
              max_usd_per_review=CEILING - lg["spent_usd"],
              variant=variant)
    if case["mode"] == "pr":
        # Frozen roster SHAs are authoritative: abort on live-PR drift instead
        # of silently reviewing the wrong revision (M9.1 Phase A3).
        out = S.review_pr(case["repo"], case["pr"], expected_base=case.get("base"),
                           expected_head=case.get("head"), **kw)
        if out.get("stage") == "sha_mismatch":
            print(json.dumps({"case": case_id, "error": "sha_mismatch",
                              "detail": out.get("error")}, indent=1))
            save(lg)
            return 2
    else:
        out = S.review_local(case["local_path"], case["base"], case["head"], **kw)
    spend = {}
    if out.get("run_dir"):
        mp = Path(out["run_dir"]) / "metrics.json"
        if mp.exists():
            spend = json.loads(mp.read_text()).get("spend", {})
    usd = spend.get("usd_estimate") or 0.0
    lg["spent_usd"] = round(lg["spent_usd"] + usd, 4)
    lg["runs"].append({"case": case_id, "model": model_name, "variant": variant,
                       "status": out.get("status"), "run_dir": out.get("run_dir"),
                       "tool_calls": out.get("tool_calls"), "usd_estimate": usd,
                       "usage": spend.get("usage", {}),
                       "accounting_status": spend.get("accounting_status", "unknown")})
    save(lg)
    print(json.dumps({"case": case_id, "model": model_name, "variant": variant,
                      "status": out.get("status"), "tools": out.get("tool_calls"),
                      "usd": usd, "usage": spend.get("usage", {}),
                      "spent_total": lg["spent_usd"]}, indent=1))
    return 0 if out.get("status") in ("complete", "incomplete_budget_exhausted", "unresolved_empty_final", "unresolved_unparseable") else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: m9_run.py <case_id> <model_name> [baseline|challenger]")
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "baseline"))
