"""Consolidated review digest model + renderers (DIGEST-01).

Builds ONE delivery-independent review model from stored run artifacts
(model output -> validated finding -> artifact) and renders it as
responsive HTML, plain text, or Markdown. No inference, no new claims:
every sentence is traceable to a validated artifact field. Anything the
artifacts do not say is labelled as unknown, never invented.

Verification honesty is structural: advisories carry their disposition
(candidate vs corroborated) and substantive-assessment state into every
renderer, so an unassessed candidate can never read as a confirmed
defect.
"""

from __future__ import annotations

import html as _html
import json
import re
from pathlib import Path
from typing import Any

MODEL_VERSION = "codatiel_i2.digest_model.v1"

_CONTEXT_RE = re.compile(
    r"^[^:]*:([\w.\-]+/[\w.\-]+)#(\d+)\s*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_EXCERPT_CHARS = 400
_EVIDENCE_CAP = 3


def _short_sha(sha: str) -> str:
    return sha[:12] if isinstance(sha, str) else ""


def _parse_context(context: str | None) -> dict:
    """owner/repo + PR number from an event label like dev:owner/repo#10."""
    if not context:
        return {}
    m = _CONTEXT_RE.match(context.strip())
    if not m:
        return {}
    return {"repo_slug": m.group(1), "pr_number": m.group(2)}


def _blob_url(slug: str, sha: str, path: str,
              start: int | None, end: int | None) -> str | None:
    if not (slug and _SHA_RE.match(sha or "") and path):
        return None
    url = f"https://github.com/{slug}/blob/{sha}/{path}"
    if start:
        url += f"#L{start}" + (f"-L{end}" if end and end != start else "")
    return url


def _evidence_index(run_dir: Path) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    try:
        lines = (run_dir / "evidence.jsonl").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return idx
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("evidence_id"):
            idx[str(e["evidence_id"])] = e
    return idx


def _verification_label(f: dict) -> str:
    """Honest human-readable verification state. Never a severity.

    Authenticity (the cited code is real and at the stated lines) is
    separated from interpretation (whether the claim about it is
    correct), because conflating the two is exactly what makes
    review emails misleading.
    """
    disp = str(f.get("disposition") or "")
    subst = str(f.get("substantive_assessment") or "UNASSESSED")
    if f.get("publish_eligible") is True or disp == "verified_proof":
        return ("Corroborated by replayed evidence at the pinned "
                "revisions: the cited code is real and the claim held "
                "up to the available checks. Interpret before acting.")
    if disp == "advisory_grounded":
        return ("Needs human review: the code excerpts below are real and "
                "were re-read at the stated lines of the reviewed "
                "commit, but nobody has yet judged whether the claim "
                f"about it is correct (interpretation: {subst.lower()}). "
                "Do not treat this as a confirmed defect.")
    if disp:
        return (f"Status {disp}; substantive assessment {subst.lower()} — "
                "treat as unverified.")
    return ("Verification state unknown — treat as unverified.")


def _finding_entry(n: int, f: dict, ev_idx: dict,
                   slug: str, head_sha: str) -> dict:
    raw = f.get("raw") if isinstance(f.get("raw"), dict) else {}
    anchor = (f.get("changed_anchor")
              if isinstance(f.get("changed_anchor"), dict) else {})
    path = str(anchor.get("path") or raw.get("path") or "")
    try:
        start = int(anchor.get("line_start") or raw.get("line_start") or 0
                    ) or None
    except (TypeError, ValueError):
        start = None
    try:
        end = int(anchor.get("line_end") or raw.get("line_end") or 0) or None
    except (TypeError, ValueError):
        end = None
    revision = str(anchor.get("revision") or raw.get("revision") or "")
    problem = (f.get("bad_outcome") or f.get("head_state")
               or raw.get("claim") or "")
    problem = " ".join(str(problem).split())
    title = str(raw.get("title") or "").strip()
    if not title:
        where = f"{path}:{start}" if path else "unknown location"
        title = (f"Untitled {f.get('detector', 'review')} observation "
                 f"at {where}")
    evidence = []
    for eid in (f.get("evidence_ids") or raw.get("evidence_ids") or []):
        e = ev_idx.get(str(eid))
        if not isinstance(e, dict):
            evidence.append({"id": str(eid), "missing": True})
            continue
        excerpt = " ".join(str(e.get("excerpt") or "").split())
        evidence.append({
            "id": str(eid), "path": str(e.get("path") or ""),
            "line_start": e.get("line_start"), "line_end": e.get("line_end"),
            "revision": str(e.get("revision") or ""),
            "rev_sha": str(e.get("revision_sha") or e.get("head_sha")
                           or e.get("base_sha") or ""),
            "excerpt": excerpt[:_EXCERPT_CHARS],
            "truncated": len(excerpt) > _EXCERPT_CHARS,
        })
        if len(evidence) >= _EVIDENCE_CAP:
            break
    return {
        "n": n, "finding_id": str(f.get("finding_id") or f"finding-{n}"),
        "title": title, "detector": str(f.get("detector") or ""),
        "path": path, "line_start": start, "line_end": end,
        "revision": revision,
        "url": _blob_url(slug, head_sha, path, start, end),
        "problem": problem, "verification": _verification_label(f),
        "evidence": evidence,
        "evidence_total": len(f.get("evidence_ids")
                              or raw.get("evidence_ids") or []),
    }


def _semdup_entry(sem: dict) -> dict:
    """Faithful semdup section, including honest coverage language."""
    status = str(sem.get("status") or "unknown")
    spend = sem.get("spend_usd", 0.0)
    if sem.get("spend_unknown"):
        spend_s = "spend unknown (not recorded; no amount is claimed)"
    else:
        try:
            spend_s = f"${float(spend):.4f}"
        except (TypeError, ValueError):
            spend_s = "unknown"
    targets = sem.get("targets") or []
    skipped = [s for s in (sem.get("skipped") or [])
               if isinstance(s, dict)]
    cov = str(sem.get("coverage") or "unknown")
    # Legacy records may claim "full" while listing skips. Any skip means
    # the examination was partial: the stored label never overrides the
    # skip list. Never report a partially examined PR as completely
    # checked for duplication.
    effective_full = (cov == "full" and not skipped)
    detail = sem.get("coverage_detail") if isinstance(
        sem.get("coverage_detail"), dict) else {}
    exam = detail.get("examination") if isinstance(
        detail.get("examination"), dict) else {}
    if "targets_examined" in exam:
        examined_targets = exam.get("targets_examined")
    else:
        # Legacy records predate the detail block: the skipped unit
        # strings name selected targets that were never examined.
        skipped_units = {s.get("unit") for s in skipped}
        examined_targets = sum(
            1 for t in targets
            if f"{t.get('path')}::{t.get('symbol')}" not in skipped_units)
    pairs = sem.get("pairs_examined", 0)
    published = [p for p in (sem.get("published") or [])
                 if isinstance(p, dict)]
    suppressed = [s for s in (sem.get("suppressed") or [])
                 if isinstance(s, dict)]
    # Legacy records predate the pairs counter: a stored zero alongside
    # published rows is a missing counter, not a real zero.
    if not pairs and "pairs_examined" not in sem and published:
        pairs_display = "not recorded (legacy run record)"
    else:
        pairs_display = f"{pairs} candidate pairs"
    if status != "ok":
        state = (f"Did not complete ({status}): "
                 f"{str(sem.get('reason') or '')[:200]}. No reuse claim "
                 "is made; the ordinary review findings above stand on "
                 "their own.")
    elif effective_full:
        if examined_targets == 0 and not pairs and not published:
            state = ("No changed units in scope and nothing skipped — a "
                     "genuine clean outcome for this check.")
        else:
            state = (f"Complete: {examined_targets} changed units "
                     f"examined, {pairs_display}, {len(published)} reuse "
                     "suggestions approved.")
    else:
        reasons = "; ".join(sorted({str(s.get("reason") or "")
                                    for s in skipped if s.get("reason")}))
        state = (f"Partial: {examined_targets} changed units examined, "
                 f"{pairs_display}, {len(skipped)} skipped "
                 f"({reasons or 'see semdup.json'}). "
                 "Nothing found in the examined code — this is not proof "
                  "that no duplication exists elsewhere in the change.")
    coverage_word = ("not completed" if status != "ok"
                     else "complete" if effective_full else "partial")
    pubs = []
    for p in published:
        ta = ((p.get("anchors") or {}).get("target") or {})
        ca = ((p.get("anchors") or {}).get("counterpart") or {})
        tgt, cpt = p.get("target") or {}, p.get("counterpart") or {}
        pubs.append({
            "target_ref": (f"{ta.get('path') or tgt.get('path')}:"
                           f"{ta.get('start_line')}-{ta.get('end_line')}"),
            "target_symbol": str(tgt.get("symbol") or ""),
            "counterpart_ref": (f"{ca.get('path') or cpt.get('path')}:"
                                f"{ca.get('start_line')}-"
                                f"{ca.get('end_line')}"),
            "counterpart_symbol": str(cpt.get("symbol") or ""),
            "decision": str(p.get("decision") or ""),
            "comment": " ".join(str(p.get("revised_comment") or "")
                                .split())[:400],
            "differences": " ".join(str(p.get("reason") or "")
                                    .split())[:400],
        })
    sups = []
    for s in suppressed:
        t, c = s.get("target") or {}, s.get("counterpart") or {}
        sups.append({
            "target": f"{t.get('path')}::{t.get('symbol')}",
            "counterpart": f"{c.get('path')}::{c.get('symbol')}",
            "reason": str(s.get("reason") or ""),
            "detail": " ".join(str(s.get("detail") or "").split())[:200],
        })
    return {
        "present": True, "status": status, "coverage": cov,
        "coverage_word": coverage_word,
        "spend": spend_s, "state": state,
        "reason": str(sem.get("reason") or "")[:200],
        "examined_targets": examined_targets, "pairs": pairs,
        "skipped": [{"unit": str(s.get("unit") or "?"),
                     "reason": str(s.get("reason") or "")}
                    for s in skipped],
        "published": pubs, "suppressed": sups,
    }


def build_review_model(run_dir: Path | str,
                       context: str | None = None) -> dict:
    """Assemble the delivery-independent review model from artifacts."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    try:
        shadow = json.loads((run_dir / "shadow_result.json").read_text(
            encoding="utf-8"))
    except OSError:
        shadow = {}
    try:
        metrics = json.loads((run_dir / "metrics.json").read_text(
            encoding="utf-8"))
    except OSError:
        metrics = {}
    try:
        sem = json.loads((run_dir / "semdup.json").read_text(
            encoding="utf-8"))
        if not isinstance(sem, dict):
            sem = {}
    except OSError:
        sem = {}
    ctx = _parse_context(context)
    slug = ctx.get("repo_slug", "")
    try:
        pr_number = int(ctx.get("pr_number", ""))
    except (TypeError, ValueError):
        pr_number = None
    base_sha = str(run.get("base_sha") or "")
    head_sha = str(run.get("head_sha") or "")
    advisories = [f for f in (shadow.get("advisory") or [])
                  if isinstance(f, dict)]
    ev_idx = _evidence_index(run_dir)
    findings = [_finding_entry(i + 1, f, ev_idx, slug, head_sha)
                for i, f in enumerate(advisories)]
    spend = (metrics.get("spend") or {})
    try:
        cost = float(spend.get("usd_estimate") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    status = str(shadow.get("status") or "?")
    counts = shadow.get("counts") if isinstance(
        shadow.get("counts"), dict) else {}
    verified = sum(1 for f in advisories
                   if f.get("disposition") == "verified_proof"
                   or f.get("publish_eligible") is True)
    return {
        "model_version": MODEL_VERSION,
        "repo_slug": slug,
        "repo_label": slug or str(run.get("repo") or ""),
        "pr_number": pr_number,
        "pr_url": (f"https://github.com/{slug}/pull/{pr_number}"
                   if slug and pr_number else None),
        "base_sha": base_sha, "head_sha": head_sha,
        "run_id": str(run.get("run_id") or ""),
        "review_status": status,
        "review_mode": str((metrics.get("mode") or run.get("mode")
                            or "")),
        "model": str((metrics.get("model")
                      or (run.get("config") or {}).get("model")
                      or "?")),
        "tool_calls": metrics.get("tool_calls"),
        "cost_usd": cost,
        "cost_unknown": bool(spend.get("accounting_status") == "unknown")
        if isinstance(spend, dict) else False,
        "counts": counts,
        "findings": findings,
        "n_findings": len(findings),
        "n_verified": verified,
        "silence": (shadow.get("silence") or {}) if isinstance(
            shadow.get("silence"), dict) else {},
        "semdup": _semdup_entry(sem) if sem else {"present": False},
        "overview": _overview(slug, pr_number, status, findings,
                              verified, sem),
    }


def _overview(slug: str, pr: int | None, status: str,
              findings: list[dict], verified: int, sem: dict) -> str:
    """Deterministic 2-3 sentence summary. Derived only, never invented."""
    where = f"{slug}#{pr}" if slug and pr else "this change"
    if status != "complete":
        return (f"Codatiel's review of {where} did not complete, so the "
                f"coverage below is partial and absence of findings proves "
                f"nothing. {len(findings)} candidate "
                f"{'finding' if len(findings) == 1 else 'findings'} "
                f"{'was' if len(findings) == 1 else 'were'} recorded before "
                f"the run ended; each needs human judgment.")
    pubs = (sem.get("published", []) if isinstance(sem, dict)
              else [])
    group = ""
    if len(pubs) > 3:
        # Longer reviews get a grouping line: area counts by target file,
        # top three named. Deterministic, derived only from stored rows.
        from collections import Counter
        areas = Counter(str(((p.get("target") or {}).get("path") or "?"))
                        for p in pubs if isinstance(p, dict))
        top = ", ".join(f"{path.split('/')[-1]} ({n})"
                        for path, n in areas.most_common(3))
        n_files = len(areas)
        group = (f" The {len(pubs)} reuse suggestions span "
                 f"{n_files} {'file' if n_files == 1 else 'files'}, "
                 f"most in {top}.")
    if not findings:
        if pubs:
            return (f"Codatiel's ordinary review of {where} surfaced no "
                    f"findings, but the duplication check approved "
                    f"{len(pubs)} reuse "
                    f"{'suggestion' if len(pubs) == 1 else 'suggestions'} "
                    f"(see Possible existing functionality below).{group}")
        return (f"Codatiel completed its review of {where} with no material "
                f"findings. Silence here means the investigated surface "
                f"produced nothing worth surfacing — see Review completeness "
                f"for exactly what was and was not examined.")
    first = findings[0]
    loc = (f"{first['path']}:{first['line_start']}"
           if first["path"] else "the change")
    s = (f"Codatiel's review of {where} surfaced {len(findings)} "
         f"candidates with verified code citations; "
         f"{verified} {'is' if verified == 1 else 'are'} corroborated, the "
         f"rest need{'' if verified else 's'} a human verdict "
         f"before acting. ")
    if len(findings) == 1:
        s += f"Start with {loc}: {first['title']}."
    else:
        s += (f"Start with the first item below ({loc}: "
              f"{first['title']}); the remainder are ordered by review "
              f"priority.")
    if isinstance(sem, dict) and (
            sem.get("coverage") != "full" or sem.get("skipped")):
        s += (" Duplication examination was partial, so unexamined code "
              "may still contain reuse opportunities.")
    pubs = sem.get("published", []) if sem else []
    s += group
    return s


# Human glosses for stable internal reason codes. Presentation only:
# the stored code is always shown alongside so nothing is hidden.
_REASON_GLOSSARY = {
    "terra_no_lead": "the review model declined to propose this reuse",
    "deterministic_drop": "a deterministic overlap check ruled it out",
    "duplicate_of_review": "the ordinary review already covers it",
    "mirror_of_published": "the same pair published in the other direction",
    "delta pair budget exhausted": "pair budget reached before it",
    "experimental pair budget": "held back by the explicit experiment limit",
    "budget_stop": "stopped by the spending guard before inference",
    "retrieval failed": "candidate lookup failed for it",
    "tree unavailable": "source tree unavailable for it",
}


def _gloss_reason(reason: str) -> str:
    reason = reason or ""
    for code, gloss in _REASON_GLOSSARY.items():
        if code in reason:
            if reason.strip() == code:
                return gloss
            return f"{reason} ({gloss})"
    if reason.startswith("target selection budget"):
        return (f"{reason} (never became a candidate; alphabetical "
                "truncation, not a quality judgment)")
    return reason


# -- shared rendering helpers -------------------------------------------

def _loc_text(f: dict) -> str:
    if not f["path"]:
        return "location unknown"
    loc = f["path"]
    if f["line_start"]:
        loc += f":{f['line_start']}"
        if f["line_end"] and f["line_end"] != f["line_start"]:
            loc += f"-{f['line_end']}"
    if f["revision"]:
        loc += f" ({f['revision']})"
    return loc


# -- plain text ----------------------------------------------------------

def render_text(model: dict) -> str:
    L: list[str] = []
    L.append(f"Codatiel review: {model['repo_label']}"
             + (f"#{model['pr_number']}" if model["pr_number"] else ""))
    if model["pr_url"]:
        L.append(model["pr_url"])
    L.append(f"Reviewed commit: {model['head_sha'][:12]} "
             f"(base {model['base_sha'][:12]}) | "
             f"status: {model['review_status']} | "
             f"{model['n_findings']} findings "
             f"({model['n_verified']} corroborated)")
    L.append("")
    L.append("Overview")
    L.append(model["overview"])
    L.append("")
    if model["findings"]:
        L.append("Findings (in priority order)")
        for f in model["findings"]:
            L.append("")
            L.append(f"{f['n']}. {f['title']}")
            loc = _loc_text(f)
            L.append(f"   {_link_text(loc, f['url'])}")
            L.append(f"   Problem: {f['problem'] or '(not stated)'}")
            L.append(f"   Status: {f['verification']}")
            if f["evidence"]:
                L.append("   Supporting evidence (context for the claim; "
                         "the claim itself concerns the location above):")
                for e in f["evidence"]:
                    if e.get("missing"):
                        L.append(f"   - citation {e['id']} is referenced "
                                 "but missing from the run evidence")
                        continue
                    ref = f"{e['path'] or '?'}"
                    if e.get("line_start"):
                        ref += f":{e['line_start']}"
                    L.append(f"   - [{e['id']}] {ref}: {e['excerpt']}"
                             + (" …" if e.get("truncated") else ""))
                if f["evidence_total"] > len(f["evidence"]):
                    L.append(f"   (+{f['evidence_total'] - len(f['evidence'])} "
                             "further citations in the run evidence — see "
                             "the diagnostics reference at the end)")
            L.append(f"   Reference {f['finding_id']} (quote this ID if you "
                     "rate this finding) — confirm or refute "
                     "against the cited lines before changing code.")
    else:
        L.append("Findings: none surfaced.")
    sem = model.get("semdup") or {}
    if sem.get("present"):
        L.append("")
        L.append(f"Possible existing functionality (duplication check "
                 f"finished, {sem['coverage_word']} coverage, {sem['spend']})")
        L.append(sem["state"])
        for p in sem["published"]:
            L.append("")
            L.append(f"- {p['target_ref']} `{p['target_symbol']}` may reuse "
                     f"{p['counterpart_ref']} `{p['counterpart_symbol']}` "
                     f"[{p['decision']}]")
            if p["comment"]:
                L.append(f"  {p['comment']}")
            if p["differences"] and p["differences"] != p["comment"]:
                L.append(f"  Behavioural differences to preserve: "
                         f"{p['differences']}")
        if sem["suppressed"]:
            L.append(f"Dismissed duplication candidates "
                     f"({len(sem['suppressed'])}):")
            for s in sem["suppressed"][:6]:
                L.append(f"- {s['target']} -> {s['counterpart']}: "
                         f"{_gloss_reason(s['reason'])}"
                         + (f" ({s['detail']})" if s["detail"] else ""))
        if sem["skipped"]:
            L.append(f"Not examined for duplication "
                     f"({len(sem['skipped'])}):")
            for s in sem["skipped"][:6]:
                L.append(f"- {s['unit']}: {_gloss_reason(s['reason'])}")
    L.append("")
    L.append("Review completeness")
    mode = model["review_mode"]
    L.append(f"- Ordinary review: {model['review_status']}"
             + (f" ({mode})" if mode else "")
             + f", model {model['model']}, cost ${model['cost_usd']:.4f}.")
    if not sem.get("present"):
        L.append("- Duplication check: did not run for this review.")
    L.append("- Model claims are unverified until human-rated; the "
             "evidence excerpts above were re-read from the reviewed "
             "commit (head).")
    L.append(f"- Diagnostics reference: run {model['run_id']} "
             "(operator-held run bundle).")
    return "\n".join(L) + "\n"


def _link_text(text: str, url: str | None) -> str:
    if url:
        return f"{text} <{url}>"
    return text


# -- HTML -----------------------------------------------------------------

_CSS = ("body{margin:0;padding:0;background:#f6f7f9;}"
        "div.wrap{max-width:640px;margin:0 auto;background:#ffffff;"
        "font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;"
        "font-size:15px;line-height:1.55;color:#1c2333;padding:0 0 8px 0;}"
        "div.pad{padding:20px 22px;}"
        "h1{font-size:20px;margin:0 0 4px 0;}"
        "h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;"
        "color:#5b6478;margin:26px 0 10px 0;}"
        "h3{font-size:16px;margin:0 0 4px 0;}"
        "p.meta{color:#5b6478;margin:2px 0;}"
        "p.ov{margin:8px 0;}"
        "div.find{border:1px solid #dfe3ec;border-left:4px solid #b98a1d;"
        "border-radius:6px;padding:12px 14px;margin:12px 0;background:#fffdf5;}"
        "div.find.corr{border-left-color:#2f7d4f;background:#f7fcf8;}"
        "p.lab{font-size:12.5px;text-transform:uppercase;letter-spacing:.05em;"
        "color:#5b6478;margin:10px 0 2px 0;}"
        "p.body{margin:2px 0 0 0;}"
        "pre.ev{background:#f1f3f7;border-radius:4px;padding:8px 10px;"
        "font-size:13px;overflow-x:auto;white-space:pre-wrap;"
        "word-wrap:break-word;margin:6px 0;}"
        "a{color:#1a56db;}"
        "div.sdup{border:1px solid #dfe3ec;border-radius:6px;"
        "padding:12px 14px;margin:12px 0;background:#fbfcff;}"
        "div.foot{border-top:1px solid #e4e7ee;margin-top:18px;"
        "padding-top:10px;color:#5b6478;font-size:13px;}"
        "span.tag{display:inline-block;font-size:12px;color:#5b6478;"
        "border:1px solid #dfe3ec;border-radius:10px;padding:1px 8px;"
        "margin-right:6px;}")


def _esc(s: Any) -> str:
    return _html.escape("" if s is None else str(s), quote=True)


def _link_html(text: str, url: str | None) -> str:
    if url:
        return f'<a href="{_esc(url)}">{_esc(text)}</a>'
    return _esc(text)


def render_html(model: dict) -> str:
    h: list[str] = [f"<!DOCTYPE html><html lang=\"en\"><head>"
                    f"<meta charset=\"utf-8\">"
                    f"<meta name=\"viewport\" "
                    f"content=\"width=device-width,initial-scale=1\">"
                    f"<title>{_esc('Codatiel review')}</title>"
                    f"<style>{_CSS}</style></head>"
                    f"<body><div class=\"wrap\"><div class=\"pad\">"]
    title = (f"Codatiel review: {_esc(model['repo_label'])}"
             + (f"#{model['pr_number']}" if model["pr_number"] else ""))
    h.append(f"<h1>{title}</h1>")
    if model["pr_url"]:
        h.append(f"<p class=\"meta\">{_link_html(model['pr_url'], model['pr_url'])}</p>")
    h.append(f"<p class=\"meta\">Reviewed commit "
             f"<code>{_esc(model['head_sha'][:12])}</code> "
             f"(base <code>{_esc(model['base_sha'][:12])}</code>) · "
             f"status: {_esc(model['review_status'])} · "
             f"{model['n_findings']} "
             f"{'finding' if model['n_findings'] == 1 else 'findings'} "
             f"({model['n_verified']} corroborated)</p>")
    h.append("<h2>Overview</h2>")
    h.append(f"<p class=\"ov\">{_esc(model['overview'])}</p>")
    if model["findings"]:
        h.append("<h2>Findings (in priority order)</h2>")
        for f in model["findings"]:
            cls = "find corr" if f["verification"].startswith(
                "Corroborated") else "find"
            h.append(f"<div class=\"{cls}\"><h3>{f['n']}. "
                     f"{_esc(f['title'])}</h3>")
            h.append(f"<p class=\"body\">"
                     f"{_link_html(_loc_text(f), f['url'])}"
                     f"</p>")
            h.append(f"<p class=\"body\"><span class=\"tag\">"
                     f"{_esc(f['detector'] or 'review')}</span>"
                     f"<span class=\"tag\">{_esc(f['finding_id'])}</span></p>")
            h.append("<p class=\"lab\">Problem</p>")
            h.append(f"<p class=\"body\">"
                     f"{_esc(f['problem']) or '(not stated)'}</p>")
            h.append("<p class=\"lab\">Review status</p>")
            h.append(f"<p class=\"body\">{_esc(f['verification'])}</p>")
            if f["evidence"]:
                h.append("<p class=\"lab\">Supporting evidence (context for "
                         "the claim; the claim itself concerns the location "
                         "above)</p>")
                for e in f["evidence"]:
                    if e.get("missing"):
                        h.append(f"<p class=\"body\">Citation "
                                 f"<code>{_esc(e['id'])}</code> is referenced "
                                 f"but missing from the run evidence.</p>")
                        continue
                    ref = e["path"] or "?"
                    if e.get("line_start"):
                        ref += f":{e['line_start']}"
                    h.append(f"<p class=\"body\"><code>{_esc(e['id'])}</code> "
                             f"{_esc(ref)}</p>")
                    h.append(f"<pre class=\"ev\">{_esc(e['excerpt'])}"
                             + (" …" if e.get("truncated") else "")
                             + "</pre>")
                if f["evidence_total"] > len(f["evidence"]):
                    h.append(f"<p class=\"body\">+"
                             f"{f['evidence_total'] - len(f['evidence'])} "
                             f"further citations in the run evidence (see "
                             f"the diagnostics reference at the end).</p>")
            h.append(f"<p class=\"body\">Reference "
                     f"<code>{_esc(f['finding_id'])}</code> (quote this ID if you "
                     f"rate this finding) — confirm or refute "
                     f"against the cited lines before changing code.</p>")
            h.append("</div>")
    else:
        h.append("<h2>Findings</h2><p class=\"body\">None surfaced.</p>")
    sem = model.get("semdup") or {}
    if sem.get("present"):
        h.append("<h2>Possible existing functionality</h2>")
        h.append(f"<div class=\"sdup\"><p class=\"body\">Duplication check "
                 f"finished, {_esc(sem['coverage_word'])} coverage, "
                 f"{_esc(sem['spend'])}. "
                 f"{_esc(sem['state'])}</p>")
        for p in sem["published"]:
            h.append(f"<p class=\"body\"><strong>{_esc(p['target_ref'])} "
                     f"<code>{_esc(p['target_symbol'])}</code></strong> may "
                     f"reuse <strong>{_esc(p['counterpart_ref'])} "
                     f"<code>{_esc(p['counterpart_symbol'])}</code></strong> "
                     f"[{_esc(p['decision'])}]</p>")
            if p["comment"]:
                h.append(f"<p class=\"body\">{_esc(p['comment'])}</p>")
            if p["differences"] and p["differences"] != p["comment"]:
                h.append(f"<p class=\"body\">Behavioural differences to "
                         f"preserve: {_esc(p['differences'])}</p>")
        if sem["suppressed"]:
            h.append(f"<p class=\"lab\">Dismissed candidates "
                     f"({len(sem['suppressed'])})</p>")
            for s in sem["suppressed"][:6]:
                h.append(f"<p class=\"body\">— {_esc(s['target'])} → "
                         f"{_esc(s['counterpart'])}: "
                         f"{_esc(_gloss_reason(s['reason']))}"
                         + (f" ({_esc(s['detail'])})" if s["detail"] else "")
                         + "</p>")
        if sem["skipped"]:
            h.append(f"<p class=\"lab\">Not examined for duplication "
                     f"({len(sem['skipped'])})</p>")
            for s in sem["skipped"][:6]:
                h.append(f"<p class=\"body\">— {_esc(s['unit'])}: "
                         f"{_esc(_gloss_reason(s['reason']))}</p>")
        h.append("</div>")
    h.append("<h2>Review completeness</h2><div class=\"foot\">")
    mode = model["review_mode"]
    h.append(f"<p class=\"body\">Ordinary review: "
             f"{_esc(model['review_status'])}"
             + (f" ({_esc(mode)})" if mode else "")
             + f", model {_esc(model['model'])}, cost "
             f"${model['cost_usd']:.4f}.</p>")
    if not sem.get("present"):
        h.append("<p class=\"body\">Duplication check: did not run for "
                 "this review.</p>")
    h.append("<p class=\"body\">Model claims are unverified until "
             "human-rated; the evidence excerpts above were re-read from "
             "the reviewed commit (head).</p>")
    h.append(f"<p class=\"body\">Diagnostics reference: run "
             f"<code>{_esc(model['run_id'])}</code> (operator-held run "
             f"bundle).</p>")
    h.append("</div></div></div></body></html>")
    return "".join(h)


# -- Markdown (future PR comments; NOT wired to GitHub) --------------------

def render_markdown(model: dict) -> str:
    L: list[str] = []
    head = (f"# Codatiel review: {model['repo_label']}"
            + (f"#{model['pr_number']}" if model["pr_number"] else ""))
    L.append(head)
    L.append("")
    if model["pr_url"]:
        L.append(model["pr_url"])
    L.append(f"Reviewed commit `{model['head_sha'][:12]}` "
             f"(base `{model['base_sha'][:12]}`) · "
             f"status: {model['review_status']} · "
             f"{model['n_findings']} findings "
             f"({model['n_verified']} corroborated)")
    L.append("")
    L.append("## Overview")
    L.append("")
    L.append(model["overview"])
    L.append("")
    if model["findings"]:
        L.append("## Findings (in priority order)")
        for f in model["findings"]:
            L.append("")
            L.append(f"### {f['n']}. {f['title']}")
            L.append("")
            loc = _loc_text(f)
            L.append(f"[{loc}]({f['url']})" if f["url"] else f"`{loc}`")
            L.append("")
            L.append(f"**Problem:** {f['problem'] or '(not stated)'}")
            L.append("")
            L.append(f"**Review status:** {f['verification']}")
            if f["evidence"]:
                L.append("")
                L.append("**Supporting evidence** (context for the claim; "
                         "the claim itself concerns the location above):")
                for e in f["evidence"]:
                    if e.get("missing"):
                        L.append(f"- citation `{e['id']}` is referenced "
                                 "but missing from the run evidence")
                        continue
                    ref = e["path"] or "?"
                    if e.get("line_start"):
                        ref += f":{e['line_start']}"
                    L.append(f"- `{e['id']}` {ref}: {e['excerpt']}"
                             + (" …" if e.get("truncated") else ""))
            L.append("")
            L.append(f"Reference `{f['finding_id']}` (quote this ID if you "
                     "rate this finding) — confirm or refute "
                     "against the cited lines before changing code.")
    else:
        L.append("## Findings")
        L.append("")
        L.append("None surfaced.")
    sem = model.get("semdup") or {}
    if sem.get("present"):
        L.append("")
        L.append("## Possible existing functionality")
        L.append("")
        L.append(f"Duplication check finished, {sem['coverage_word']} "
                 f"coverage, {sem['spend']}. {sem['state']}")
        for p in sem["published"]:
            L.append("")
            L.append(f"- {p['target_ref']} `{p['target_symbol']}` may reuse "
                     f"{p['counterpart_ref']} `{p['counterpart_symbol']}` "
                     f"[{p['decision']}]")
            if p["comment"]:
                L.append(f"  {p['comment']}")
            if p["differences"] and p["differences"] != p["comment"]:
                L.append(f"  Behavioural differences to preserve: "
                         f"{p['differences']}")
        if sem["suppressed"]:
            L.append("")
            L.append(f"Dismissed candidates ({len(sem['suppressed'])}):")
            for s in sem["suppressed"][:6]:
                L.append(f"- {s['target']} → {s['counterpart']}: "
                         f"{_gloss_reason(s['reason'])}")
        if sem["skipped"]:
            L.append("")
            L.append(f"Not examined for duplication ({len(sem['skipped'])}):")
            for s in sem["skipped"][:6]:
                L.append(f"- {s['unit']}: {s['reason']}")
    L.append("")
    L.append("## Review completeness")
    L.append("")
    mode = model["review_mode"]
    L.append(f"- Ordinary review: {model['review_status']}"
             + (f" ({mode})" if mode else "")
             + f", model {model['model']}, cost ${model['cost_usd']:.4f}.")
    if not sem.get("present"):
        L.append("- Duplication check: did not run for this review.")
    L.append("- Model claims are unverified until human-rated; the "
             "evidence excerpts above were re-read from the reviewed "
             "commit (head).")
    L.append(f"- Diagnostics reference: run `{model['run_id']}` "
             "(operator-held run bundle).")
    return "\n".join(L) + "\n"
