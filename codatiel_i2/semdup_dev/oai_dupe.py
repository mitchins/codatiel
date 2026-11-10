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

TERRA_INSTRUCTIONS_VERSION = "semdup-terra-assess-0.1"
LUNA_INSTRUCTIONS_VERSION = "semdup-luna-challenge-0.1"

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
3. What material behavioural or contractual differences exist.
4. Whether the relationship warrants a developer-facing advisory.

Also report two specific checks:
- wrapper_or_delegation: true when the new code merely calls/delegates to the existing implementation (or vice versa) rather than independently re-implementing it. A wrapper call is not a duplicate.
- ownership_lifecycle_difference: true when the two live under clearly different ownership, lifecycle or stability contracts (e.g. generated code vs hand-written, vendored vs first-party, test fixture vs production) that would make consolidation misleading.

Strength guidance: "likely" only when the implementations are substantively interchangeable for the shared responsibility and you found no material difference; otherwise "possible". When in doubt, say so in uncertainty.

Respond with exactly one JSON object, no prose:
{"overlap": boolean,
 "relationship": "equivalent|shared_part|diverged|related_only|unknown",
 "strength": "likely|possible|none",
 "shared_behavior": "one or two sentences naming the shared responsibility",
 "differences": ["concrete difference, or a stated uncertainty when unknown"],
 "wrapper_or_delegation": boolean,
 "ownership_lifecycle_difference": boolean,
 "uncertainty": "what you could not verify"}"""

LUNA_INSTRUCTIONS = """You are challenging one proposed duplication overlap for a semantic code-duplication detector. This is not a general code review and not a repository-wide sweep: consider ONLY the two implementations below and the proposed overlap.

Your job is to find concrete reasons the proposed overlap could mislead a developer. Look for:
- Divergent behaviour on real inputs (branches, edge cases, error semantics).
- Different contracts (argument shapes, return values, units, rounding, ordering).
- Ownership or lifecycle differences (generated vs hand-written, vendored, test-only).
- Wrapper/delegation rather than duplication (one side merely calls the other).
- Side effects, concurrency, statefulness or resource handling that differ.
- Important exceptional branches handled on one side only.

Do NOT vote on whether the proposer is correct. Substantiate counterexamples with spans from the given sources, or state explicitly that you found none. A lack of counterexamples is a valid outcome, not a failure.

Respond with exactly one JSON object, no prose:
{"fatal_objections": [{"claim": "concrete counter-claim", "span": "path:lines of the cited evidence", "kind": "behavioral|action-only", "resolved_by_proposal": boolean}],
 "unknowns": ["material question you could not resolve from the evidence"],
 "responsibility_holds": boolean}"""


def instructions_id(name: str, text: str) -> str:
    """Content-derived instruction id (same convention as service)."""
    return f"{name}@{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


TERRA_INSTRUCTIONS_ID = instructions_id(
    "semdup-terra-assess-0.1", TERRA_INSTRUCTIONS)
LUNA_INSTRUCTIONS_ID = instructions_id(
    "semdup-luna-challenge-0.1", LUNA_INSTRUCTIONS)


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


def challenge_pair(*, evidence: dict, terra: dict, tools: list, model: Any,
                   model_settings: Any, ledger: Any, ctx: Any = None,
                   call: Callable[..., Any] | None = None) -> dict:
    """Run Luna's challenge on a Terra-eligible pair. Never raises."""
    from_cod = "codatiel_i2.semdup_vendored.semdup"
    import importlib
    AS = importlib.import_module(f"{from_cod}.assess")
    SC = importlib.import_module(f"{from_cod}.schema_check")
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
        transcript.update({"status": "challenge_unreadable"})
        out = dict(assessment)
        out["action"] = "investigate"
        out["validation"] = {"level": "unresolved",
                             "unresolved": [f"challenge unreadable: {exc}"],
                             "artifacts": []}
        return {"ok": True, "transcript": transcript, "assessment": out,
                "challenged": True,
                "fatal_claims": [f"challenge unreadable: {exc}"]}
    errors = SC.validate(value, AS.CHALLENGE_SCHEMA)
    if errors:
        transcript.update({"status": "challenge_invalid", "errors": errors})
        out = dict(assessment)
        out["action"] = "investigate"
        out["validation"] = {"level": "unresolved",
                             "unresolved": ["challenge rejected: "
                                            + "; ".join(errors)],
                             "artifacts": []}
        return {"ok": True, "transcript": transcript, "assessment": out,
                "challenged": True, "fatal_claims": list(errors)}
    reconciled, claims = AS.reconcile(assessment, value, packet)
    transcript.update({"status": "challenged"})
    if (not claims and bool(value.get("responsibility_holds"))
            and reconciled.get("action") in ("reuse_existing",
                                             "extract_shared_part",
                                             "share_contract")):
        # Survived an independent challenge with no counterevidence:
        # this is what "characterised" means. reconcile only downgrades,
        # so the upgrade is applied here, explicitly and only here.
        validation = dict(reconciled.get("validation") or {})
        validation["level"] = "characterised"
        reconciled = dict(reconciled)
        reconciled["validation"] = validation
    return {"ok": True, "transcript": transcript, "assessment": reconciled,
            "challenged": True, "fatal_claims": claims}


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
    if not overlap:
        status, recommendation = "investigate", "investigate"
        prelim = "keep_separate"
    elif challenged and action in ("reuse_existing", "extract_shared_part",
                                  "share_contract"):
        status, recommendation = "finding", action
        prelim = action
    elif challenged:
        status, recommendation = "investigate", "investigate"
        prelim = {"equivalent": "reuse_existing",
                  "shared_part": "extract_shared_part"}.get(relationship,
                                                            "investigate")
    else:
        status, recommendation = "investigate", "investigate"
        prelim = {"equivalent": "reuse_existing",
                  "shared_part": "extract_shared_part"}.get(rel, "investigate")
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
