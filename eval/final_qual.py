"""FINAL-01 qualification: A+FINAL vs B+FINAL, 7 cases, 3 reps, luna, cap $2."""

import json, os, sys
from pathlib import Path
sys.path.insert(0, "/tmp/codatiel-public")
from codatiel_i2 import providers as P, service as S

CASES = {
    "FW3": {"repo": "mitchins/frigate-watchdog", "pr": 3, "base": "956f7445fa13e93309dc5809395cd316ae4d7403", "head": "d9b61df6c889a28b0cb49547979897533b873ba4"},
    "KL1": {"repo": "mitchins/kinglet-lms", "pr": 1, "base": "dbf06e0004a44570dc253e88fffd7433d95997c8", "head": "694269d572bd77791f50dfd61d2ec52323da9c47"},
    "L08": {"repo": "Smart-Compliance-Systems/scs-infra", "pr": 4, "base": "aa807b86fda364ef0723b536672e07cca4646c6e", "head": "aff2a0680940964ce0d7304a2699f0415893a4b4"},
    "L06": {"repo": "My-Day-My-Way/my-day-my-way-ios", "pr": 11, "base": "6509400ca318ba90d868e3cfa7fe7a5f2571bb86", "head": "4051ff05090a76165f1a533675086e5760b97cbd"},
    "L10": {"repo": "mitchins/needletail-harness", "pr": 4, "base": "08847b4cd2691c127597218dfd31457bf246145f", "head": "80bf493d1cae6d9d40fe70894ee294be80292af2"},
    "WT4": {"repo": "mitchins/wall-tidbits", "pr": 4, "base": "9333622385f7139ae2eddeb4a1efaec42c59f1aa", "head": "f053c2f89fd1f1acd509b0962e83ab211cbb4f71"},
    "P15": {"repo": "mitchins/yt2wiki", "pr": 15, "base": "c45c2ec6e43faa43a136ce49ed16197aaf6a144b", "head": "58c26e20e0fc4c764fd224b3a562ddf08e38be7a"},
}
ARMS = {"A": "file-list-0.1", "B": "bundle-0.1"}
ROOT = Path(os.environ.get("FINAL_ROOT", "/tmp/qual-final"))
RUNS = ROOT / "runs"
WORK = ROOT / "work"
LEDGER = ROOT / "ledger.json"
CEILING = 2.0
RESERVE = 0.20

def load():
    if not LEDGER.exists():
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps({"ceiling_usd": CEILING, "spent_usd": 0.0, "runs": []}))
    return json.loads(LEDGER.read_text())
def save(lg):
    LEDGER.write_text(json.dumps(lg, indent=1))

def run_one(case, arm, rep):
    lg=load()
    if lg["spent_usd"]+RESERVE >= CEILING:
        print(json.dumps({"refused": "ceiling", "spent": lg["spent_usd"]}))
        return 2
    spec=CASES[case]
    strat=ARMS[arm]
    resolved=P.resolve_backend("luna")
    model, settings=P.build_model("luna")
    from agents.model_settings import ModelSettings
    settings=ModelSettings(store=False, max_tokens=3000)
    # Use local for P15
    if case=="P15":
        repo_root=Path(os.environ.get("YT2WIKI_REPO", "/tmp/yt2wiki"))
        out=S.review_local(repo_root, spec["base"], spec["head"], model=model, model_name=f"luna:{resolved['model']}", runs_root=RUNS/"luna"/arm/case/f"rep{rep}", work_root=WORK/"luna"/arm/case/f"rep{rep}", max_turns=12, model_settings=settings, rate=resolved["rate_usd_per_mtok"], rate_status=resolved["rate_status"], max_usd_per_review=CEILING-lg["spent_usd"], inventory_strategy=strat, enable_final=True)
    else:
        out=S.review_pr(spec["repo"], spec["pr"], model=model, model_name=f"luna:{resolved['model']}", runs_root=RUNS/"luna"/arm/case/f"rep{rep}", work_root=WORK/"luna"/arm/case/f"rep{rep}", max_turns=12, model_settings=settings, rate=resolved["rate_usd_per_mtok"], rate_status=resolved["rate_status"], max_usd_per_review=CEILING-lg["spent_usd"], expected_base=spec["base"], expected_head=spec["head"], inventory_strategy=strat, enable_final=True)
    spend={}
    if out.get("run_dir"):
        mp=Path(out["run_dir"])/"metrics.json"
        if mp.exists():
            spend=json.loads(mp.read_text()).get("spend",{})
    usd=spend.get("usd_estimate")or 0.0
    # Check for pre-response infra
    err=out.get("error") or ""
    if out.get("stage")=="agent" and any(s in err for s in ("credit balance","authentication","PermissionDenied","ConnectionError","Timeout","timed out","APIConnectionError")):
        usd=0.0
    lg["spent_usd"]=round(lg["spent_usd"]+usd,4)
    lg["runs"].append({"case":case,"arm":arm,"rep":rep,"status":out.get("status"),"stage":out.get("stage"),"run_dir":out.get("run_dir"),"tool_calls":out.get("tool_calls"),"usd":usd,"spend":spend})
    save(lg)
    print(json.dumps({"case":case,"arm":arm,"rep":rep,"status":out.get("status"),"usd":usd,"spent":lg["spent_usd"]}))

if __name__=="__main__":
    import sys
    if len(sys.argv)!=4:
        print("usage: final_qual.py <CASE> <A|B> <rep>")
        sys.exit(2)
    sys.exit(run_one(sys.argv[1], sys.argv[2], int(sys.argv[3])))
