"""Minimal dashboard build (§20): per-detector table, no narrative generation."""

from __future__ import annotations

from pathlib import Path


def render_markdown(*, repo: str, base_sha: str, head_sha: str,
                    coverage: dict, findings: list[dict]) -> str:
    d = (coverage or {}).get("detectors", {})
    rows = [
        ("Contracts",
         f"{d.get('contracts', {}).get('checked', 0)}/{d.get('contracts', {}).get('changed_boundaries', 0)}",
         str(sum(1 for f in findings if f.get('detector') == 'contracts'))),
        ("Authorization",
         f"{d.get('authorization', {}).get('checked', 0)}/{d.get('authorization', {}).get('changed_entrypoints', 0)}",
         str(sum(1 for f in findings if f.get('detector') == 'authorization'))),
        ("Docs <-> Code",
         f"{d.get('docs_code', {}).get('checked', 0)}/{d.get('docs_code', {}).get('applicable_relationships', 0)}",
         str(sum(1 for f in findings if f.get('detector') == 'docs_code'))),
        ("Operating constraints",
         f"{d.get('operating_constraints', {}).get('checked', 0)}/{d.get('operating_constraints', {}).get('changed_candidate_sites', 0)}",
         str(sum(1 for f in findings if f.get('detector') == 'operating_constraints'))),
        ("Semantic twins",
         f"{d.get('semantic_twins', {}).get('queried', 0)}/{d.get('semantic_twins', {}).get('changed_functions', 0)}",
         str(sum(1 for f in findings if f.get('detector') == 'semantic_twins'))),
    ]
    lines = [f"# Codatiel Iteration 2 — {repo}", "",
             f"base `{base_sha[:8]}` head `{head_sha[:8]}`", "",
             "Authoritative status is each finding's `disposition` "
             "(verified_proof / advisory_grounded / observed_not_material / "
             "unresolved / refuted_unsupported / rejected_invalid), assigned by "
             "the canonical service after evidence replay and trust "
             "validation. Detector-claimed proof/verification labels are "
             "inputs to that decision, not verdicts. All output is "
             "shadow-only: nothing here is published.", "",
             "Detector | Checked | Findings", "--- | --- | ---"]
    for name, checked, verified in rows:
        lines.append(f"{name} | {checked} | {verified}")
    lines.append("")
    for f in findings:
        anchor = f.get("changed_anchor", {})
        counterparts = f.get("counterpart_anchors") or []
        if counterparts:
            first = counterparts[0]
            counterpart_text = f"{first.get('path')}:{first.get('line_start')}-{first.get('line_end')}"
            if len(counterparts) > 1:
                counterpart_text += f" (+{len(counterparts) - 1} more)"
        else:
            counterpart_text = "none"
        lines.append(f"- [{f.get('detector')}/{f.get('subclass')}] {f.get('invariant')}"
                     f" @ {anchor.get('path')}:{anchor.get('line_start')}-{anchor.get('line_end')}"
                     f" (disposition={f.get('disposition', 'unrecorded')},"
                     f" claimed proof={f.get('proof', {}).get('level')},"
                     f" claimed verification={f.get('verification', {}).get('status')})")
        lines.append(f"  counterpart: {counterpart_text}")
        lines.append(f"  trigger: {f.get('trigger')}; bad outcome: {f.get('bad_outcome')}")
    lines.append("")
    return "\n".join(lines)


def write_dashboard(run_dir: Path, content: str) -> Path:
    out = Path(run_dir) / "dashboard.md"
    out.write_text(content, encoding="utf-8")
    return out
