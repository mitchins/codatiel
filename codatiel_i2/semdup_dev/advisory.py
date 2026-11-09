"""Evidence-backed duplication advisories (SEMDUP-03, DEV-only, report-only).

The engine's definitive findings are conservative: genuine overlaps come
back as `investigate` whenever the challenger cannot verify the claim —
most often because the investigation target's own text is absent from the
challenge evidence (Phase 1: 13/13 records). An `investigate` record is
neither a success nor a failure; it is an unverified suspicion.

This layer adds an outward-facing classification WITHOUT rewriting the
engine's verdict (kept separately in each record):

- possible_overlap: a concrete, inspectable reason to suspect duplication.
  Both implementations were read, anchors resolve, shared behaviour is
  described, differences and uncertainty are disclosed, and the suggested
  next step is an investigation — never a mandated refactor.
- likely_duplication: the post-challenge evidence strongly supports reuse.
  Still advisory. Never a prescribed refactor.
- suppressed: no developer-facing alert. Evidence is incomplete, the pair
  is ambiguous, a guard fired, coverage is not full, or the overlap claim
  cannot be anchored. Suppressions are recorded with reasons (auditability),
  not shown to developers.

Trust rules (all must hold for any advisory):
- retrieval_coverage is exactly "full" (never a reduced/deferred launder),
- the counterpart is a real examined unit (full symbol, not a #region
  fragment, not an oracle fixture, not the target itself),
- both full texts resolve in the pinned merged head-view DB and are
  non-empty (this deterministically repairs the Phase-1 evidence gap:
  the layer fetches the target text the challenger never saw),
- no wrapper/use or ownership signal,
- the arbiter's direction supports overlap (prelim reuse/extract, or a
  single unambiguous pair with a clean challenge).

Hard negatives: wrapper/use, keep_separate prelims, divergent prelims with
named behavioural incompatibilities (e.g. HALF_UP vs HALF_EVEN), and
multi-candidate ambiguity can never become likely_duplication, and most
can never become advisories at all.

No model calls. No network. Deterministic: same record + same merged DB
=> byte-identical advisory output (no timestamps in emitted objects).
Failure degrades to no advisory, never to a fabricated one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

OVERLAP_PRELIMS = ("reuse_existing", "extract_shared_part")
EXCERPT_LINES = 30


def _is_oracle(path: str) -> bool:
    return path == "oracles" or path.startswith("oracles/")


def _is_fragment(symbol: str) -> bool:
    return "#region" in symbol


def validate_anchor(db_path: Path, path: str, symbol: str) -> dict:
    """Resolve (path, symbol) exactly in the pinned merged head-view DB."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT path, symbol, start_line, end_line, text FROM units "
                "WHERE path=? AND symbol=?", (path, symbol)).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - unresolvable => suppress
        return {"ok": False, "reason": f"anchor DB unreadable: {type(exc).__name__}"}
    rows = [r for r in rows if (r[4] or "").strip()]
    if not rows:
        return {"ok": False, "reason": "anchor not found or empty in reviewed revision"}
    if len(rows) > 1:
        return {"ok": False, "reason": "anchor ambiguous in reviewed revision"}
    r = rows[0]
    return {"ok": True, "path": r[0], "symbol": r[1],
            "start_line": r[2], "end_line": r[3], "text": r[4]}


def _existing_full_units(finalists: list, target_id: str) -> list[str]:
    out = []
    for f in finalists or []:
        if f == target_id:
            continue
        p, _, s = f.partition("::")
        if _is_oracle(p) or _is_fragment(s) or not s.strip():
            continue
        out.append(f)
    return out


def _shared_behavior(record: dict) -> str:
    """Describe the suspected shared behaviour without inventing any.

    Only the engine's own proposed interface is quoted; otherwise the
    advisory states the evidence level honestly.
    """
    pb = record.get("proposed_boundary") or {}
    iface = (pb.get("interface") or "").strip()
    if iface and iface.lower() != "investigate":
        return f"Investigator-described shared responsibility: {iface}"
    return ("Retrieval and investigation both surfaced this existing "
            "implementation as the closest match; no established behavioural "
            "conflict was found in the examined evidence.")


