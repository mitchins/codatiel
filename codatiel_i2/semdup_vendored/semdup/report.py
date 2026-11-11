"""Markdown report rendering (terminal-friendly, no service needed)."""
from __future__ import annotations


def render(title: str, findings: list[dict], coverage: dict, usage: list[dict], notes: list[str]) -> str:
    lines = [f"# {title}", ""]
    for f in findings:
        lines += [f"## {f['id']}: {f['relationship']} / {f['action']}",
                  "", f"Responsibility: {f['responsibility']}", ""]
        for m in f["members"]:
            lines.append(f"- `{m['path']}:{m['start_line']}-{m['end_line']}`" +
                         (f" ({m['symbol']})" if m.get("symbol") else ""))
        lines += ["", f"Proposal: {f['proposal']['interface']}",
                  f"Benefit: {f['proposal']['benefit']}"]
        if f["observed_differences"]:
            lines.append("Differences: " + "; ".join(f["observed_differences"]))
        lines += [f"Validation: {f['validation']['level']}", ""]
    lines += ["## Coverage", "", str(coverage), "", "## Usage", "", str(usage), ""]
    if notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"
