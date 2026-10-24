"""M10 cold batch: one frozen Luna-A run per F1-F6 case (no tuning).

See eval/M10_COLD_FREEZE.md (committed before any F-case execution).
Root /tmp/m10, ledger tagged batch=F-cold. SHA gate enforced by service.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/codatiel-public")  # NOSONAR - fixed source path
from codatiel_i2 import providers as P  # NOSONAR
from codatiel_i2 import service as S  # NOSONAR

ROOT = Path(os.environ.get("M10_ROOT", "/tmp/m10"))
RUNS = ROOT / "runs"
WORK = ROOT / "work"
LEDGER = ROOT / "ledger.json"
CEILING = float(os.environ.get("M10_CEILING_USD", "5.0"))
RESERVE = float(os.environ.get("M10_RESERVE_USD", "0.50"))

F_CASES = {
    "F1": {"repo": "mitchins/scanweave", "pr": 22,
           "base": "6cc38698", "head": "43bb8157"},
    "F2": {"repo": "mitchins/scanweave", "pr": 21,
           "base": "9a187132", "head": "8639a3a4"},
    "F3": {"repo": "Smart-Compliance-Systems/scs-infra", "pr": 25,
           "base": "03b52943", "head": "cd8ba31a"},
    "F4": {"repo": "Smart-Compliance-Systems/scs-infra", "pr": 24,
           "base": "a13405ad", "head": "c933bdf0"},
    "F5": {"repo": "mitchins/justcards-app", "pr": 18,
           "base": "249ac335", "head": "bb308642"},
    "F6": {"repo": "mitchins/selectivetutor-website", "pr": 60,
           "base": "a918db4c", "head": "b352e0fc"},
}


def ledger():
    return json.loads(LEDGER.read_text())  # NOSONAR - created by m10_run


def save(lg):  # NOSONAR
    LEDGER.write_text(json.dumps(lg, indent=1))  # NOSONAR


def main(case_id: str):
    case = F_CASES[case_id]
    lg = ledger()
    if lg["spent_usd"] + RESERVE >= CEILING:
        print(json.dumps({"refused": "ceiling-reserve",
                           "spent": lg["spent_usd"],
                           "ceiling": CEILING}))
        return 2
    resolved = P.resolve_backend("luna")
    model, settings = P.build_model("luna")
    from agents.model_settings import ModelSettings
    settings = ModelSettings(store=False, max_tokens=3000)
    kw = dict(model=model, model_name=f"luna:{resolved['model']}",
              runs_root=RUNS / "luna" / "A" / case_id / "rep1",
              work_root=WORK / "luna" / "A" / case_id / "rep1",
              max_turns=12, model_settings=settings,
              rate=resolved["rate_usd_per_mtok"],
              rate_status=resolved["rate_status"],
              max_usd_per_review=CEILING - lg["spent_usd"],
              variant="baseline",
              expected_base=case["base"], expected_head=case["head"],
              playbook_name=None, playbook_text=None,
              inventory_strategy="file-list-0.1")
    out = S.review_pr(case["repo"], case["pr"], **kw)
    spend = {}
    if out.get("run_dir"):
        mp = Path(out["run_dir"]) / "metrics.json"
        if mp.exists():
            spend = json.loads(mp.read_text()).get("spend", {})
    usd = spend.get("usd_estimate") or 0.0
    lg["spent_usd"] = round(lg["spent_usd"] + usd, 4)
    lg["runs"].append({"case": case_id, "model": "luna", "arm": "A",
                       "batch": "F-cold", "rep": 1,
                       "status": out.get("status"),
                       "stage": out.get("stage"),
                       "run_dir": out.get("run_dir"),
                       "tool_calls": out.get("tool_calls"),
                       "usd_estimate": usd,
                       "usage": spend.get("usage", {}),
                       "accounting_status": spend.get("accounting_status",
                                                     "unknown")})
    save(lg)
    print(json.dumps({"case": case_id, "model": "luna", "batch": "F-cold",
                       "status": out.get("status"),
                       "tools": out.get("tool_calls"), "usd": usd,
                       "spent_total": lg["spent_usd"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