def _differences(record: dict) -> list[str]:
    seen: list[str] = []
    pools = [record.get("material_differences") or [],
             ((record.get("challenge") or {}).get("fatal_claims") or [])]
    for pool in pools:
        for src in pool:
            s = str(src).strip()
            if s and s not in seen:
                seen.append(s[:600])
    return seen[:6]


def _question(category: str, differences: list[str]) -> str:
    if category == "likely_duplication":
        return ("Worth checking: can the new code reuse or extend the "
                "existing implementation instead of carrying a duplicate?")
    if differences:
        return ("Worth checking: is the difference below intentional? If "
                "so, can the existing implementation be extended — or "
                "should the new code stay separate?")
    return ("Worth checking: does the existing implementation already "
            "cover this behaviour?")


def _uncertainty(record: dict, coverage: str) -> str:
    ch = record.get("challenge") or {}
    return (
        "Uncertainty: this is a suspicion, not a proof of equivalence. "
        f"Engine disposition is {record.get('status')}/"
        f"{record.get('recommended_action')} (preserved separately); "
        f"challenge validation is {ch.get('validation_level')}; "
        f"retrieval coverage is {coverage}. "
        "Model verdicts on this detector vary run to run; the pair above "
        "recurred in the stored evidence even where the verdict did not."
    )


def _provenance(record: dict, coverage: str) -> dict:
    prov = record.get("provenance") or {}
    return {
        "detector": record.get("detector"),
        "engine_sha": prov.get("engine_sha"),
        "investigator_model": (prov.get("investigator") or {}).get("requested_model"),
        "served_model_verified": (prov.get("served_model_check") or {}).get("verified"),
        "retrieval_coverage": coverage,
        "repo": record.get("repo"), "pr": record.get("pr"),
        "base_rev": record.get("base_rev"), "head_rev": record.get("head_rev"),
        "delta_fingerprint": record.get("delta_fingerprint"),
        "preliminary_action": record.get("preliminary_action"),
        "recommended_action": record.get("recommended_action"),
        "relationship": record.get("relationship"),
    }


def _excerpt(text: str) -> str:
    lines = text.splitlines()
    cut = lines[:EXCERPT_LINES]
    s = "\n".join(cut)
    if len(lines) > EXCERPT_LINES:
        s += f"\n… [{len(lines) - EXCERPT_LINES} more lines in reviewed revision]"
    return s


def _suppressed(record: dict, reason: str, detail: str = "") -> dict:
    tgt = record.get("target") or {}
    return {"suppressed": True, "category": "suppressed",
            "target": {"path": tgt.get("path"), "symbol": tgt.get("symbol")},
            "reason": reason, "detail": detail[:300]}


