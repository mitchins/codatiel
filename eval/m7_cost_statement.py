"""Reconcile the frozen M7 cost ledger without inventing unknown costs.

The historical ``M7_LEDGER.json`` is preserved unchanged. This statement
classifies each execution from immutable ledger and trace artifacts only:

- ``CONFIRMED_USAGE``: provider usage is recorded for a paid backend.
- ``CONFIRMED_ZERO_USAGE``: local/scripted execution with authoritative
  zero-cost profile and recorded usage.
- ``UNKNOWN_USAGE``: no authoritative provider usage is available. A $0.00
  ledger value is not evidence of zero billed cost.

The historical $0.8687 accounted amount is a lower bound, not the total
billed. Transcripts and console totals are not converted into usage.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACES_ROOT = REPO_ROOT / "eval" / "M7_TRACES"
LEDGER_PATH = REPO_ROOT / "eval" / "M7_LEDGER.json"
STATEMENT_PATH = REPO_ROOT / "eval" / "M7_COST_STATEMENT.json"

PAID_BACKENDS = {"haiku", "luna"}


def _trace_by_run_id() -> dict:
    traces = {}
    for trace_dir in sorted(TRACES_ROOT.iterdir()):
        run_path = trace_dir / "run.json"
        metrics_path = trace_dir / "metrics.json"
        if not run_path.exists():
            continue
        run = json.loads(run_path.read_text(encoding="utf-8"))
        metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
        traces[run.get("run_id")] = {"trace": trace_dir.name, "run": run, "metrics": metrics}
    return traces


def _has_recorded_usage(usage: object) -> bool:
    return (isinstance(usage, dict) and bool(usage)
            and any(key in usage for key in ("requests", "input_tokens", "output_tokens")))


def classify(row: dict, traces: dict) -> dict:
    run_dir = str(row.get("run_dir") or "")
    run_id = run_dir.rstrip("/").rsplit("/", 1)[-1] if run_dir else ""
    trace = traces.get(run_id, {})
    metrics = trace.get("metrics", {})
    ledger_usage = row.get("usage")
    trace_usage = (metrics.get("spend") or {}).get("usage")
    usage = ledger_usage if _has_recorded_usage(ledger_usage) else trace_usage
    rate_status = (metrics.get("spend") or {}).get("rate_status", "")
    evidence = "M7_LEDGER.json usage" if _has_recorded_usage(ledger_usage) else (
        f"{trace.get('trace')}/metrics.json spend.usage" if _has_recorded_usage(trace_usage)
        else "no authoritative provider usage")
    if _has_recorded_usage(usage):
        if row.get("model") not in PAID_BACKENDS and rate_status == "local_zero_cost":
            return {"usage_class": "CONFIRMED_ZERO_USAGE", "evidence": evidence, "usage": usage}
        return {"usage_class": "CONFIRMED_USAGE", "evidence": evidence, "usage": usage}
    return {"usage_class": "UNKNOWN_USAGE", "evidence": evidence, "usage": {}}


def build() -> Path:
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    traces = _trace_by_run_id()
    rows = []
    for index, row in enumerate(ledger.get("runs", [])):
        classified = classify(row, traces)
        usd = row.get("usd_estimate")
        usd_value = float(usd) if isinstance(usd, (int, float)) else None
        rows.append({
            "ledger_index": index,
            "case": row.get("case"),
            "model": row.get("model"),
            "status": row.get("status"),
            "run_dir": row.get("run_dir"),
            "accounted_usd_estimate": usd_value,
            **classified,
        })
    accounted = float(ledger.get("spent_haiku_usd"))
    # Preserve the frozen campaign total verbatim. Re-summing artifact-laden
    # binary floats would alter its fourth decimal place; the ledger's own
    # total is the historical accounted lower bound.
    statement = {
        "schema_version": "codatiel_i2.m7_cost_statement.v1",
        "source_ledger": "eval/M7_LEDGER.json (frozen; this file does not modify it)",
        "source_traces": "eval/M7_TRACES/*/metrics.json (frozen)",
        "accounted_usd_lower_bound": accounted,
        "accounted_is_lower_bound_not_total": True,
        "rows": rows,
        "unknown_executions": [row["ledger_index"] for row in rows if row["usage_class"] == "UNKNOWN_USAGE"],
    }
    STATEMENT_PATH.write_text(json.dumps(statement, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8")
    return STATEMENT_PATH


if __name__ == "__main__":
    print(build())
