"""Terra/Luna semantic-duplication path (SEMDUP-04, DEV-only, report-only).

Product pipeline, replacing the sandbox Gemma inference stages while
keeping the deterministic machinery:

- Candidate retrieval: existing deterministic semantic index
  (vendored ``staged.discover`` — union retrieval over channels).
- Evidence: both full implementations resolved exactly from the pinned
  merged head-view DB (repairs the SEMDUP-03 defect where the challenger
  never received the target's own source).
- Terra assessment: one scoped agentic run per candidate pair through
  the canonical SDK runtime (``runtime_sdk.run_review``) with the I2
  revision-bound source tools available for surrounding code, callers
  and tests when required.
- Luna challenge (config B only): only for Terra-eligible pairs; Luna
  receives both implementations plus Terra's proposed overlap and must
  substantiate concrete counterexamples — never a vote on Terra.
- Advisory decision: the unchanged deterministic ``advisory`` layer
  over engine-record-shaped dicts. Engine records are never rewritten
  by this module; it only produces them.

Model routing: none. Two fixed backends (``terra``, ``luna``) resolved
through ``providers``; no new providers, no language expansion.

Cost control: every call is preflight-estimated; the caller-supplied
spend guard refuses new calls past the experiment ceiling. Luna runs
only on advisory-eligible Terra pairs; Luna timeout/failure falls back
to the unchallenged Terra advisory, marked as such.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from codatiel_i2.semdup_dev import SEMDUP_DEV_DETECTOR
from codatiel_i2.semdup_dev import advisory as AV

TERRA_INSTRUCTIONS_VERSION = "semdup-terra-assess-0.2"
LUNA_INSTRUCTIONS_VERSION = "semdup-luna-challenge-0.2"

TERRA_MAX_TURNS = 6
TERRA_TOOL_BUDGET = 8
LUNA_MAX_TURNS = 4
LUNA_TOOL_BUDGET = 6
MAX_PAIRS_PER_TARGET = 3

# Conservative output allowance (tokens) for preflight estimates.
EST_OUTPUT_TOKENS = 1500

TERRA_RELATIONSHIPS = ("equivalent", "shared_part", "diverged",
                       "related_only", "unknown")
OVERLAP_RELATIONSHIPS = ("equivalent", "shared_part")
ACTIONABLE_RELATIONSHIPS = ("equivalent", "shared_part")

TERRA_INSTRUCTIONS = """You are assessing one candidate pair for a semantic code-duplication detector. This is not a general code review and not a repository-wide sweep: examine ONLY the new/changed implementation and the existing implementation given below, plus any surrounding code, callers or tests you pull with your tools when required to judge the overlap.

