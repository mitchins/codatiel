"""M10 runner: one (case, rep) with the DeepSeek Flash backend.

Frozen M9.2 development cases, canonical minimal prompt (arm A) + original
file-list inventory only. No prompt tuning, no Sonnet/Sol. Root /tmp/m10
(never /tmp/m9_2); ceiling $5.00 hard.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/codatiel-public")  # NOSONAR - fixed source path
from codatiel_i2 import providers as P  # NOSONAR
from codatiel_i2 import service as S  # NOSONAR
from eval.m9_2_run import CASES  # NOSONAR - frozen roster + SHAs

ROOT = Path(os.environ.get("M10_ROOT", "/tmp/m10"))
RUNS = ROOT / "runs"
WORK = ROOT / "work"
LEDGER = ROOT / "ledger.json"
CEILING = float(os.environ.get("M10_CEILING_USD", "5.0"))
RESERVE = float(os.environ.get("M10_RESERVE_USD", "0.50"))

# Pre-response infra failures (no model response possible): debit $0.
# DeepSeek variants included alongside the M9.2 patterns.
INFRA_PATTERNS = ("credit balance", "authentication", "PermissionDenied",
                  "ConnectionError", "Timeout", "timed out",
                  "APIConnectionError", "insufficient", "balance",
                  "unauthorized", "invalid_api_key", "rate_limit_exceeded")


def ledger():
    if not LEDGER.exists():  # NOSONAR
        LEDGER.parent.mkdir(parents=True, exist_ok=True)  # NOSONAR
        LEDGER.write_text(json.dumps({"ceiling_usd": CEILING, "spent_usd": 0.0,  # NOSONAR
                                      "runs": []}))
    return json.loads(LEDGER.read_text())  # NOSONAR


def save(lg):  # NOSONAR
    LEDGER.write_text(json.dumps(lg, indent=1))  # NOSONAR


def main(case_id: str, rep: int, tool_budget: int = 40):
    case = CASES[case_id]
    lg = ledger()
    if lg["spent_usd"] + RESERVE >= CEILING:
        print(json.dumps({"refused": "ceiling-reserve",
                           "spent": lg["spent_usd"],
                           "ceiling": CEILING}))
        return 2
    resolved = P.resolve_backend("deepseek")
    model, settings = P.build_model("deepseek")
    kw = dict(model=model, model_name=f"deepseek:{resolved['model']}",
              runs_root=RUNS / "deepseek" / "A" / case_id / f"rep{rep}",
              work_root=WORK / "deepseek" / "A" / case_id / f"rep{rep}",
              max_turns=12, model_settings=settings, tool_budget=tool_budget,
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
    err = out.get("error") or ""
    pre_response_infra = (out.get("stage") == "agent" and any(
        s in err for s in INFRA_PATTERNS))
    if pre_response_infra:
        usd = 0.0
    usage = dict(spend.get("usage", {}))
    for extra in ("cached_input_tokens", "reasoning_output_tokens",
                  "usage_details_reported_responses"):
        if spend.get(extra) is not None:
            usage[extra] = spend[extra]
    lg["spent_usd"] = round(lg["spent_usd"] + usd, 4)
    lg["runs"].append({"case": case_id, "model": "deepseek", "arm": "A",
                       "rep": rep, "tool_budget": tool_budget,
                       "status": out.get("status"),
                       "stage": out.get("stage"),
                       "run_dir": out.get("run_dir"),
                       "tool_calls": out.get("tool_calls"),
                       "usd_estimate": usd,
                       "usage": usage,
                       "accounting_status": spend.get("accounting_status",
                                                     "unknown")})
    save(lg)
    print(json.dumps({"case": case_id, "model": "deepseek", "arm": "A",
                       "rep": rep, "status": out.get("status"),
                       "tools": out.get("tool_calls"), "usd": usd,
                       "spent_total": lg["spent_usd"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]),
                  int(sys.argv[3]) if len(sys.argv) > 3 else 40))