def classify_record(record: dict, merged_db: Path, coverage: str) -> dict:
    """Classify one engine record as likely/possible/suppressed. No I/O
    beyond reading the pinned merged DB for anchor validation."""
    tgt = record.get("target") or {}
    tpath, tsym = tgt.get("path"), tgt.get("symbol")
    if record.get("status") == "deferred" or not tpath or not tsym:
        return _suppressed(record, "status_not_actionable",
                           f"status={record.get('status')}")
    if coverage != "full":
        return _suppressed(record, "coverage_not_full", str(coverage)[:120])
    ev = record.get("evidence") or {}
    guards = ev.get("guards") or {}
    if (guards.get("wrapper_guard") or {}).get("triggered"):
        return _suppressed(record, "wrapper_use",
                           "target delegates to an existing implementation")
    if (guards.get("ownership_guard") or {}).get("fired"):
        return _suppressed(record, "ownership_signal")
    prelim = record.get("preliminary_action")
    finalists = ev.get("finalists") or []
    target_id = f"{tpath}::{tsym}"
    counterparts = [c for c in (record.get("counterparts") or [])
                    if c and c != target_id
                    and not _is_fragment(c.partition("::")[2])
                    and not _is_oracle(c.partition("::")[0])]
    pair_id: str | None = None
    if prelim in OVERLAP_PRELIMS:
        if len(counterparts) != 1:
            return _suppressed(record, "multiple_candidates" if counterparts
                               else "no_candidate",
                               f"prelim={prelim}")
        pair_id = counterparts[0]
    elif prelim == "investigate" and not counterparts:
        cands = _existing_full_units(finalists, target_id)
        if len(cands) != 1:
            return _suppressed(record, "multiple_candidates" if cands
                               else "no_candidate",
                               "single unambiguous pair required")
        pair_id = cands[0]
    else:
        return _suppressed(record, "prelim_no_overlap_claim",
                           f"prelim={prelim}")
    cpath, _, csym = pair_id.partition("::")
    t_anchor = validate_anchor(merged_db, tpath, tsym)
    if not t_anchor.get("ok"):
        return _suppressed(record, "target_unresolved", t_anchor.get("reason", ""))
    c_anchor = validate_anchor(merged_db, cpath, csym)
    if not c_anchor.get("ok"):
        return _suppressed(record, "counterpart_unresolved",
                           c_anchor.get("reason", ""))
    ch = record.get("challenge") or {}
    differences = _differences(record)
    likely = (
        record.get("status") == "finding"
        and record.get("relationship") == "equivalent"
        and not differences
        and ch.get("validation_level") == "characterised"
    )
    category = "likely_duplication" if likely else "possible_overlap"
    return {
        "suppressed": False,
        "category": category,
        "target": {"path": t_anchor["path"], "symbol": t_anchor["symbol"],
                   "start_line": t_anchor["start_line"],
                   "end_line": t_anchor["end_line"]},
        "counterpart": {"path": c_anchor["path"], "symbol": c_anchor["symbol"],
                        "start_line": c_anchor["start_line"],
                        "end_line": c_anchor["end_line"]},
        "rev": record.get("head_rev"),
        "shared_behavior": _shared_behavior(record),
        "differences": differences,
        "question": _question(category, differences),
        "uncertainty": _uncertainty(record, coverage),
        "evidence": {
            "target_text_lines": len(t_anchor["text"].splitlines()),
            "counterpart_text_lines": len(c_anchor["text"].splitlines()),
            "excerpt_target": _excerpt(t_anchor["text"]),
            "excerpt_counterpart": _excerpt(c_anchor["text"]),
            "finalists_examined": len(finalists),
        },
        "provenance": _provenance(record, coverage),
    }


def advisories_for_run(records: list, merged_db: Path, coverage: str,
                       enabled: bool = True) -> list[dict]:
    """Classify every record. Disabled or failed => no advisories (never
    fabricated). Deterministic in, deterministic out."""
    if not enabled:
        return []
    out = []
    for record in records:
        try:
            out.append(classify_record(record, merged_db, coverage))
        except Exception:  # noqa: BLE001 - degrade to no advisory
            out.append(_suppressed(record, "advisory_error"))
    return out


def render_markdown(advisory: dict) -> str:
    """Developer-facing rendering. Suppressed advisories render nothing."""
    if advisory.get("suppressed"):
        return ""
    t = advisory["target"]
    c = advisory["counterpart"]
    head = ("**Likely duplication**" if advisory["category"] == "likely_duplication"
            else "**Possible existing implementation**")
    lines = [
        head,
        "",
        f"New code `{t['path']}::{t['symbol']}` "
        f"(lines {t['start_line']}–{t['end_line']}, rev "
        f"{str(advisory.get('rev'))[:12]}) appears to overlap with existing "
        f"`{c['path']}::{c['symbol']}` "
        f"(lines {c['start_line']}–{c['end_line']}).",
        "",
        advisory["shared_behavior"],
    ]
    if advisory["differences"]:
        lines += ["", "Known differences (do not ignore):"]
        lines += [f"- {d}" for d in advisory["differences"]]
    else:
        lines += ["", "No material differences were established in the examined evidence."]
    lines += ["", advisory["question"], "", advisory["uncertainty"]]
    return "\n".join(lines)