Decide:
1. Whether the two implementations have overlapping responsibilities (shared behaviour worth a developer's attention). A candidate can be useful without being interchangeable.
2. What functionality appears shared.
3. The concrete shared component: the specific reusable piece, named precisely.
4. Whether the opportunity is direct reuse (substantially interchangeable) or partial extraction (a shared component inside implementations that are not interchangeable as wholes), or neither.
5. What material behavioural or contractual differences exist.
6. Whether the relationship warrants a developer-facing advisory.

Also report two specific checks:
- wrapper_or_delegation: true when the new code merely calls/delegates to the existing implementation (or vice versa) rather than independently re-implementing it. A wrapper call is not a duplicate.
- ownership_lifecycle_difference: true when the two live under clearly different ownership, lifecycle or stability contracts (e.g. generated code vs hand-written, vendored vs first-party, test fixture vs production) that would make consolidation misleading.

Strength guidance: "likely" only when the implementations are substantively interchangeable for the shared responsibility and you found no material difference; otherwise "possible". When in doubt, say so in uncertainty.

Respond with exactly one JSON object, no prose:
{"overlap": boolean,
 "relationship": "equivalent|shared_part|diverged|related_only|unknown",
 "strength": "likely|possible|none",
 "shared_behavior": "one or two sentences naming the shared responsibility",
 "shared_component": "the concrete reusable piece, or \"\" when there is none",
 "opportunity": "direct_reuse|partial_extraction|none",
 "differences": ["concrete difference, or a stated uncertainty when unknown"],
 "wrapper_or_delegation": boolean,
 "ownership_lifecycle_difference": boolean,
 "uncertainty": "what you could not verify"}"""

LUNA_INSTRUCTIONS = """You are challenging one proposed duplication overlap for a semantic code-duplication detector. This is not a general code review and not a repository-wide sweep: consider ONLY the two implementations below and the proposed overlap.

Your central question: after accounting for the differences, is there still a concrete shared implementation, responsibility or reusable component worth bringing to the developer's attention?

Investigate the proposed relationship. Distinguish:
- Overlap survives: a meaningful shared component exists, possibly with differences.
- Overlap refuted: the evidence demonstrates the implementations provide no useful common component, or the proposed relationship is misleading.
- Uncertain: insufficient evidence to establish either conclusion.

Separately record concrete reasons direct replacement would be unsafe. A difference in behaviour is not automatically an overlap refutation: a difference refutes only when it changes the shared component's own observable behaviour for ordinary inputs. Differences in surrounding handling, extra features, failure modes outside the component, or unusual inputs restrict how the code could be reused — record them, do not refute on them.

Evidence rules: the challenge must produce concrete, source-backed observations, not agreement or disagreement. An unsupported statement about ownership, generated code, tests or vendoring is not a demonstrated counterexample: put it in unknowns, never in fatal_objections. Use kind "behavioral" only for differences in the shared component's observable behaviour; anything else is "action-only". Use your source tools when the comparison depends on code or data outside the two implementations shown below.

Respond with exactly one JSON object, no prose:
{"verdict": "survives|refuted|uncertain",
 "shared_component": "the concrete surviving component, or \"\" when none survives",
 "core_comparable": boolean,
 "replacement_unsafe_reasons": ["concrete reason direct replacement would be unsafe"],
 "fatal_objections": [{"claim": "concrete counter-claim", "span": "path:lines of the cited evidence", "kind": "behavioral|action-only", "resolved_by_proposal": boolean}],
 "unknowns": ["material question you could not resolve from the evidence"],
 "responsibility_holds": boolean}"""


def instructions_id(name: str, text: str) -> str:
    """Content-derived instruction id (same convention as service)."""
    return f"{name}@{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


TERRA_INSTRUCTIONS_ID = instructions_id(
    "semdup-terra-assess-0.2", TERRA_INSTRUCTIONS)
LUNA_INSTRUCTIONS_ID = instructions_id(
    "semdup-luna-challenge-0.2", LUNA_INSTRUCTIONS)

OPPORTUNITIES = ("direct_reuse", "partial_extraction", "none")
OPPORTUNITY_PRELIM = {"direct_reuse": "reuse_existing",
                      "partial_extraction": "extract_shared_part",
                      "none": "keep_separate"}


# --------------------------------------------------------------------------
# Evidence construction (Phase 2 repair — provider-independent).
# --------------------------------------------------------------------------

EVIDENCE_ERRORS = ("target_unresolved", "counterpart_unresolved",
                   "fragment_source", "stale_source")


def build_pair_evidence(merged_db: Path, target: dict, counterpart: dict,
                        expected_sha: dict[str, str] | None = None) -> dict:
    """Resolve BOTH full implementations from the pinned merged DB.

    This is the shared assessment/challenge boundary fix: the SEMDUP-03
    audit found the challenger received the target's own source in 0/13
    records. Every consumer (Terra, Luna, advisory renderer) gets both
    texts from here or nothing at all — no model call may proceed on a
    partial pair.

    Exact (path, symbol) resolution only: the basename/symbol fallbacks
    that caused misresolved evidence elsewhere are refused here.
    """
    expected_sha = expected_sha or {}
    sides = {}
    for label, unit in (("target", target), ("counterpart", counterpart)):
        path, symbol = unit.get("path"), unit.get("symbol")
        if not path or not symbol:
            return {"ok": False, "code": f"{label}_unresolved",
                    "detail": "missing path/symbol"}
        if "#region" in symbol:
            return {"ok": False, "code": "fragment_source",
                    "detail": f"{label} {path}::{symbol} is a region fragment"}
        anchor = AV.validate_anchor(merged_db, path, symbol)
        if not anchor.get("ok"):
            return {"ok": False, "code": f"{label}_unresolved",
                    "detail": anchor.get("reason", "")}
        text = anchor["text"]
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        want = (expected_sha.get(label) or "")
        if want and want != sha:
            return {"ok": False, "code": "stale_source",
                    "detail": f"{label} text sha {sha[:12]} != expected {want[:12]}"}
        sides[label] = {
            "path": anchor["path"], "symbol": anchor["symbol"],
            "start_line": anchor["start_line"], "end_line": anchor["end_line"],
            "text": text, "sha256": sha,
        }
    pair_sha = hashlib.sha256(
        (sides["target"]["sha256"] + sides["counterpart"]["sha256"]).encode()
    ).hexdigest()
    return {"ok": True, "target": sides["target"],
            "counterpart": sides["counterpart"], "pair_sha256": pair_sha}


def render_terra_input(evidence: dict, head_rev: str,
                       target_kind: str = "introduced_or_changed") -> str:
    """Assemble the exact model input: both full sources, pinned revision."""
    t, c = evidence["target"], evidence["counterpart"]
    return "\n".join([
        f"Reviewed revision: {head_rev} (both sources below are pinned to this revision).",
        f"Change role: the NEW/CHANGED implementation is the first one ({target_kind}).",
        "",
        f"## New/changed implementation: {t['path']}::{t['symbol']} "
        f"(lines {t['start_line']}-{t['end_line']})",
        "```", t["text"], "```",
        "",
        f"## Existing implementation: {c['path']}::{c['symbol']} "
        f"(lines {c['start_line']}-{c['end_line']})",
        "```", c["text"], "```",
        "",
        "Assess this pair per your instructions. Use your source tools when "
        "you need surrounding code, callers or tests; both implementations "
        "above are complete and authoritative for this revision.",
    ])


def render_luna_input(evidence: dict, terra: dict) -> str:
    """Challenge input: same sources plus Terra's proposed overlap."""
    t, c = evidence["target"], evidence["counterpart"]
    return "\n".join([
        "## Proposed overlap under challenge",
        f"shared_behavior: {terra.get('shared_behavior', '')}",
        f"relationship: {terra.get('relationship', '')}",
        f"stated differences: {terra.get('differences', [])}",
        "",
        "## Same source evidence (no prior verdict attached)",
        f"### New/changed: {t['path']}::{t['symbol']} "
        f"(lines {t['start_line']}-{t['end_line']})",
        "```", t["text"], "```",
        "",
        f"### Existing: {c['path']}::{c['symbol']} "
        f"(lines {c['start_line']}-{c['end_line']})",
        "```", c["text"], "```",
    ])


# --------------------------------------------------------------------------
# Deterministic retrieval (shared by configs A and B — runs once).
# --------------------------------------------------------------------------

def retrieve_pairs(merged_db: Path, target: dict, emb: dict | None,
                   max_pairs: int = MAX_PAIRS_PER_TARGET) -> dict:
    """Deterministic candidate retrieval for one target (no inference)."""
    from_cod = "codatiel_i2.semdup_vendored.semdup"
    import importlib
    ST = importlib.import_module(f"{from_cod}.staged")
    conn = sqlite3.connect(str(merged_db))
    try:
        tid = ST._unit_row(conn, target["path"], target["symbol"])["id"]
        found = ST.discover(conn, {"file": target["path"],
                                   "symbol": target["symbol"]}, tid, emb=emb)
    finally:
        conn.close()
    pairs: list[dict] = []
    seen: set[str] = set()
    for area in found.get("candidate_areas", []):
        for m in area.get("members", []):
            mid = m.get("id", "")
            p, _, s = mid.partition("::")
            if not mid or mid in seen or mid == f"{target['path']}::{target['symbol']}":
                continue
            if AV._is_oracle(p) or AV._is_fragment(s) or not s.strip():
                continue
            seen.add(mid)
            pairs.append({"path": p, "symbol": s,
                          "via": m.get("provenance", [])})
            if len(pairs) >= max_pairs:
                break
        if len(pairs) >= max_pairs:
            break
    return {"pairs": pairs,
            "coverage": "full" if emb is not None else "reduced",
            "n_union": found.get("n_union", 0),
            "n_dropped_by_cap": found.get("n_dropped_by_cap", 0)}


# --------------------------------------------------------------------------
# Model-call plumbing (injectable for tests; SDK runtime live).
# --------------------------------------------------------------------------

def estimate_call_usd(prompt_chars: int, rate: dict) -> float | None:
    """Conservative preflight estimate for one scoped call."""
    try:
        rate_in = rate.get("input")
        rate_out = rate.get("output")
        if rate_in is None or rate_out is None:
            return None
        return (prompt_chars / 4.0 / 1e6 * float(rate_in)
                + EST_OUTPUT_TOKENS / 1e6 * float(rate_out))
    except (TypeError, ValueError):
        return None


def run_model_call(*, instructions: str, user_input: str, tools: list,
                   model: Any, model_settings: Any, ledger: Any,
                   max_turns: int, ctx: Any = None,
                   call: Callable[..., Any] | None = None) -> dict:
    """One bounded SDK run. Returns final text + tool log, never raises."""
    from codatiel_i2 import runtime_sdk
    fn = call or runtime_sdk.run_review
    t0 = time.monotonic()
    try:
        result = fn(instructions=instructions, user_input=user_input,
                    tools=tools, model=model, max_turns=max_turns,
                    model_settings=model_settings, usage_ledger=ledger,
                    session=None)
    except Exception as exc:  # noqa: BLE001 - failure is a recorded outcome
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "tool_calls": ([dict(t) for t in (ctx.tool_log or [])]
                               if ctx is not None else []),
                "wall_s": round(time.monotonic() - t0, 1)}
    tool_log = ([dict(t) for t in (ctx.tool_log or [])]
                if ctx is not None else [])
    if isinstance(result, dict):
        # Test seam: injected calls return plain dicts.
        return {"ok": bool(result.get("ok", True)),
                "final_text": str(result.get("final_text", "") or ""),
                "tool_log": result.get("tool_calls", tool_log),
                "wall_s": result.get("wall_s",
                                     round(time.monotonic() - t0, 1)),
                "error": str(result.get("error", "") or "")}
    return {"ok": True,
            "final_text": str(getattr(result, "final_output", "") or ""),
            "tool_log": tool_log,
            "wall_s": round(time.monotonic() - t0, 1)}


