"""REVIEW-ADDITIVE-01: minimal additive review path.

Invariant: Terra's primary review runs FIRST, complete and unchanged
(canonical model/tools/prompt/budget, NO delegation tools). Only after
Terra seals does Luna's blind investigation run against the same immutable
snapshot. Consolidation is deterministic (no merge model):

- Terra findings are preserved verbatim (sealed run.json).
- Luna findings arrive in a machine-readable contract; each is validated
  against pinned source evidence, deduplicated against Terra, and appended
  with provenance — or rejected with an explicit reason.
- Any subagent/merge failure degrades to Terra-only output and never
  fails the primary review.

Luna's PR-scoped session persists after the seal for post-review
follow-ups (same retention/cleanup guarantees as REVIEW-DELEGATE-01).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from pathlib import Path
from typing import Any

# Output contract appended to the unchanged investigative instructions.
# Constrains FORMAT only, so blind-sweep behavior stays comparable.
SWEEP_OUTPUT_CONTRACT = """
Output contract (format only — investigation discipline unchanged).
Append exactly one fenced block at the end of your report:

```json
{"findings": [
  {"title": "...", "severity": "P0|P1|P2|P3",
   "path": "repo/relative/path", "revision": "base|head",
   "line_start": 1, "line_end": 1,
   "evidence": "verbatim quoted lines",
   "why_pr_introduced": "one sentence"} ]}

Rules: one object per genuine defect (unrelated defects welcome);
omit the block only when the change is clean; severity is your honest
guess; every claim needs a file:line anchor and verbatim evidence.
"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _keywords(text: str) -> set[str]:
    stop = {"the", "and", "with", "from", "that", "this", "when", "which",
            "have", "were", "been", "into", "such", "than", "then", "also",
            "each", "both", "more", "most", "only", "over", "under", "new",
            "using", "used", "base", "head", "file", "line", "code"}
    return {w for w in re.findall(r"[a-z0-9_]{4,}", _norm(text))
            if w not in stop}


def parse_sweep_findings(sweep_text: str) -> tuple[list[dict], str | None]:
    """Extract the findings array from a sweep report.

    Returns (findings, error). Unparseable text is NOT an error for the
    primary review: callers degrade to Terra-only with the reason.
    """
    m = re.search(r"```json\s*(\{.*?\})\s*```", sweep_text or "",
                  re.DOTALL)
    if not m:
        return [], "no fenced json findings block"
    try:
        doc = json.loads(m.group(1))
    except Exception as exc:
        return [], f"findings block unparseable: {type(exc).__name__}"
    items = doc.get("findings", [])
    if not isinstance(items, list):
        return [], "findings is not a list"
    return items, None


def _read_span(base_dir: Path, head_dir: Path, path: str, revision: str,
               start: int, end: int) -> tuple[str | None, str]:
    if revision not in ("base", "head"):
        return None, f"bad revision {revision!r}"
    try:
        text = (base_dir if revision == "base" else head_dir
                ).joinpath(path).read_text(encoding="utf-8",
                                            errors="replace")
    except OSError as exc:
        return None, f"unreadable path: {type(exc).__name__}"
    lines = text.splitlines()
    if start < 1 or end < start:
        return None, "bad line range"
    span = "\n".join(lines[start - 1:end])
    return span, ""


def _quoted_segments(evidence: str) -> list[str]:
    """Double-quoted substrings: the model's verbatim excerpts.

    Quotes are unescaped first (JSON blocks often carry \"...\").
    Only segments of 9+ chars count; surrounding prose is ignored.
    """
    unesc = str(evidence or "").replace('\\"', '"').replace("\\'", "'")
    return [s.strip() for s in re.findall(r'"([^"]{9,})"', unesc)
            if s.strip()]


def _evidence_lines_present(evidence: str, text: str) -> tuple[bool, str]:
    """Per-line evidence check for excerpt-assembled quotes.

    Subagents legitimately quote several non-adjacent lines. Require a
    majority of substantive evidence lines verbatim (whitespace-folded)
    in the text; single-line evidence must match exactly.
    """
    lines = [l.strip() for l in str(evidence or "").splitlines()
             if len(l.strip()) > 8]
    if not lines:
        return False, "empty evidence"
    flat = _norm(text)
    hits = sum(1 for l in lines if _norm(l) in flat)
    if len(lines) == 1:
        return (hits == 1, "" if hits == 1 else
                "single-line evidence not found")
    need = max(1, math.ceil(len(lines) * 0.6))
    if hits >= need:
        return True, f"{hits}/{len(lines)} evidence lines present"
    return False, f"only {hits}/{len(lines)} evidence lines present"


def validate_anchor(finding: dict, base_dir: Path,
                    head_dir: Path) -> tuple[bool, str]:
    """Anchor + evidence check against the pinned snapshot."""
    for key in ("title", "path", "evidence"):
        if not finding.get(key):
            return False, f"missing {key}"
    if finding.get("severity") not in ("P0", "P1", "P2", "P3"):
        return False, "bad severity"
    try:
        start = int(finding.get("line_start", 0) or 0)
        end = int(finding.get("line_end", 0) or start)
    except (TypeError, ValueError):
        return False, "bad line numbers"
    span, err = _read_span(base_dir, head_dir, str(finding["path"]),
                           str(finding.get("revision", "head") or "head"),
                           start, end)
    if err:
        return False, err
    ev = _norm(finding["evidence"])
    if not ev or _norm(span).find(ev[:120]) < 0 and ev not in _norm(span):
        # Evidence must appear verbatim (modulo whitespace) in the span;
        # fall back to segment/line matching: subagents legitimately quote
        # several excerpts ("..."-joined or multi-line) plus prose, and
        # JSON blocks often add escape/outer quotes. A span miss with
        # file-level hits is recorded with a note, not lost.
        try:
            full = (base_dir if str(finding.get("revision", "head")) == "base"
                    else head_dir).joinpath(
                        str(finding["path"])).read_text(
                            encoding="utf-8", errors="replace")
        except OSError:
            return False, "evidence not in anchored span"
        flat = _norm(full)
        segs = _quoted_segments(finding["evidence"])
        if segs:
            hits = sum(1 for s in segs if _norm(s) in flat)
            need = max(1, math.ceil(len(segs) * 0.6))
            if hits >= need:
                return True, (f"evidence quoted excerpts {hits}/{len(segs)}"
                              f" in file, outside anchored span")
            return False, (f"evidence not found at pinned revision "
                           f"(only {hits}/{len(segs)} quoted excerpts)")
        ok, note = _evidence_lines_present(finding["evidence"], full)
        if ok:
            return True, f"evidence assembled excerpts ({note})"
        return False, f"evidence not found at pinned revision ({note})"
    return True, ""


def is_duplicate(luna: dict, terra_findings: list[dict]) -> tuple[bool, str]:
    """Same defect iff same file + overlapping lines + shared substance."""
    lp = str(luna.get("path", ""))
    try:
        ls = int(luna.get("line_start", 0) or 0)
        le = int(luna.get("line_end", 0) or ls)
    except (TypeError, ValueError):
        return False, ""
    lkw = _keywords(str(luna.get("title", "")) + " " +
                    str(luna.get("evidence", "")))
    for t in terra_findings:
        a = t.get("changed_anchor", {}) or {}
        tp = str(a.get("path", ""))
        if not tp or Path(tp).name != Path(lp).name and tp != lp:
            continue
        try:
            ts = int(a.get("line_start") or 0)
            te = int(a.get("line_end") or ts)
        except (TypeError, ValueError):
            continue
        overlap = not (le < ts - 5 or ls > te + 5)
        tkw = _keywords(str(t.get("bad_outcome", "")))
        shared = lkw & tkw
        if overlap and len(shared) >= 2:
            return True, f"same file+lines, shared: {sorted(shared)[:4]}"
    return False, ""


def _is_explicit_clean(sweep_text: str) -> bool:
    """Explicit clean verdict (no block needed): conservative Terra-only."""
    low = _norm(sweep_text or "")
    return any(p in low for p in (
        "no genuine defects found", "no genuine pr-introduced defects",
        "no defects found", "no findings."))


def consolidate(*, terra_run_dir: Path, luna_findings: list[dict],
                base_dir: Path, head_dir: Path) -> dict[str, Any]:
    """Deterministic merge: sealed Terra findings + validated Luna-only."""
    shadow = json.loads((terra_run_dir / "shadow_result.json").read_text())
    terra_rows = list(shadow.get("advisory", []))
    added, duplicates, rejected = [], [], []
    for f in luna_findings:
        ok, note = validate_anchor(f, base_dir, head_dir)
        if not ok:
            rejected.append({"finding": f, "reason": f"anchor: {note}"})
            continue
        dup, why = is_duplicate(f, terra_rows)
        if dup:
            duplicates.append({"finding": f, "reason": f"duplicate: {why}"})
            continue
        entry = dict(f)
        if note:
            entry["validation_note"] = note
        entry["provenance"] = "luna-blind-sweep"
        added.append(entry)
    return {"terra_findings": len(terra_rows), "luna_added": added,
            "duplicates": duplicates, "rejected": rejected,
            "luna_sweep_claims": len(luna_findings)}


def run_additive(*, mode: str, target: dict[str, Any],
                 runs_root: Path, work_root: Path,
                 sub_role: str = "luna",
                 sub_ceiling_usd: float = 0.25) -> dict[str, Any]:
    """Run Terra canonical first (sealed), then the blind subagent, merge.

    ``mode``/``target`` mirror the D01 runner ({mode: local|pr, ...}).
    Never fails a completed Terra primary because of subagent trouble.
    """
    from codatiel_i2 import delegate as delegate_mod
    from codatiel_i2 import providers as providers_mod
    from codatiel_i2 import runtime_sdk
    from codatiel_i2 import service as service_mod
    from codatiel_i2.core.spend import SpendLedger

    started = time.time()
    resolved = providers_mod.resolve_backend("terra")
    model, settings = providers_mod.build_model("terra")
    kw = dict(model=model, model_name=f"terra:{resolved['model']}",
              runs_root=runs_root, work_root=work_root,
              max_turns=12, model_settings=settings,
              rate=resolved["rate_usd_per_mtok"],
              rate_status=resolved["rate_status"],
              max_usd_per_review=5.0,
              inventory_strategy="bundle-0.1", enable_final=True,
              delegation=None)
    if target["mode"] == "local":
        out = service_mod.review_local(
            target["root"], target["base"], target["head"], **kw)
    else:
        out = service_mod.review_pr(
            target["repo"], target["pr"], expected_base=target["base"],
            expected_head=target["head"], **kw)
    if out.get("status") not in ("complete", "inconclusive") or \
            not out.get("run_dir"):
        return {"status": out.get("status"), "stage": out.get("stage"),
                "error": out.get("error"), "mode": "terra-only-primary",
                "run_dir": out.get("run_dir")}
    terra_run_dir = Path(out["run_dir"])
    # Terra is sealed here: run.json + shadow_result.json are immutable
    # from this point on.
    try:
        spec = delegate_mod.DelegationSpec(
            tag=f"ADD-{target['case']}", artifact_dir=work_root / "sub",
            subs=(sub_role,))
        rsp = spec.role_spec(sub_role)
        sub_resolved = providers_mod.resolve_backend(sub_role)
        sub_model, sub_settings = providers_mod.build_model(sub_role)
        if sub_role == "luna":
            from agents.model_settings import ModelSettings
            sub_settings = ModelSettings(
                store=False,
                max_tokens=int(rsp.get("sub_max_tokens", 8000)))
        sub_ledger = SpendLedger(
            rate=sub_resolved["rate_usd_per_mtok"],
            rate_status=sub_resolved["rate_status"],
            ceiling_usd=float(sub_ceiling_usd))
        recorded = runtime_sdk.UsageRecordingModel(sub_model, sub_ledger)
        inv = json.loads((terra_run_dir / "prompt_provenance.json")
                         .read_text())["inventory"]
        # Reuse Terra's materialized snapshot dirs for anchor checks.
        ws_state = json.loads((terra_run_dir / "run.json").read_text())
        _ws = ws_state.get("workspace", {}) if isinstance(ws_state,
                                                          dict) else {}
        base_dir = Path(_ws.get("base_dir", "") or work_root)
        head_dir = Path(_ws.get("head_dir", "") or work_root)
        if target["mode"] == "local":
            diff_repo = Path(target["root"])
        else:
            from codatiel_i2.core import workspace as ws_mod
            sub_ws = ws_mod.prepare_github_workspace(
                target["repo"], target["pr"], work_root / "sub_ws")
            if sub_ws.status != "ready":
                raise RuntimeError(f"sub workspace: {sub_ws.error}")
            for label, want, got in (
                    ("base", target["base"], sub_ws.base_sha),
                    ("head", target["head"], sub_ws.head_sha)):
                if not (got.startswith(want) or want.startswith(got)):
                    raise RuntimeError(
                        f"sub workspace {label} mismatch {got[:12]}")
            diff_repo = Path(sub_ws.head_dir)
        sess = delegate_mod.SubagentSession(
            role=sub_role, backend=sub_role,
            tag=f"ADD-{target['case']}", artifact_dir=work_root / "sub",
            repo=ws_state.get("repo", ""), base_sha=ws_state["base_sha"],
            head_sha=ws_state["head_sha"], base_dir=base_dir,
            head_dir=head_dir, diff_repo=diff_repo, model=recorded,
            model_settings=sub_settings,
            rate=sub_resolved["rate_usd_per_mtok"],
            rate_status=sub_resolved["rate_status"], spec=rsp,
            instructions=delegate_mod.SWEEP_INSTRUCTIONS +
            SWEEP_OUTPUT_CONTRACT,
            fresh=False)
        sweep_input = ("Review this change between BASE and HEAD. "
                       f"Repository: {ws_state.get('repo', '')}. {inv}")
        sweep = asyncio.run(sess.sweep(sweep_input, sub_ledger))
        try:
            (work_root / "sub" / f"sweep-{sub_role}.txt").write_text(
                sweep["text"], encoding="utf-8")
        except OSError:
            pass
        if sweep["status"] != "ok" or not sweep["text"].strip():
            raise RuntimeError(f"sweep {sweep['status']}")
        findings, perr = parse_sweep_findings(sweep["text"])
        if perr:
            if _is_explicit_clean(sweep["text"]):
                findings = []
            else:
                raise RuntimeError(perr)
        merged = consolidate(terra_run_dir=terra_run_dir,
                             luna_findings=findings, base_dir=base_dir,
                             head_dir=head_dir)
        merged["sub_role"] = sub_role
        merged["sweep_chars"] = len(sweep["text"])
        (terra_run_dir / "additive.json").write_text(
            json.dumps(merged, indent=1, default=str))
        sub_rec = sub_ledger.to_record()
        return {"status": out["status"], "mode": "additive",
                "run_dir": str(terra_run_dir),
                "terra_findings": merged["terra_findings"],
                "added": len(merged["luna_added"]),
                "duplicates": len(merged["duplicates"]),
                "rejected": len(merged["rejected"]),
                "sub_usd": sub_rec.get("usd_estimate", 0.0),
                "wall_seconds": round(time.time() - started, 2)}
    except Exception as exc:
        return {"status": out["status"], "mode": "terra-only-degraded",
                "reason": f"{type(exc).__name__}: {exc}",
                "run_dir": str(terra_run_dir)}
