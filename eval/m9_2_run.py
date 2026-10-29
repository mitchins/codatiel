"""M9.2 P4 runner: one (case, arm, rep) per invocation.

Arms: A = minimal + file-list; B = guidebook-port + file-list;
C = guidebook-port + triage-0.1. Identical max_turns/tool_budget/settings/
revisions/SHA gate. Ceiling enforced pre-run with a reserve; no retries.
Appends one record per run to the durable ledger; the published ledger is
regenerated from artifacts by eval/m9_2_ledger.py (never hand-written).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/codatiel-public")  # NOSONAR - fixed source path
from codatiel_i2 import providers as P  # NOSONAR
from codatiel_i2 import service as S  # NOSONAR

CASES = {
    "FW3": {"repo": "mitchins/frigate-watchdog", "pr": 3,
            "base": "956f7445fa13e93309dc5809395cd316ae4d7403",
            "head": "d9b61df6c889a28b0cb49547979897533b873ba4"},
    "KL1": {"repo": "mitchins/kinglet-lms", "pr": 1,
            "base": "dbf06e0004a44570dc253e88fffd7433d95997c8",
            "head": "694269d572bd77791f50dfd61d2ec52323da9c47"},
    "L08": {"repo": "Smart-Compliance-Systems/scs-infra", "pr": 4,
            "base": "aa807b86fda364ef0723b536672e07cca4646c6e",
            "head": "aff2a0680940964ce0d7304a2699f0415893a4b4"},
    "L06": {"repo": "My-Day-My-Way/my-day-my-way-ios", "pr": 11,
            "base": "6509400ca318ba90d868e3cfa7fe7a5f2571bb86",
            "head": "4051ff05090a76165f1a533675086e5760b97cbd"},
    "L10": {"repo": "mitchins/needletail-harness", "pr": 4,
            "base": "08847b4cd2691c127597218dfd31457bf246145f",
            "head": "80bf493d1cae6d9d40fe70894ee294be80292af2"},
    "WT4": {"repo": "mitchins/wall-tidbits", "pr": 4,
            "base": "9333622385f7139ae2eddeb4a1efaec42c59f1aa",
            "head": "f053c2f89fd1f1acd509b0962e83ab211cbb4f71"},
}

ARMS = {
    "A": {"playbook_name": None, "playbook_text": None,
          "inventory_strategy": "file-list-0.1"},
    "B": {"playbook_name": "guidebook-port-0.1",
          "playbook_text": "__GUIDEBOOK__",
          "inventory_strategy": "file-list-0.1"},
    "C": {"playbook_name": "guidebook-port-0.1",
          "playbook_text": "__GUIDEBOOK__",
          "inventory_strategy": "triage-0.1"},
}

ROOT = Path(os.environ.get("M9_2_ROOT", "/tmp/m9_2"))
RUNS = ROOT / "runs"
WORK = ROOT / "work"
LEDGER = ROOT / "ledger.json"
CEILING = float(os.environ.get("M9_2_CEILING_USD", "6.50"))
RESERVE = float(os.environ.get("M9_2_RESERVE_USD", "0.20"))


def ledger():
    if not LEDGER.exists():  # NOSONAR
        LEDGER.parent.mkdir(parents=True, exist_ok=True)  # NOSONAR
        LEDGER.write_text(json.dumps({"ceiling_usd": CEILING, "spent_usd": 0.0,  # NOSONAR
                                      "runs": []}))
    return json.loads(LEDGER.read_text())  # NOSONAR


def save(lg):  # NOSONAR
    LEDGER.write_text(json.dumps(lg, indent=1))  # NOSONAR


def main(case_id: str, model_name: str, arm: str, rep: int):
    case = CASES[case_id]
    spec = dict(ARMS[arm])
    if spec["playbook_text"] == "__GUIDEBOOK__":
        spec["playbook_text"] = S.GUIDEBOOK_PORT_INSTRUCTIONS
    lg = ledger()
    if lg["spent_usd"] + RESERVE >= CEILING:
        print(json.dumps({"refused": "ceiling-reserve",
                           "spent": lg["spent_usd"],
                           "ceiling": CEILING}))
        return 2
    resolved = P.resolve_backend(model_name)
    model, settings = P.build_model(model_name)
    if model_name == "luna":
        from agents.model_settings import ModelSettings
        settings = ModelSettings(store=False, max_tokens=3000)
    kw = dict(model=model, model_name=f"{model_name}:{resolved['model']}",
              runs_root=RUNS / model_name / arm / case_id / f"rep{rep}",
              work_root=WORK / model_name / arm / case_id / f"rep{rep}",
              max_turns=12, model_settings=settings,
              rate=resolved["rate_usd_per_mtok"],
              rate_status=resolved["rate_status"],
              max_usd_per_review=CEILING - lg["spent_usd"],
              variant="baseline",
              expected_base=case["base"], expected_head=case["head"],
              playbook_name=spec["playbook_name"],
              playbook_text=spec["playbook_text"],
              inventory_strategy=spec["inventory_strategy"])
    out = S.review_pr(case["repo"], case["pr"], **kw)
    spend = {}
    if out.get("run_dir"):
        mp = Path(out["run_dir"]) / "metrics.json"
        if mp.exists():
            spend = json.loads(mp.read_text()).get("spend", {})
    usd = spend.get("usd_estimate") or 0.0
    # §9: unknown usage stays UNKNOWN in the record, but ceiling debit is
    # conservative: any run where inference actually occurred debits its
    # (possibly estimated) spend; only pre-response infra failures
    # (billing-blocked, auth, network — no model response possible) debit
    # $0. Incompletes keep their partial spend.
    err = out.get("error") or ""
    pre_response_infra = (out.get("stage") == "agent" and any(
        s in err for s in ("credit balance", "authentication",
                           "PermissionDenied", "ConnectionError",
                           "Timeout", "timed out", "APIConnectionError")))
    if pre_response_infra:
        usd = 0.0
    lg["spent_usd"] = round(lg["spent_usd"] + usd, 4)
    lg["runs"].append({"case": case_id, "model": model_name, "arm": arm,
                       "rep": rep, "status": out.get("status"),
                       "stage": out.get("stage"),
                       "run_dir": out.get("run_dir"),
                       "tool_calls": out.get("tool_calls"),
                       "usd_estimate": usd,
                       "usage": spend.get("usage", {}),
                       "accounting_status": spend.get("accounting_status",
                                                     "unknown")})
    save(lg)
    print(json.dumps({"case": case_id, "model": model_name, "arm": arm,
                       "rep": rep, "status": out.get("status"),
                       "tools": out.get("tool_calls"), "usd": usd,
                       "spent_total": lg["spent_usd"]}, indent=1))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: m9_2_run.py <CASE> <luna|haiku> <A|B|C> <rep>")
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])))