def scoped_tools(ctx: Any) -> list:
    """Small tool surface for duplication assessment (subset of I2 tools)."""
    from codatiel_i2 import runtime_sdk
    keep = {"read_file", "search_code", "find_callers", "diff_window"}
    out = []
    for tool in runtime_sdk.build_i2_tools(ctx):
        name = getattr(tool, "name", "") or getattr(tool, "__name__", "")
        if name in keep:
            out.append(tool)
    return out


def materialize_checkouts(repo_path: Path, base_sha: str, head_sha: str,
                          work_dir: Path) -> dict:
    """Materialise pinned base/head checkouts for revision-bound tools."""
    work_dir.mkdir(parents=True, exist_ok=True)
    base_dir = work_dir / "base"
    head_dir = work_dir / "head"
    for rev, dest in ((base_sha, base_dir), (head_sha, head_dir)):
        if dest.exists():
            continue
        dest.mkdir(parents=True)
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "archive", rev],
            capture_output=True, check=False, timeout=120)
        if proc.returncode != 0:
            return {"ok": False,
                    "error": f"git archive {rev[:12]}: "
                             f"{proc.stderr.decode()[:200]}"}
        tar = subprocess.run(["tar", "-x", "-C", str(dest)],
                             input=proc.stdout, capture_output=True,
                             check=False, timeout=120)
        if tar.returncode != 0:
            return {"ok": False, "error": "tar extract failed"}
    return {"ok": True, "base_dir": base_dir, "head_dir": head_dir}


