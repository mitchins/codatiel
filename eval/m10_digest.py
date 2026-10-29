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
    submitted = load_jsonl(d / "model_findings_raw.jsonl")
    ev = {}
    for e in load_jsonl(d / "evidence.jsonl"):
        ev[e.get("evidence_id")] = e
    from collections import Counter
    dismissed = Counter(f.get("detector", "?") for f in findings
                        if f.get("disposition") == "verified_proof")
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
    verified_ids = {f.get("finding_id") for f in findings
                    if f.get("disposition") == "verified_proof"}
    if status == "complete" and not submitted:
        sil = (shadow.get("silence") or {})
        summ = sil.get("summary") or "model submitted no findings."
        out.append("## No material findings (completed investigation)")
        out.append("")
        out.append(f"Silence rationale (model-authored, unverified): {summ}")
    for i, s in enumerate(submitted, 1):
        out.append(f"## {i}. {s.get('title', '(untitled)')}")
        out.append("")
        out.append(f"- Location: `{s.get('path')}` lines "
                   f"{s.get('line_start')}–{s.get('line_end')} "
                   f"(revision `{s.get('revision', '?')}`)")
        out.append(f"- Claim (model-authored, unverified): {s.get('claim', '?')}")
        out.append(f"- Detector: `{s.get('detector', '?')}`")
        out.append("- Evidence:")
        for eid in s.get("evidence_ids") or []:
            e = ev.get(eid, {})
            excerpt = (e.get("excerpt") or "")[:600].replace("\n", " ")
            out.append(f"  - `{eid}` "
                       f"{e.get('path', '?')}@{str(e.get('head_sha') or e.get('base_sha') or '')[:12]}: "
                       f"{excerpt}")
    if dismissed:
        out.append("")
        out.append(f"Note: {sum(dismissed.values())} verified candidates "
                   f"({dict(dismissed)}) were dismissed by the model and not "
                   "surfaced above. A dismissal is a model judgment, not a "
                   "proof of non-materiality.")
    if status != "complete" and not submitted:
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
