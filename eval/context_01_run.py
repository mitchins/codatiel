"""CONTEXT-01 pilot runner: 7 frozen heads × 3 arms (A/B/C) on luna.

Arms:
  A = file-list-0.1 (baseline, no bundle)
  B = bundle-0.1 (prepared change bundle, Feature A)
  C = bundle-cite-0.1 (bundle + citable diff lines, Feature B)

Identical model, prompt, tool budget (40), finalisation policy.
Ceiling $8 total with pre-dispatch reserve; isolated runs (no GitHub
posting, no email, no unpausing). Exercises the deployed image's
model adapters and accounting code (cache, tiers, PARTIAL checks).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/codatiel-public")
from codatiel_i2 import providers as P
from codatiel_i2 import service as S

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
    "P15": {"repo": "mitchins/yt2wiki", "pr": 15,
            "base": "c45c2ec6e43faa43a136ce49ed16197aaf6a144b",
            "head": "58c26e20e0fc4c764fd224b3a562ddf08e38be7a"},
}

ARMS = {
    "A": {"inventory_strategy": "file-list-0.1"},
    "B": {"inventory_strategy": "bundle-0.1"},
    "C": {"inventory_strategy": "bundle-cite-0.1"},
}

ROOT = Path(os.environ.get("CONTEXT_01_ROOT", "/tmp/qual"))
RUNS = ROOT / "runs"
WORK = ROOT / "work"
LEDGER = ROOT / "ledger.json"
CEILING = float(os.environ.get("CONTEXT_01_CEILING_USD", "8.00"))
RESERVE = float(os.environ.get("CONTEXT_01_RESERVE_USD", "0.20"))


def load_ledger():
    if not LEDGER.exists():
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps({"ceiling_usd": CEILING, "spent_usd": 0.0, "runs": []}))
    return json.loads(LEDGER.read_text())


def save_ledger(lg):
    LEDGER.write_text(json.dumps(lg, indent=1))


def main(case_id: str, model_name: str, arm: str, rep: int):
    case = CASES[case_id]
    spec = dict(ARMS[arm])
    lg = load_ledger()
    if lg["spent_usd"] + RESERVE >= CEILING:
        print(json.dumps({"refused": "ceiling-reserve", "spent": lg["spent_usd"], "ceiling": CEILING}))
        return 2
    resolved = P.resolve_backend(model_name)
    model, settings = P.build_model(model_name)
    if model_name == "luna":
        from agents.model_settings import ModelSettings
        settings = ModelSettings(store=False, max_tokens=3000)
    common_kw = dict(model=model, model_name=f"{model_name}:{resolved['model']}",
                       runs_root=RUNS / model_name / arm / case_id / f"rep{rep}",
                       work_root=WORK / model_name / arm / case_id / f"rep{rep}",
                       max_turns=12, model_settings=settings,
                       rate=resolved["rate_usd_per_mtok"],
                       rate_status=resolved["rate_status"],
                       max_usd_per_review=CEILING - lg["spent_usd"],
                       variant="baseline",
                       inventory_strategy=spec["inventory_strategy"])
    # P15: frozen head no longer matches live PR; use local repo.
    if case_id == "P15":
        repo_root = Path(os.environ.get("YT2WIKI_REPO", "/tmp/yt2wiki"))
        out = S.review_local(repo_root, case["base"], case["head"], **common_kw)
    else:
        kw = dict(common_kw, expected_base=case["base"],
                  expected_head=case["head"])
        out = S.review_pr(case["repo"], case["pr"], **kw)
    spend = {}
    if out.get("run_dir"):
        mp = Path(out["run_dir"]) / "metrics.json"
        if mp.exists():
            spend = json.loads(mp.read_text()).get("spend", {})
    usd = spend.get("usd_estimate") or 0.0
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
                       "accounting_status": spend.get("accounting_status", "unknown")})
    save_ledger(lg)
    print(json.dumps({"case": case_id, "model": model_name, "arm": arm,
                      "rep": rep, "status": out.get("status"),
                      "tools": out.get("tool_calls"), "usd": usd,
                      "spent_total": lg["spent_usd"]}, indent=1))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: context_01_run.py <CASE> <luna|haiku> <A|B|C> <rep>")
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])))