def _tolerant_json_loads(body: str) -> tuple[dict, bool]:
    """Strict first; retry after stripping control chars models copy."""
    import re as _re
    try:
        return json.loads(body[body.find("{"):body.rfind("}") + 1]), False
    except (ValueError, TypeError):
        cleaned = _re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]", "", body)
        return json.loads(cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]), True


# --------------------------------------------------------------------------
# Terra assessment.
# --------------------------------------------------------------------------

def validate_terra_output(value: dict) -> list[str]:
    """Schema + coherence checks on a Terra assessment (deterministic)."""
    if not isinstance(value, dict):
        return ["not an object"]
    errors = []
    for field in ("overlap", "relationship", "strength", "shared_behavior",
                  "shared_component", "opportunity",
                  "differences", "wrapper_or_delegation",
                  "ownership_lifecycle_difference", "uncertainty"):
        if field not in value:
            errors.append(f"missing field: {field}")
    if errors:
        return errors
    if not isinstance(value.get("overlap"), bool):
        errors.append("overlap must be boolean")
    if value.get("relationship") not in TERRA_RELATIONSHIPS:
        errors.append(f"bad relationship: {value.get('relationship')!r}")
    if value.get("strength") not in ("likely", "possible", "none"):
        errors.append(f"bad strength: {value.get('strength')!r}")
    if value.get("opportunity") not in OPPORTUNITIES:
        errors.append(f"bad opportunity: {value.get('opportunity')!r}")
    if not isinstance(value.get("differences"), list):
        errors.append("differences must be a list")
    if value.get("overlap"):
        # overlap = shared responsibility worth attention. `diverged`
        # (same responsibility, implementations differ) is coherent here
        # and matches the established taxonomy; `related_only` means no
        # shared responsibility and contradicts overlap=true.
        if value.get("relationship") not in OVERLAP_RELATIONSHIPS + ("diverged",):
            errors.append("overlap=true requires equivalent/shared_part/diverged")
        if value.get("strength") == "likely" and value.get("relationship") \
                not in OVERLAP_RELATIONSHIPS:
            errors.append("likely strength requires equivalent/shared_part")
        if value.get("strength") == "none":
            errors.append("overlap=true requires likely/possible strength")
        if not str(value.get("shared_behavior", "")).strip():
            errors.append("overlap=true requires shared_behavior")
    else:
        if value.get("strength") != "none":
            errors.append("overlap=false requires strength none")
    if value.get("opportunity") != "none" and \
            not str(value.get("shared_component", "")).strip():
        errors.append("a reusable opportunity requires shared_component")
    return errors


def opportunity_of(terra: dict) -> str:
    """Terra's stated opportunity, falling back to the legacy relationship.

    The fallback exists for stored SEMDUP-04 outputs only; live
    assessments carry the explicit field (validated above).
    """
    opp = terra.get("opportunity")
    if opp in OPPORTUNITIES:
        return opp
    return {"equivalent": "direct_reuse",
            "shared_part": "partial_extraction"}.get(
                terra.get("relationship"), "none")


def validate_luna_output(value: dict) -> list[str]:
    """Schema + coherence checks on a Luna challenge (deterministic)."""
    if not isinstance(value, dict):
        return ["not an object"]
    errors = []
    for field in ("verdict", "shared_component", "core_comparable",
                  "replacement_unsafe_reasons", "fatal_objections",
                  "unknowns", "responsibility_holds"):
        if field not in value:
            errors.append(f"missing field: {field}")
    if errors:
        return errors
    if value.get("verdict") not in ("survives", "refuted", "uncertain"):
        errors.append(f"bad verdict: {value.get('verdict')!r}")
    if not isinstance(value.get("core_comparable"), bool):
        errors.append("core_comparable must be boolean")
    if not isinstance(value.get("replacement_unsafe_reasons"), list):
        errors.append("replacement_unsafe_reasons must be a list")
    if not isinstance(value.get("fatal_objections"), list):
        errors.append("fatal_objections must be a list")
    if not isinstance(value.get("unknowns"), list):
        errors.append("unknowns must be a list")
    if value.get("verdict") == "survives" and \
            not str(value.get("shared_component", "")).strip():
        errors.append("survives requires the surviving shared_component")
    if value.get("verdict") == "refuted" and \
            not value.get("fatal_objections") and \
            bool(value.get("responsibility_holds", True)):
        errors.append("refuted requires a demonstrated fatal objection or "
                      "a rejected shared responsibility")
    return errors


