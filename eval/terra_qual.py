"""REVIEW-TERRA-01 qualification: Terra B+FINAL, 9 cases, 3 reps, cap $5.

Configuration parity with FINAL-01 B+FINAL except the model:
bundle-0.1, same instructions, same tools, same evidence rules,
12 turns, 40 tool calls, enable_final with 39/11 reserve. Terra uses
plain store=false settings (no max_tokens cap; Luna's harness used
max_tokens=3000 — disclosed, not copied: no model-specific tuning).

Cases: FW3, KL1, L08 (review_pr, live heads match frozen),
L06, L10 (review_local, PRs merged since), WT4 (review_pr),
P15 H5, PR25, PR27 (review_local on frozen yt2wiki checkout).
Frozen reference sets untouched. Isolated runs: no GitHub posting,
no email, no watcher, no Haiku.
"""

import json, os, sys
from pathlib import Path
sys.path.insert(0, "/tmp/codatiel-public")
from codatiel_i2 import providers as P, service as S

CASES = {
    "FW3": {"mode": "pr", "repo": "mitchins/frigate-watchdog", "pr": 3, "base": "956f7445fa13e93309dc5809395cd316ae4d7403", "head": "d9b61df6c889a28b0cb49547979897533b873ba4"},
    "KL1": {"mode": "pr", "repo": "mitchins/kinglet-lms", "pr": 1, "base": "dbf06e0004a44570dc253e88fffd7433d95997c8", "head": "694269d572bd77791f50dfd61d2ec52323da9c47"},
    "L08": {"mode": "pr", "repo": "Smart-Compliance-Systems/scs-infra", "pr": 4, "base": "aa807b86fda364ef0723b536672e07cca4646c6e", "head": "aff2a0680940964ce0d7304a2699f0415893a4b4"},
    "L06": {"mode": "local", "root": "/tmp/repos/mdmwi", "base": "6509400ca318ba90d868e3cfa7fe7a5f2571bb86", "head": "4051ff05090a76165f1a533675086e5760b97cbd"},
    "L10": {"mode": "local", "root": "/tmp/repos/needletail", "base": "08847b4cd2691c127597218dfd31457bf246145f", "head": "80bf493d1cae6d9d40fe70894ee294be80292af2"},
    "WT4": {"mode": "pr", "repo": "mitchins/wall-tidbits", "pr": 4, "base": "9333622385f7139ae2eddeb4a1efaec42c59f1aa", "head": "f053c2f89fd1f1acd509b0962e83ab211cbb4f71"},
    "P15": {"mode": "local", "root": "/tmp/yt2wiki", "base": "c45c2ec6e43faa43a136ce49ed16197aaf6a144b", "head": "58c26e20e0fc4c764fd224b3a562ddf08e38be7a"},
    "PR25": {"mode": "local", "root": "/tmp/yt2wiki", "base": "5c17a97c21d0ce77900957dcaa9613e856f1aa28", "head": "ebd9783cc2aa6534abdd39b68fe27c070996bc82"},
    "PR27": {"mode": "local", "root": "/tmp/yt2wiki", "base": "bed3aae8df6994ccb6c5b8e38628c45949dee581", "head": "d28cdfcbc0cff39e8dbc628a225a4fe2259d465a"},
}
ROOT = Path(os.environ.get("TERRA_ROOT", "/tmp/terra"))
RUNS = ROOT / "runs"
WORK = ROOT / "work"
LEDGER = ROOT / "ledger.json"
CEILING = float(os.environ.get("TERRA_CEILING_USD", "5.00"))
RESERVE = float(os.environ.get("TERRA_RESERVE_USD", "0.20"))

def load():
    if not LEDGER.exists():
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps({"ceiling_usd": CEILING, "spent_usd": 0.0, "runs": []}))
    return json.loads(LEDGER.read_text())
def save(lg):
    LEDGER.write_text(json.dumps(lg, indent=1))

def run_one(case, rep):
    lg = load()
    if lg["spent_usd"] + RESERVE >= CEILING:
        print(json.dumps({"refused": "ceiling", "spent": lg["spent_usd"]}))
        return 2
    spec = CASES[case]
    resolved = P.resolve_backend("terra")
    model, settings = P.build_model("terra")
    kw = dict(model=model, model_name=f"terra:{resolved['model']}",
              runs_root=RUNS / "terra" / "B" / case / f"rep{rep}",
              work_root=WORK / "terra" / "B" / case / f"rep{rep}",
              max_turns=12, model_settings=settings,
              rate=resolved["rate_usd_per_mtok"],
              rate_status=resolved["rate_status"],
              max_usd_per_review=CEILING - lg["spent_usd"],
              inventory_strategy="bundle-0.1", enable_final=True)
    if spec["mode"] == "local":
        out = S.review_local(spec["root"], spec["base"], spec["head"], **kw)
    else:
        out = S.review_pr(spec["repo"], spec["pr"], expected_base=spec["base"],
                           expected_head=spec["head"], **kw)
    spend = {}
    if out.get("run_dir"):
        mp = Path(out["run_dir"]) / "metrics.json"
        if mp.exists():
            spend = json.loads(mp.read_text()).get("spend", {})
    usd = spend.get("usd_estimate") or 0.0
    err = out.get("error") or ""
    if out.get("stage") == "agent" and any(s in err for s in ("credit balance", "authentication", "PermissionDenied", "ConnectionError", "Timeout", "timed out", "APIConnectionError")):
        usd = 0.0
    lg["spent_usd"] = round(lg["spent_usd"] + usd, 4)
    lg["runs"].append({"case": case, "arm": "B", "rep": rep, "status": out.get("status"), "stage": out.get("stage"), "run_dir": out.get("run_dir"), "tool_calls": out.get("tool_calls"), "usd": usd, "spend": spend})
    save(lg)
    print(json.dumps({"case": case, "rep": rep, "status": out.get("status"), "usd": usd, "spent": lg["spent_usd"]}))

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: terra_qual.py <CASE> <rep>")
        sys.exit(2)
    sys.exit(run_one(sys.argv[1], int(sys.argv[2])))
