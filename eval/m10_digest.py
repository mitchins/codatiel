"""M10 P4.1: private review digest generator (no inference).

Reads one canonical run dir, emits a readable Markdown digest to stdout.
Surfaces model-verified findings with their evidence; every claim is
labeled with its provenance (model confidence, verification disposition).
An incomplete run NEVER renders as successful silence.
"""

import json
import sys
from pathlib import Path


def load_jsonl(p):
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()
            if line.strip()]


def main(run_dir: str):
    d = Path(run_dir)
    run = json.loads((d / "run.json").read_text())
    metrics = json.loads((d / "metrics.json").read_text())
    spend = metrics.get("spend", {})
    try:
        shadow = json.loads((d / "shadow_result.json").read_text())
    except FileNotFoundError:
        shadow = {}
    findings = load_jsonl(d / "findings.jsonl")
    ev = {}
    for e in load_jsonl(d / "evidence.jsonl"):
        ev[e.get("evidence_id")] = e
    status = metrics.get("status", "?")
    out = []
    out.append(f"# Shadow review: {run.get('repo')} "
               f"(base `{str(run.get('base_sha'))[:12]}` → "
               f"head `{str(run.get('head_sha'))[:12]}`)")
    out.append("")
    out.append(f"- Run: `{run.get('run_id')}` | model "
               f"`{metrics.get('model')}` | instructions "
               f"`{metrics.get('instructions_id')}`")
    out.append(f"- Status: **{status}** | tool calls "
               f"{metrics.get('tool_calls')} | wall "
               f"{metrics.get('wall_seconds', '?')}s | cost "
               f"${(spend.get('usd_estimate') or 0):.4f} "
               f"({spend.get('accounting_status', '?')})")
    if status != "complete":
        out.append("")
        out.append("> **INCOMPLETE INVESTIGATION.** Findings below (if any) "
                   "are partial. Absence of findings is NOT evidence of a "
                   "clean change.")
    out.append("")
    verified = [f for f in findings
                if f.get("disposition") == "verified_proof"]
    verified.sort(key=lambda f: -(f.get("confidence") or 0))
    if status == "complete" and not verified:
        sil = (shadow.get("silence") or {})
        summ = sil.get("summary") or "model submitted no findings."
        out.append("## No material findings (completed investigation)")
        out.append("")
        out.append(f"Silence rationale (model-authored, unverified): {summ}")
    for i, f in enumerate(verified, 1):
        anchor = f.get("changed_anchor") or {}
        out.append(f"## {i}. {f.get('bad_outcome', '(untitled)')}")
        out.append("")
        out.append(f"- Location: `{anchor.get('path')}` "
                   f"lines {anchor.get('line_start')}–{anchor.get('line_end')}")
        out.append(f"- Trigger: {f.get('trigger', '?')}")
        out.append(f"- Behavioral consequence: {f.get('head_state', '?')} "
                   f"(base: {f.get('base_state', '?')})")
        out.append(f"- Detector: `{f.get('detector')}` | model confidence "
                   f"{f.get('confidence')} | disposition "
                   f"`{f.get('disposition')}` (model-performed verification, "
                   f"not independent) | finding `{f.get('finding_id')}`")
        out.append(f"- Verification note (mechanical, not a repair plan): "
                   f"{f.get('reason', '?')}")
        out.append("- Evidence:")
        for eid in f.get("evidence_ids") or []:
            e = ev.get(eid, {})
            excerpt = (e.get("excerpt") or "")[:600].replace("\n", " ")
            out.append(f"  - `{eid}` "
                       f"{e.get('path', '?')}@{str(e.get('head_sha') or e.get('base_sha') or '')[:12]}: "
                       f"{excerpt}")
    if status != "complete" and not verified:
        out.append("No findings were submitted before the run ended. "
                   "Re-running (or a conditional second pass) may produce "
                   "coverage; do not treat this as a clean bill.")
    out.append("")
    out.append("---")
    out.append("Private shadow artifact. Model claims are unverified until "
               "human-rated (see M10_FEEDBACK.md) or independently "
               "adjudicated. Never post as a PR comment.")
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1])