def assess_pair(*, evidence: dict, head_rev: str, target_kind: str,
                tools: list, model: Any, model_settings: Any, ledger: Any,
                ctx: Any = None,
                call: Callable[..., Any] | None = None) -> dict:
    """Run Terra on one pair. Returns assessment or a recorded failure."""
    user_input = render_terra_input(evidence, head_rev, target_kind)
    res = run_model_call(instructions=TERRA_INSTRUCTIONS,
                         user_input=user_input, tools=tools, model=model,
                         model_settings=model_settings, ledger=ledger,
                         max_turns=TERRA_MAX_TURNS, ctx=ctx, call=call)
    transcript = {"instructions_id": TERRA_INSTRUCTIONS_ID,
                  "instructions_sha256": hashlib.sha256(
                      TERRA_INSTRUCTIONS.encode()).hexdigest(),
                  "user_input": user_input,
                  "pair_sha256": evidence.get("pair_sha256"),
                  "tool_calls": res.get("tool_calls", []),
                  "final_text": str(res.get("final_text", ""))[:4000],
                  "wall_s": res.get("wall_s")}
    if not res.get("ok"):
        transcript.update({"status": "model_failed",
                           "error": res.get("error", "")})
        return {"ok": False, "transcript": transcript,
                "error": res.get("error", "")}
    try:
        value, _stripped = _tolerant_json_loads(res.get("final_text", ""))
    except (ValueError, TypeError) as exc:
        transcript.update({"status": "unparseable",
                           "final_text": res.get("final_text", "")[:2000]})
        return {"ok": False, "transcript": transcript,
                "error": f"unparseable Terra output: {exc}"}
    errors = validate_terra_output(value)
    if errors:
        transcript.update({"status": "invalid", "errors": errors,
                           "final_text": res.get("final_text", "")[:2000]})
        return {"ok": False, "transcript": transcript,
                "error": "invalid Terra output: " + "; ".join(errors)}
    transcript.update({"status": "assessed"})
    return {"ok": True, "terra": value, "transcript": transcript}


# --------------------------------------------------------------------------
# Luna challenge (config B only) + deterministic reconcile.
# --------------------------------------------------------------------------

def terra_to_assessment(terra: dict, evidence: dict) -> dict:
    """Map a Terra assessment onto the assessment shape `reconcile` needs."""
    t, c = evidence["target"], evidence["counterpart"]
    rel = terra.get("relationship", "unknown")
    action = {"equivalent": "reuse_existing",
              "shared_part": "extract_shared_part"}.get(rel, "investigate")
    shared = str(terra.get("shared_behavior", ""))
    diffs = [str(d) for d in (terra.get("differences") or [])]
    return {
        "responsibility": shared,
        "relationship": rel,
        "action": action,
        "change_scenario": f"new/changed {t['path']}::{t['symbol']}",
        "observed_differences": diffs,
        "member_notes": [
            {"member": {"path": c["path"],
                        "start_line": c["start_line"],
                        "end_line": c["end_line"]},
             "note": shared or c["symbol"]}],
        "proposal": {
            "target": None,
            "interface": shared,
            "preserved": [shared] if shared else [],
            "remain_separate": diffs,
            "benefit": "avoid duplicate maintenance of the shared responsibility",
            "behavior_changes": [],
        },
        "validation": {"level": "source_supported", "unresolved": [],
                       "artifacts": []},
    }


def challenge_packet(evidence: dict) -> dict:
    """Span packet for citation enforcement: exactly the two examined units."""
    t, c = evidence["target"], evidence["counterpart"]
    return {"members": [
        {"path": t["path"], "start_line": t["start_line"],
         "end_line": t["end_line"]},
        {"path": c["path"], "start_line": c["start_line"],
         "end_line": c["end_line"]}],
        "support": []}


