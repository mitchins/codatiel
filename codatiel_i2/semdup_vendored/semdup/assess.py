"""Nominal assessment + adversarial challenge over evidence packets.

Facts (spans, hashes, fingerprints, flags) vs hypotheses (descriptions,
model verdicts) stay separated end to end. Model verdicts never become
repository facts without passing: strict JSON parse (no brace-stripping),
local schema validation, span re-validation, and a fresh-context
challenge with downgrade on unresolved fatal objections.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from . import ledger as led
from . import schema_check as sc

PROMPT_VERSION = "assessor_v1"
CHALLENGE_PROMPT_VERSION = "challenger_v2"

MEMBER_NOTES_SPEC = (
    "member_notes shape (exact): an array with exactly one entry per member "
    "header above, each entry {\"member\": {\"path\": \"<exact path from the header>\", "
    "\"start_line\": <int from the header>, \"end_line\": <int from the header>}, "
    "\"note\": \"<one line: how this member implements the proposed responsibility>\"}. "
    "Example: {\"member\": {\"path\": \"a.py\", \"start_line\": 3, \"end_line\": 9}, "
    "\"note\": \"branch-chain implementation of the clamp\"}. "
    "The member value is always an object with path/start_line/end_line keys, "
    "never a bare string; the text key is always \"note\"."
)

RELATIONSHIPS = ("equivalent", "shared_part", "diverged", "related_only", "unknown")
ACTIONS = ("reuse_existing", "extract_shared_part", "share_contract", "keep_separate", "investigate")
ACTIONABLE = ("reuse_existing", "extract_shared_part", "share_contract")
LEVELS = ("source_supported", "characterised", "differentially_tested", "compiled_and_tested", "unresolved")

ASSESSMENT_SCHEMA = {
    "type": "object",
    "required": ["responsibility", "relationship", "action", "change_scenario",
                 "observed_differences", "member_notes", "proposal", "validation"],
    "properties": {
        "responsibility": {"type": "string", "minLength": 1},
        "relationship": {"type": "string", "enum": list(RELATIONSHIPS)},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "change_scenario": {"type": "string", "minLength": 1},
        "observed_differences": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "member_notes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["member", "note"],
                "properties": {
                    "member": {"type": "object",
                               "required": ["path", "start_line", "end_line"],
                               "properties": {"path": {"type": "string", "minLength": 1},
                                              "start_line": {"type": "integer", "minimum": 1},
                                              "end_line": {"type": "integer", "minimum": 1}}},
                    "note": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
        "proposal": {
            "type": "object",
            "required": ["target", "interface", "preserved", "remain_separate", "benefit", "behavior_changes"],
            "properties": {
                "target": {"anyOf": [
                    {"type": "object",
                     "required": ["path", "start_line", "end_line"],
                     "properties": {"path": {"type": "string", "minLength": 1},
                                    "start_line": {"type": "integer", "minimum": 1},
                                    "end_line": {"type": "integer", "minimum": 1}}},
                    {"type": "null"}]},
                "interface": {"type": "string", "minLength": 1},
                "preserved": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                "remain_separate": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "benefit": {"type": "string", "minLength": 1},
                "behavior_changes": {"type": "array", "items": {"type": "string", "minLength": 1}},
            },
            "additionalProperties": False,
        },
        "validation": {
            "type": "object",
            "required": ["level", "unresolved", "artifacts"],
            "properties": {
                "level": {"type": "string", "enum": list(LEVELS)},
                "unresolved": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "artifacts": {"type": "array", "items": {"type": "string", "minLength": 1}},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}


class AssessError(RuntimeError):
    pass


CHALLENGE_SCHEMA = {
    "type": "object",
    "required": ["fatal_objections", "unknowns", "responsibility_holds"],
    "properties": {
        "fatal_objections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim", "span", "resolved_by_proposal", "kind"],
                "properties": {
                    "claim": {"type": "string", "minLength": 1},
                    "span": {"type": "string", "minLength": 1},
                    "resolved_by_proposal": {"type": "boolean"},
                    "kind": {"type": "string", "enum": ["behavioral", "action-only"]},
                },
                "additionalProperties": False,
            },
        },
        "unknowns": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "responsibility_holds": {"type": "boolean"},
    },
    "additionalProperties": False,
}


def packet_sha(packet: dict) -> str:
    return hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()


def build_packet(members: list[dict], support: list[dict] | None = None) -> dict:
    """Assemble a packet from member/support span dicts.

    Each member: {path, start_line, end_line, source_sha256, symbol, text,
    description, description_provenance, fingerprint, flags}.
    """
    packet = {"members": members, "support": support or []}
    packet["packet_sha256"] = packet_sha({k: packet[k] for k in ("members", "support")})
    return packet


def render_prompt(packet: dict, prompt_text: str) -> str:
    lines = [prompt_text.strip(), "", "## Evidence packet (facts first, hypotheses labeled)",
             f"packet_sha256: {packet['packet_sha256']}", ""]
    for i, m in enumerate(packet["members"], 1):
        lines += [f"### Member {i}: {m['path']}:{m['start_line']}-{m['end_line']} (symbol {m.get('symbol', '?')})",
                  f"source_sha256: {m['source_sha256']}", "```", m["text"], "```",
                  f"hypothesis/description [{m.get('description_provenance', '?')}]: {m.get('description', '')}",
                  f"deterministic surface: fingerprint={m.get('fingerprint', '?')[:48]}… flags={m.get('flags', [])}", ""]
    for i, s in enumerate(packet["support"], 1):
        lines += [f"### Support {i}: {s['path']}:{s['start_line']}-{s['end_line']}", "```", s.get("text", ""), "```", ""]
    lines += ["Respond with exactly one JSON object matching the assessment schema: "
              "responsibility, relationship, action, change_scenario, observed_differences[], "
              "member_notes[] (one entry per member header above, each justifying that "
              "member against the proposed responsibility), "
              "proposal{target|interface|preserved[]|remain_separate[]|benefit|behavior_changes[]}, "
              "validation{level|unresolved[]|artifacts[]}. No prose.",
              "",
              "Machine constraints (must hold exactly):",
              f"- relationship is one of: {', '.join(RELATIONSHIPS)}",
              f"- action is one of: {', '.join(ACTIONS)}",
              f"- validation.level is one of: {', '.join(LEVELS)}",
              "- proposal.target is either null or an object "
              '{"path": str, "start_line": int, "end_line": int} matching one member header above — never a bare string',
              "- proposal.interface is a concrete callable signature or schema fragment "
              "(e.g. `name(args) -> type`), never a bare type word or a restatement of the responsibility",
              "- action reuse_existing requires a non-null target; other actions use null unless an existing member is reused",
              "- behavior_changes must be [] for reuse_existing/extract_shared_part/share_contract; "
              "unresolved drift belongs in investigate with the concrete difference stated",
              "- validation artifacts must be [] unless you were given build/test evidence (you were not): "
              "use level source_supported, or unresolved when something material is unknown",
              "",
              MEMBER_NOTES_SPEC]
    return "\n".join(lines)


def repair_prompt(bad_output: str, errors: list[str]) -> str:
    return ("Your previous response was rejected by the output validator. Fix ONLY "
            "the listed violations and return exactly one corrected JSON object. No prose.\n"
            "Violations:\n- " + "\n- ".join(errors) + "\n\nPrevious output:\n" + bad_output +
            "\n\n" + MEMBER_NOTES_SPEC)


def _parse_strict(content: str) -> dict:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AssessError(f"non-JSON assessment output: {exc}") from exc
    if not isinstance(value, dict):
        raise AssessError("assessment output is not an object")
    return value


def _validate_assessment(value: dict, packet: dict) -> list[str]:
    errors = sc.validate(value, ASSESSMENT_SCHEMA)
    if errors:
        return errors
    member_keys = {(m["path"], m["start_line"], m["end_line"]) for m in packet["members"]}
    noted = {(n["member"]["path"], n["member"]["start_line"], n["member"]["end_line"])
             for n in value.get("member_notes", [])}
    if len(value.get("member_notes", [])) != len(packet["members"]) or noted != member_keys:
        errors.append("member_notes must justify every packet member exactly once")
    if value["action"] in ACTIONABLE and value["proposal"]["behavior_changes"]:
        errors.append("actionable behavior-preserving consolidation declares behavior changes")
    if value["action"] == "reuse_existing" and value["proposal"]["target"] is None:
        errors.append("reuse_existing without an existing target span")
    target = value["proposal"]["target"]
    if target is not None and (target["path"], target["start_line"], target["end_line"]) not in member_keys:
        errors.append("reuse target is not one of the packet members")
    if value["validation"]["level"] in ("characterised", "differentially_tested", "compiled_and_tested") \
            and not value["validation"]["artifacts"]:
        errors.append("executed validation claimed without artifact references")
    # F6 (M6): vague proposal interfaces ("string", "rounding_strategy",
    # "unify implementation style…") must not ship as actionable. An
    # actionable interface is a concrete callable signature or schema
    # fragment (name + parameter shape, arrow, or typed/schema markers).
    # NOTE (documented coexistence): validation.level describes evidence
    # strength for what IS claimed, while validation.unresolved lists
    # residual questions — source_supported with minor unknowns ships
    # only when behavior_changes is empty and challenge upholds; forcing
    # every unknown to level=unresolved would erase the distinction
    # between "evidence supports the claim" and "nothing is unknown".
    if value["action"] in ACTIONABLE and not _concrete_interface(
            value["proposal"].get("interface", "")):
        errors.append("actionable proposal.interface must be a concrete callable "
                      "signature or schema fragment (e.g. `name(args) -> type`), "
                      "never a bare type word or restatement of the responsibility")
    return errors


def _concrete_interface(text: str) -> bool:
    """Callable signature or schema fragment, not prose (F6 gate)."""
    if not isinstance(text, str):
        return False
    if re.search(r"\w+\s*\(.*\)", text):
        return True
    return any(mark in text for mark in ("->", "{", "}", ":", "=>")) and len(text.split()) <= 12


def assess(
    packet: dict,
    prompt_text: str,
    transport: Callable[[list[dict]], object],
    record: led.Ledger,
    *,
    role: str,
    stage: str,
    endpoint: str,
    served_model_id: str | None,
    expected_model_id: str | None,
) -> tuple[dict, str]:
    """Run one assessment; return (validated assessment, packet_sha).

    Raises AssessError on malformed/invalid output (ledger records failure).
    `transport` dependency-injects the model call for testability.
    """
    prompt = render_prompt(packet, prompt_text)
    result = transport([{"role": "user", "content": prompt}])  # type: ignore[operator]
    value = _parse_strict(result.content)
    errors = _validate_assessment(value, packet)
    if errors:
        raise AssessError("assessment rejected: " + "; ".join(errors))
    return value, packet["packet_sha256"]


def _challenge_prompt(packet: dict, assessment: dict, prompt_text: str) -> str:
    lines = [prompt_text.strip(), "", "## Proposed consolidation under challenge",
             f"relationship={assessment['relationship']} action={assessment['action']}",
             f"responsibility: {assessment['responsibility']}",
             f"interface: {assessment['proposal']['interface']}",
             f"preserved: {assessment['proposal']['preserved']}",
             f"remain_separate: {assessment['proposal']['remain_separate']}", "",
             "## Same source evidence (no prior verdict attached)"]
    for i, m in enumerate(packet["members"], 1):
        lines += [f"### Member {i}: {m['path']}:{m['start_line']}-{m['end_line']}", "```", m["text"], "```", ""]
    lines += ['Respond with exactly one JSON object: {"fatal_objections": [{"claim": str, '
              '"span": "path:lines of the evidence", "resolved_by_proposal": bool, '
              '"kind": "behavioral" or "action-only"}], '
              '"unknowns": [str], "responsibility_holds": bool}. No prose.']
    return "\n".join(lines)


def _span_paths(value: dict) -> list[str]:
    paths = []
    for objection in value.get("fatal_objections", []):
        span = objection.get("span", "")
        path = span.split(":")[0].strip() if isinstance(span, str) else ""
        if path:
            paths.append(path)
    return paths


def reconcile(assessment: dict, challenge_value: dict, packet: dict | None = None) -> tuple[dict, list[str]]:
    """Reconcile challenge counterevidence with the assessed relationship.

    - `behavioral` fatals dispute interchangeability: if the shared
      responsibility still holds, the pair is `diverged` (same
      responsibility, implementations differ — never blindly replace);
      if the responsibility itself falls, fail closed to `unknown`.
    - `action-only` fatals dispute the consolidation, not the equivalence:
      relationship stands, action falls back to `investigate`.
    - Challenger uncertainty (`unknowns`) or a disputed responsibility
      with no fatals still fails closed on equivalence claims.
    - Any downgrade forces `validation.level = unresolved`.
    - `diverged` is used only when same-responsibility behavioral
      divergence is source-supported (a concrete fatal behavioral
      objection); otherwise `unknown`.
    - When `packet` is given, fatal spans must cite member/support paths;
      an uncited span fails closed to `unknown` (hallucinated evidence
      never upgrades a relationship).
    """
    fatal = [o for o in challenge_value.get("fatal_objections", [])
             if not o.get("resolved_by_proposal", False)]
    claims = [o["claim"] for o in fatal]
    holds = bool(challenge_value.get("responsibility_holds", False))
    unknowns = list(challenge_value.get("unknowns", []))
    behavioral = [o["claim"] for o in fatal if o.get("kind") == "behavioral"]
    equivalence_claim = assessment.get("relationship") in ("equivalent", "shared_part")
    if packet is not None and fatal:
        known = {m["path"] for m in packet.get("members", [])} | \
                {s["path"] for s in packet.get("support", [])}
        cited = _span_paths(challenge_value)
        if any(p not in known for p in cited):
            out = _downgrade_on_fatal(assessment, claims + ["challenger cited evidence outside the packet"])
            out["relationship"] = "unknown"
            return out, claims
    if not fatal:
        if (unknowns or not holds) and equivalence_claim:
            out = _downgrade_on_fatal(
                assessment, unknowns + ([] if holds else ["shared responsibility disputed by challenger"]))
            out["relationship"] = "unknown"
            return out, unknowns
        return assessment, list(unknowns)
    if not holds:
        out = _downgrade_on_fatal(assessment, claims + ["shared responsibility disputed by challenger"])
        out["relationship"] = "unknown"
        return out, claims
    if behavioral and assessment.get("relationship") in ("equivalent", "shared_part", "diverged"):
        out = _downgrade_on_fatal(assessment, claims)
        out["relationship"] = "diverged"
        return out, claims
    out = _downgrade_on_fatal(assessment, claims)
    return out, claims


def _downgrade_on_fatal(assessment: dict, claims: list[str]) -> dict:
    """Fall back to investigate with level unresolved.

    A fatal unresolved objection means the safe claim is unknown, not
    merely a narrower action: level must follow the action down. Applied
    to actionable proposals and to any equivalence claim under doubt.
    """
    assessment = dict(assessment)
    assessment["action"] = "investigate"
    validation = dict(assessment["validation"])
    validation["unresolved"] = list(validation["unresolved"]) + claims
    validation["level"] = "unresolved"
    assessment["validation"] = validation
    assessment["observed_differences"] = list(assessment.get("observed_differences", [])) + claims
    return assessment


def challenge(
    packet: dict,
    assessment: dict,
    prompt_text: str,
    transport: Callable[[list[dict]], object],
) -> tuple[dict, list[str], dict]:
    """Fresh-context challenge (single implementation; audit calls this).

    Returns (possibly downgraded assessment, fatal claims, diagnosis with
    prompt text + raw result for ledger attribution). Malformed challenger
    output raises AssessError so the caller downgrades fail-closed — a
    challenge that cannot be read is an unresolved objection, never a pass.
    Equivalence claims (equivalent/shared_part) are always challenged here;
    callers should skip only terminal non-equivalence verdicts.
    """
    if assessment["action"] not in ACTIONABLE and \
            assessment.get("relationship") not in ("equivalent", "shared_part"):
        return assessment, [], {"prompt": "", "result": None}
    prompt = _challenge_prompt(packet, assessment, prompt_text)
    result = transport([{"role": "user", "content": prompt}])  # type: ignore[operator]
    value = _parse_strict(result.content)
    errors = sc.validate(value, CHALLENGE_SCHEMA)
    if errors:
        raise AssessError("challenge rejected: " + "; ".join(errors))
    assessment, claims = reconcile(assessment, value, packet)
    return assessment, claims, {"prompt": prompt, "result": result}