def reconcile_v2(assessment: dict, challenge: dict,
                 packet: dict | None = None) -> dict:
    """Evidence-preserving reconciliation (SEMDUP-05).

    A single model disagreement never suppresses a lead. Outcomes:

    - publish: the shared component survives (possibly with disclosed
      differences), or objections are speculative/unresolved while the
      component itself is comparable from the evidence.
    - suppress: Luna demonstrates no useful common component, the core
      behavior cannot be compared from the evidence, or Terra proposed
      no opportunity (handled by the caller via opportunity=none).

    Suppression is expressed as the honest post-challenge state
    (relationship unknown/diverged, action investigate, validation
    unresolved) so the frozen advisory contract suppresses it for the
    right reason. Publication keeps Terra's relationship and carries
    every difference, objection and open question into the advisory.

    `challenge` carries the new verdict fields; for stored SEMDUP-04
    outputs the caller supplies the Phase-1 interpretation
    (luna_verdict, core_comparable) explicitly.
    """
    verdict = challenge.get("luna_verdict", challenge.get("verdict"))
    core_comparable = challenge.get("core_comparable", True)
    fatals = [o for o in (challenge.get("fatal_objections") or [])
              if not o.get("resolved_by_proposal", False)]
    unknowns = list(challenge.get("unknowns") or [])
    holds = bool(challenge.get("responsibility_holds", True))
    unsafe = [str(r) for r in
              (challenge.get("replacement_unsafe_reasons") or [])]

    # Citation enforcement: objections citing evidence outside the
    # examined pair are unsupported and discarded (noted, never fatal).
    discarded: list[str] = []
    if packet is not None and fatals:
        known = {m["path"] for m in packet.get("members", [])} | \
                {s["path"] for s in packet.get("support", [])}
        kept = []
        for objection in fatals:
            span = objection.get("span", "")
            path = span.split(":")[0].strip() if isinstance(span, str) else ""
            if path and path not in known:
                discarded.append(objection.get("claim", "")[:200])
            else:
                kept.append(objection)
        fatals = kept

    notes = {"discarded_unsupported": discarded,
             "luna_verdict": verdict,
             "core_comparable": bool(core_comparable)}
    base_diffs = list(assessment.get("observed_differences") or [])
    fatal_claims = [o.get("claim", "") for o in fatals]
    disclosed = (base_diffs + fatal_claims + unsafe +
                 [f"Open question: {u}" for u in unknowns])

    def _publish(validation_level: str) -> dict:
        out = dict(assessment)
        out["observed_differences"] = disclosed
        validation = dict(out.get("validation") or {})
        validation["level"] = validation_level
        validation["unresolved"] = list(unknowns)
        out["validation"] = validation
        return {"decision": "publish", "assessment": out,
                "fatal_claims": fatal_claims, "unknowns": unknowns,
                "notes": notes}

    def _suppress(reason: str) -> dict:
        out = dict(assessment)
        behavioral = [o.get("claim", "") for o in fatals
                      if o.get("kind") == "behavioral"]
        if behavioral and holds and assessment.get("relationship") in \
                ("equivalent", "shared_part", "diverged"):
            out["relationship"] = "diverged"
        else:
            out["relationship"] = "unknown"
        out["action"] = "investigate"
        out["observed_differences"] = disclosed
        out["validation"] = {"level": "unresolved",
                             "unresolved": (fatal_claims or unknowns or [reason]),
                             "artifacts": []}
        return {"decision": "suppress", "assessment": out,
                "fatal_claims": fatal_claims, "unknowns": unknowns,
                "notes": dict(notes, suppress_reason=reason)}

    if verdict == "survives":
        return _publish("characterised")
    if verdict == "refuted":
        if fatals or not holds:
            return _suppress("luna demonstrated no useful common component")
        # A bare refuted label with no demonstrated basis is
        # unsubstantiated disagreement, not evidence.
        notes["unsubstantiated"] = True
        if core_comparable:
            return _publish("unresolved")
        return _suppress("shared behavior cannot be compared from evidence")
    # uncertain (or unrecognized): speculative/unresolved objections
    # preserve an independently supported lead; an uncomparable core
    # cannot establish one.
    if core_comparable:
        return _publish("unresolved")
    return _suppress("shared behavior cannot be compared from evidence")


def challenge_pair(*, evidence: dict, terra: dict, tools: list, model: Any,
                   model_settings: Any, ledger: Any, ctx: Any = None,
                   call: Callable[..., Any] | None = None) -> dict:
    """Run Luna's challenge on a Terra-eligible pair. Never raises."""
    user_input = render_luna_input(evidence, terra)
    res = run_model_call(instructions=LUNA_INSTRUCTIONS,
                         user_input=user_input, tools=tools, model=model,
                         model_settings=model_settings, ledger=ledger,
                         max_turns=LUNA_MAX_TURNS, ctx=ctx, call=call)
    transcript = {"instructions_id": LUNA_INSTRUCTIONS_ID,
                  "instructions_sha256": hashlib.sha256(
                      LUNA_INSTRUCTIONS.encode()).hexdigest(),
                  "user_input": user_input,
                  "pair_sha256": evidence.get("pair_sha256"),
                  "tool_calls": res.get("tool_calls", []),
                  "final_text": str(res.get("final_text", ""))[:4000],
                  "wall_s": res.get("wall_s")}
    assessment = terra_to_assessment(terra, evidence)
    packet = challenge_packet(evidence)
    if not res.get("ok"):
        transcript.update({"status": "challenge_failed",
                           "error": res.get("error", "")})
        return {"ok": False, "transcript": transcript,
                "assessment": assessment, "challenged": False,
                "fatal_claims": [],
                "error": res.get("error", "")}
    try:
        value, _s = _tolerant_json_loads(res.get("final_text", ""))
    except (ValueError, TypeError) as exc:
        # An unreadable challenge is not evidence against the lead:
        # preserve the Terra advisory, marked unchallenged.
        transcript.update({"status": "challenge_unreadable",
                           "error": f"challenge unreadable: {exc}"})
        return {"ok": False, "transcript": transcript,
                "assessment": assessment, "challenged": False,
                "fatal_claims": [],
                "error": f"challenge unreadable: {exc}"}
    errors = validate_luna_output(value)
    if errors:
        transcript.update({"status": "challenge_invalid", "errors": errors})
        return {"ok": False, "transcript": transcript,
                "assessment": assessment, "challenged": False,
                "fatal_claims": [],
                "error": "challenge rejected: " + "; ".join(errors)}
    result = reconcile_v2(assessment, value, packet)
    transcript.update({"status": "challenged",
                       "luna_verdict": value.get("verdict")})
    return {"ok": True, "transcript": transcript,
            "assessment": result["assessment"],
            "challenged": True,
            "fatal_claims": result["fatal_claims"],
            "unknowns": result["unknowns"],
            "replacement_unsafe": [str(r) for r in
                                   (value.get("replacement_unsafe_reasons")
                                    or [])],
            "reconcile": {"decision": result["decision"],
                          "unknowns": result["unknowns"],
                          "notes": result["notes"]}}


# --------------------------------------------------------------------------
# Record mapping onto the advisory contract.
# --------------------------------------------------------------------------

def _deterministic_wrapper_hit(target_text: str, counterpart_symbol: str) -> bool:
    """Textual call evidence: target calls the counterpart's symbol.

    Same generic mechanism as the sandbox challenger enrichment (a call
    is not an independent re-implementation), computed deterministically
    from the delivered source — not a model hypothesis.
    """
    sym = (counterpart_symbol or "").split("#")[0].split("@")[0].lstrip("$")
    return bool(sym) and f"{sym}(" in (target_text or "")


def to_engine_record(*, repo: str, pr: int, base_rev: str, head_rev: str,
                     from_head: str, delta_fingerprint: str,
                     classification: str, target: dict, evidence: dict,
                     terra: dict, challenge: dict | None,
                     retrieval_coverage: str, provenance: dict) -> dict:
    """Build an advisory-contract record from Terra (+ optional Luna)."""
    t = evidence["target"]
    c = evidence["counterpart"]
    pair_id = f"{c['path']}::{c['symbol']}"
    overlap = bool(terra.get("overlap"))
    rel = terra.get("relationship", "unknown")
    wrapper = bool(terra.get("wrapper_or_delegation")) or \
        _deterministic_wrapper_hit(t["text"], c["symbol"])
    ownership = bool(terra.get("ownership_lifecycle_difference"))
    diffs = [str(d) for d in (terra.get("differences") or [])]
    challenged = bool(challenge and challenge.get("challenged"))
    reconcile = (challenge or {}).get("reconcile") or {}
    if challenge is not None and challenged:
        assessment = challenge["assessment"]
        action = assessment.get("action", "investigate")
        relationship = assessment.get("relationship", "unknown")
        validation = (assessment.get("validation") or {}).get("level")
        fatal_claims = list(challenge.get("fatal_claims") or [])
        surviving = list((assessment.get("validation") or {}).get("unresolved") or [])
        observed = list(assessment.get("observed_differences") or diffs)
    else:
        action = {"equivalent": "reuse_existing",
                  "shared_part": "extract_shared_part"}.get(rel, "investigate") \
            if overlap else "investigate"
        relationship = rel
        validation = "source_supported" if overlap else None
        fatal_claims = []
        surviving = []
        observed = diffs
    opportunity = terra.get("opportunity")
    if opportunity not in OPPORTUNITIES:
        # Legacy stored outputs (SEMDUP-04) carry no explicit
        # opportunity; the caller supplies it via the annotation, else
        # derive conservatively from the relationship.
        opportunity = terra.get("annotated_opportunity") or \
            {"equivalent": "direct_reuse",
             "shared_part": "partial_extraction"}.get(rel, "none")
    if opportunity == "none" or not overlap:
        status, recommendation = "investigate", "investigate"
        prelim = "keep_separate"
    elif challenged and action in ("reuse_existing", "extract_shared_part",
                                  "share_contract"):
        status, recommendation = "finding", action
        prelim = action
    elif challenged:
        status, recommendation = "investigate", "investigate"
        prelim = OPPORTUNITY_PRELIM[opportunity] \
            if relationship in ("equivalent", "shared_part") \
            else "investigate"
    else:
        status, recommendation = "investigate", "investigate"
        prelim = OPPORTUNITY_PRELIM[opportunity]
    record = {
        "detector": SEMDUP_DEV_DETECTOR,
        "status": status,
        "repo": repo, "pr": pr,
        "base_rev": base_rev, "head_rev": head_rev,
        "from_head": from_head, "delta_fingerprint": delta_fingerprint,
        "classification": classification,
        "target": {"path": t["path"], "symbol": t["symbol"],
                   "start_line": t["start_line"], "end_line": t["end_line"]},
        "counterparts": [pair_id],
        "relationship": relationship,
        "recommended_action": recommendation,
        "preliminary_action": prelim,
        "preliminary_published": False,
        "proposed_boundary": {"interface": str(terra.get("shared_behavior", "")),
                              "assessment": "terra"},
        "material_differences": observed,
        "must_remain_separate": [],
        "challenge": {
            "challenged": challenged,
            "fatal_claims": fatal_claims,
            "surviving_objections": surviving,
            "validation_level": validation,
            "luna_status": ("challenged" if challenged
                            else ("challenge_failed_unchallenged"
                                  if challenge is not None else "not_challenged")),
            "luna_verdict": reconcile.get("notes", {}).get("luna_verdict"),
            "reconcile_decision": reconcile.get("decision"),
            "reconcile_notes": reconcile.get("notes", {}),
            "replacement_unsafe_reasons": list(
                (challenge or {}).get("replacement_unsafe") or []),
            "luna_unknowns": list((challenge or {}).get("unknowns") or []),
        },
        "member_notes": [],
        "evidence": {
            "finalists": [pair_id],
            "finalist_truncated": [],
            "retrieval_coverage": retrieval_coverage,
            "guards": {
                "family_guardrail": {"dropped": [], "fired": False},
                "wrapper_guard": {"triggered": wrapper},
                "ownership_guard": {"fired": ownership},
            },
            "verification": {
                "opportunity": opportunity,
                "core_comparable": (reconcile.get("notes", {}).get(
                    "core_comparable", True) if challenged else True),
                "shared_component": str(terra.get("shared_component", ""))[:500],
            },
        },
        "provenance": provenance,
        "timing": {},
        "created_ts": time.time(),
    }
    return record


def deferred_record(*, repo: str, pr: int, base_rev: str, head_rev: str,
                    from_head: str, delta_fingerprint: str,
                    reason: str, detail: str = "",
                    classification: str = "unknown") -> dict:
    """Explicit incomplete/deferred record. Never a clean negative."""
    from codatiel_i2.semdup_dev import findings as F
    return F.deferred_record(
        repo=repo, pr=pr, base_rev=base_rev, head_rev=head_rev,
        from_head=from_head, delta_fingerprint=delta_fingerprint,
        reason=reason, detail=detail, classification=classification)


# --------------------------------------------------------------------------
# Trust: process-claim verification + single-category rendering.
# --------------------------------------------------------------------------

_TOOL_FAILURE_PATTERNS = (
    r"\btools?\b.{0,40}\b(failed|could not|couldn't|unable|unavailable)\b",
    r"\bcheckout\b.{0,40}\b(not match|failed|unavailable|could not)\b",
    r"\bcould not access\b.{0,20}\b(pinned|checkout|revision|source)\b",
    r"\bsource tools?\b.{0,40}\b(failed|could not)\b",
)


def check_process_claims(text: str, tool_calls: list) -> dict:
    """Verify model claims about tool execution against the actual tool log.

    A claim that tools/checkouts failed when the log shows zero tool
    calls is confabulation about the model's own process, not evidence
    about the code. Honest statements of non-use ("were not examined")
    never match: only failure language counts.
    """
    import re as _re
    claimed = any(_re.search(pat, text or "", _re.IGNORECASE)
                  for pat in _TOOL_FAILURE_PATTERNS)
    made = len(tool_calls or [])
    return {"claimed_tool_failure": bool(claimed),
            "tool_calls_made": made,
            "process_claim_unverified": bool(claimed) and made == 0}


def render_simple_advisory(advisory: dict, record: dict | None = None) -> str:
    """Single developer-facing category: Possible existing functionality.

    Internal evidence distinctions stay in the record; the public report
    names the shared piece, discloses differences and replacement risks,
    and states what is uncertain. Suppressed advisories render nothing.
    """
    if not advisory or advisory.get("suppressed"):
        return ""
    t = advisory["target"]
    c = advisory["counterpart"]
    shared = (advisory.get("shared_behavior") or "").strip()
    # Strip the engine-facing prefix; the sentence stands alone publicly.
    shared = shared.removeprefix(
        "Investigator-described shared responsibility: ").strip()
    diffs = list(advisory.get("differences") or [])
    ch = (record or {}).get("challenge") or {}
    unsafe = [str(r) for r in (ch.get("replacement_unsafe_reasons") or [])]
    unknowns = [str(u) for u in (ch.get("luna_unknowns") or [])]
    core_diffs = [d for d in diffs
                  if not d.startswith("Open question: ")]
    open_qs = ([d[len("Open question: "):] for d in diffs
                if d.startswith("Open question: ")] + unknowns)
    lines = [
        "**Possible existing functionality**",
        "",
        f"New code `{t['path']}::{t['symbol']}` "
        f"(lines {t['start_line']}–{t['end_line']}) may overlap with existing "
        f"`{c['path']}::{c['symbol']}` "
        f"(lines {c['start_line']}–{c['end_line']}).",
        "",
        (shared if shared else
         "The two implementations appear to share behaviour."),
    ]
    if core_diffs:
        lines += ["", "They differ in:"]
        lines += [f"- {d}" for d in core_diffs]
        lines += ["",
                  "Directly replacing one with the other may be unsafe; "
                  "consider whether the common implementation can be reused "
                  "independently."]
    else:
        lines += ["",
                  "No material differences were established in the examined "
                  "evidence; consider whether the existing implementation "
                  "already covers this behaviour."]
    if unsafe:
        lines += ["", "Replacement risks identified:"]
        lines += [f"- {r}" for r in unsafe]
    if open_qs:
        seen: list[str] = []
        for q in open_qs:
            if q and q not in seen:
                seen.append(q)
        if seen:
            lines += ["", "Open questions (not verified):"]
            lines += [f"- {q}" for q in seen[:6]]
    prov = ((record or {}).get("provenance") or {})
    lines += ["",
              "Uncertainty: this is a suspicion, not a proof of equivalence. "
              "Challenge validation is "
              f"{ch.get('validation_level')}; "
              f"luna status is {ch.get('luna_status', 'not_challenged')}."]
    pc = prov.get("process_claims") or {}
    if pc.get("process_claim_unverified"):
        lines += ["",
                  "Process note: the model described tool or checkout "
                  "problems, but the run log shows no tool calls were made; "
                  "treat any such claim as unverified."]
    return "\n".join(lines)
