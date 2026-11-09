"""Staged investigation product path: discover -> investigate ->
arbitrate -> challenge -> report.

Built on product primitives only: retrieve/fusion (A), sourceread +
agentloop (B/C), cards validation (B), assess.challenge (D), ledger +
spans + delegation guard throughout. No agents-SDK dependency.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from . import agentloop as AL
from . import assess as AS
from . import cards as CD
from . import delegation as DG
from . import fusion as FU
from . import inference as I
from . import ledger as L
from . import retrieve as RT
from . import sourceread as SR
from . import spans as SP

PROMPTS = Path(__file__).parent / "prompts"
MAX_AREAS = 8
FINALIST_CHAR_CAP = 30000

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["target_responsibility", "inspected", "related_implementations",
                 "best_existing_implementation", "recommended_family", "action",
                 "proposed_boundary", "must_remain_separate", "uncertainty"],
    "properties": {
        "target_responsibility": {"type": "string", "minLength": 1},
        "inspected": {"type": "array", "items": {"type": "string"}},
        "related_implementations": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["implementation_id", "relationship", "why"],
                      "properties": {
                          "implementation_id": {"type": "string", "minLength": 1},
                          "relationship": {"type": "string",
                                           "enum": list(CD.RELATIONSHIPS)},
                          "why": {"type": "string"}}}},
        "best_existing_implementation": {"anyOf": [{"type": "null"}, {"type": "string"}]},
        "recommended_family": {"type": "array", "items": {"type": "string"}},
        "action": {"type": "string", "enum": ["reuse_existing", "extract_shared_part",
                                              "share_contract", "keep_separate",
                                              "investigate"]},
        "proposed_boundary": {"anyOf": [{"type": "null"}, {"type": "string"}]},
        "must_remain_separate": {"type": "array", "items": {"type": "string"}},
        "uncertainty": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}

ACTION_REL = {"reuse_existing": "equivalent", "extract_shared_part": "shared_part",
              "share_contract": "related_only", "keep_separate": "related_only",
              "investigate": "unknown"}


def _transport(url: str, model: str, max_tokens: int = 4000,
               api_key: str | None = None,
               use_json_mode: bool = False,
               expected_model_id: str | None = None) -> Callable[[list[dict]], Any]:
    def call(messages: list[dict]):
        return I.chat(url, model, messages, max_tokens=max_tokens,
                      timeout_s=300.0, attempts=1, use_json_mode=use_json_mode,
                      api_key=api_key, expected_model_id=expected_model_id)
    return call


def _unit_row(conn: sqlite3.Connection, file: str, symbol: str) -> dict:
    # GAP-4 (DEV-01): exact (path, symbol) identity first. The basename
    # LIKE and bare-symbol fallbacks below are retained for hand-written
    # targets, but PR findings must never depend on an arbitrary
    # first-row win when two directories hold the same basename/symbol.
    r = conn.execute("SELECT id, path, symbol, start_line, end_line, text "
                     "FROM units WHERE path=? AND symbol=?",
                     (file, symbol)).fetchone()
    if r is None:
        rows = conn.execute("SELECT id, path, symbol, start_line, end_line, text "
                            "FROM units WHERE path LIKE ? AND symbol=?",
                            (f"%{file.split('/')[-1]}", symbol)).fetchall()
        if len(rows) == 1:
            r = rows[0]
    if r is None:
        rows = conn.execute("SELECT id, path, symbol, start_line, end_line, text "
                            "FROM units WHERE symbol=?", (symbol,)).fetchall()
        if len(rows) == 1:
            r = rows[0]
    if r is None:
        raise LookupError(f"target not in index: {file}::{symbol}")
    return {"id": r[0], "path": r[1], "symbol": r[2],
            "start_line": r[3], "end_line": r[4], "text": r[5]}


def _rel(card: dict) -> str:
    """Relationship hypothesis across card variants (v1/v2/ablations)."""
    return (card.get("relationship_hypothesis") or card.get("relationship")
            or "unknown")


def discover(conn: sqlite3.Connection, target: dict, tid: int,
             explore_fn=None, emb: dict | None = None) -> dict:
    """Stage A: deterministic candidate discovery (no LLM)."""
    t0 = time.monotonic()
    det, _ = RT.all_channel_neighbors(conn, tid, k=12, emb=emb)
    areas: dict[str, dict] = {}
    for e in det:
        r = conn.execute("SELECT path, symbol FROM units WHERE id=?",
                         (e["id"],)).fetchone()
        if r is None:
            continue
        key = f"{r[0]}::{r[1]}"
        if key == f"{target['file']}::{target['symbol']}" or \
                f"{r[0].split('/')[-1]}::{r[1]}" == f"{target['file']}::{target['symbol']}":
            continue  # target is never its own candidate
        ch = sorted(c for c, rk in (e.get("channel_ranks") or {}).items()
                    if rk is not None)
        areas[key] = {"id": key, "path": r[0], "symbol": r[1],
                      "why": "union retrieval", "provenance": ch or ["rrf"]}
    if explore_fn is not None:
        try:
            for loc in explore_fn():
                key = loc.get("ref", "")
                if not key or key in areas:
                    continue
                areas[key] = {"id": key, "path": key.split("::")[0],
                              "symbol": key.split("::")[-1],
                              "why": str(loc.get("why", ""))[:220],
                              "provenance": list(loc.get("via", []))}
        except (ValueError, RuntimeError, OSError):
            pass
    clustered: dict[str, dict] = {}
    for key, a in areas.items():
        f = a["path"]
        if f in clustered:
            clustered[f]["members"].append(a)
            clustered[f]["provenance"] = sorted(set(clustered[f]["provenance"]) | set(a["provenance"]))
        else:
            clustered[f] = {"id": f, "path": f, "symbol": "", "why": a["why"],
                            "provenance": list(a["provenance"]), "members": [a]}
    ranked = sorted(clustered.values(), key=lambda a: (-len(a["members"]), a["path"]))
    kept, dropped = ranked[:MAX_AREAS], ranked[MAX_AREAS:]
    return {"candidate_areas": kept, "n_union": len(det),
            "n_dropped_by_cap": len(dropped),
            "dropped_ids": [a["id"] for a in dropped],
            "wall_s": round(time.monotonic() - t0, 1)}


def _b_tools(root: Path, conn: sqlite3.Connection,
             read_ledger: list) -> list[AL.Tool]:
    def w_read_symbol(args: dict) -> str:
        r = SR.read_symbol(root, conn, args.get("symbol_id", ""), read_ledger)
        return f"--- {r['id']} lines {r['start']}-{r['end']} ---\n{r['text']}"

    def w_read_context(args: dict) -> str:
        r = SR.read_context(root, conn, args.get("symbol_id", ""),
                            int(args.get("before", 10)), int(args.get("after", 10)),
                            read_ledger)
        return f"--- {r['id']} lines {r['start']}-{r['end']} ---\n{r['text']}"

    def w_references(args: dict) -> str:
        r = SR.references(conn, args.get("symbol", ""))
        return "\n".join(r["callers"]) or "(no recorded callers)"

    def w_search(args: dict) -> str:
        pat = str(args.get("pattern", ""))[:120]
        hits: list[str] = []
        for full in sorted(root.rglob("*")):
            if len(hits) >= 40 or not full.is_file() or \
                    full.suffix not in (".py", ".php", ".swift"):
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if pat in line:
                    hits.append(f"{full.relative_to(root)}:{i}: {line.strip()[:160]}")
                    if len(hits) >= 40:
                        break
        return "\n".join(hits) or "(no matches)"

    return [
        AL.Tool("read_symbol", "Read one indexed unit body exactly (path::symbol).",
                "{symbol_id: str}", w_read_symbol),
        AL.Tool("read_context", "Bounded surroundings of one unit.",
                "{symbol_id: str, before?: int<=40, after?: int<=40}", w_read_context),
        AL.Tool("references", "Bounded caller list for a symbol.",
                "{symbol: str}", w_references),
        AL.Tool("narrow_search", "Literal search for one pattern (supporting only).",
                "{pattern: str}", w_search),
    ]


def investigate_area(root: Path, conn: sqlite3.Connection, area: dict,
                     target: dict, target_text: str,
                     url: str, model: str, card_variant: str = "v2",
                     expected_model_id: str | None = None) -> dict:
    """Stage B: one fresh-context card. Variants for ablation (§15)."""
    t0 = time.monotonic()
    prompt_text = (PROMPTS / f"stage_b_{card_variant}.md").read_text() \
        if (PROMPTS / f"stage_b_{card_variant}.md").exists() \
        else (PROMPTS / "stage_b_v2.md").read_text()
    read_ledger: list[dict] = []
    tools = _b_tools(root, conn, read_ledger)
    member_lines = "\n".join(f"- {m['id']}: {m['why']}" for m in area.get("members", []))
    user_msg = (f"Candidate area: {area['id']}\n{member_lines}\n\n"
                f"Target ({target['file']} :: {target['symbol']}):\n{target_text}\n"
                f"--- end target ---")
    loop = AL.run(url, model, prompt_text, user_msg, tools,
                  max_turns=14, max_tokens=4000,
                  expected_model_id=expected_model_id)
    card: dict = {}
    errors: list[str] = []
    normalizations: list[str] = []
    ok = False
    reads = sum(1 for t in loop.tool_calls
                if t["tool"] in ("read_symbol", "read_context"))
    if loop.ok:
        try:
            value = json.loads(loop.final_text[loop.final_text.find("{"):
                                               loop.final_text.rfind("}") + 1])
            value, normalizations = _normalize_card(value)
            file_hashes = {r[0]: r[1] for r in
                           conn.execute("SELECT path, sha256 FROM files").fetchall()}
            errors = validate_card_lenient(value, root, card_variant, file_hashes)
            if not errors and reads == 0:
                # Minimum evidence bar: a card with no reads is an
                # uninvestigated opinion, not delegated evidence.
                errors = ["no source read performed for this area"]
            if not errors and (card_variant == "v2" or _ablation_shape_ok(value, card_variant)):
                card, ok = value, True
            elif not errors:
                errors = [f"ablation {card_variant}: shape rejected"]
        except (ValueError, TypeError) as exc:
            errors = [f"card parse: {exc}"]
    # Ablation bookkeeping: record requested variant even on failure.
    return {"area": area["id"], "ok": ok, "errors": errors, "card": card,
            "normalizations": normalizations,
            "card_variant": card_variant,
            "role_included": card_variant in ("v2", "nodiffs"),
            "diffs_included": card_variant in ("v2", "v1", "norole"), "n_tool_calls": len(loop.tool_calls),
            "peak_live_chars": loop.usage.get("peak_live_chars", 0),
            "source": SR.ledger_lines(read_ledger),
            "loop_wall_s": loop.wall_s, "loop_error": loop.error,
            "members": [m["id"] for m in area.get("members", [])]}


def _ablation_shape_ok(value: dict, variant: str) -> bool:
    """Minimal acceptance for ablation variants (fields under test may be absent)."""
    if variant == "v1":
        return isinstance(value.get("relationship"), str) or \
            isinstance(value.get("relationship_hypothesis"), str)
    if variant == "norole":
        return isinstance(value.get("relationship_hypothesis"), str)
    if variant == "nodiffs":
        return isinstance(value.get("relationship_hypothesis"), str) and \
            "candidate_role" in value
    return True


def _normalize_card(value: dict) -> tuple[dict, list[str]]:
    """Tolerate shape variance that preserves semantics (recorded, not silent).

    - possible_shared_boundary as string/null -> {description, span_or_null}.
    - existing_abstraction as string -> {symbol, span: ""} (span checked after).
    - shared_behavior/material_differences entries as strings -> {claim, span: ""}.
    Empty spans then fail span validation honestly (no evidence located).
    """
    notes: list[str] = []
    value = dict(value)
    b = value.get("possible_shared_boundary")
    if isinstance(b, str) or b is None:
        value["possible_shared_boundary"] = {"description": b or "", "span_or_null": None}
        notes.append("boundary-normalized")
    a = value.get("existing_abstraction")
    if isinstance(a, str):
        value["existing_abstraction"] = {"symbol": a, "span": ""}
        notes.append("abstraction-normalized")
    for key, mk in (("shared_behavior",
                     lambda s: {"claim": s, "span": ""}),
                    ("material_differences",
                     lambda s: {"kind": "other", "claim": s, "span": ""})):
        items = value.get(key)
        if isinstance(items, list) and any(isinstance(x, str) for x in items):
            value[key] = [mk(x) if isinstance(x, str) else x for x in items]
            notes.append(f"{key}-normalized")
    return value, notes
    """Minimal acceptance for ablation variants (fields under test may be absent)."""
    if variant == "v1":
        return isinstance(value.get("relationship"), str) or \
            isinstance(value.get("relationship_hypothesis"), str)
    if variant == "norole":
        return isinstance(value.get("relationship_hypothesis"), str)
    if variant == "nodiffs":
        return isinstance(value.get("relationship_hypothesis"), str) and \
            "candidate_role" in value
    return True
    """Ablation C/D: drop role or material_differences (schema re-checked
    by the caller variant rules — ablation outputs are validated
    leniently: missing ablated fields are expected)."""
    value = dict(value)
    if variant == "norole":
        value.pop("candidate_role", None)
    if variant == "nodiffs":
        value.pop("material_differences", None)
    return value


def validate_card_lenient(value: dict, root: Path, variant: str,
                          file_hashes: dict | None) -> list[str]:
    if variant == "v1":
        return []  #  baseline: free-shape card, no v2 checks
    errors = CD.validate_card(value, root, file_hashes)
    if variant == "norole":
        errors = [e for e in errors if "candidate_role" not in e and "role/" not in e]
    if variant == "nodiffs":
        errors = [e for e in errors if "material_differences" not in e]
    return errors


def arbitrate(root: Path, conn: sqlite3.Connection, target: dict,
              target_text: str, cards: list[dict], finalist_source: list[dict],
              url: str, model: str, file_hashes: dict | None = None,
              expected_model_id: str | None = None) -> dict:
    """Stage C: arbitration on cards + finalist source only."""
    t0 = time.monotonic()
    prompt_text = (PROMPTS / "stage_c_v2.md").read_text()
    card_lines = []
    for c in cards:
        card = c.get("card") or {}
        rel = (card.get("relationship_hypothesis")
               or card.get("relationship") or "unknown")
        card_lines.append(
            f"- {c.get('area')}: role={card.get('candidate_role', 'n/a')} "
            f"rel={rel} (conf {card.get('confidence')}): "
            f"{str(card.get('shared_responsibility', ''))[:300]} | "
            f"diffs: {json.dumps(card.get('material_differences', card.get('important_differences', [])))[:400]} | "
            f"boundary: {json.dumps(card.get('possible_shared_boundary', ''))[:200]} | "
            f"refute: {json.dumps(card.get('what_would_refute_this', card.get('material_objection', '')))[:200]}")
    src_lines = [f"### {f['id']}\n{(f.get('text') or '')[:3000]}" for f in finalist_source]
    user_msg = (f"Target ({target['file']} :: {target['symbol']}):\n{target_text}\n\n"
                f"Evidence cards:\n" + "\n".join(card_lines) +
                "\n\nFinalist source:\n" + "\n".join(src_lines))
    raw = I.chat(url, model, [{"role": "user", "content": prompt_text + "\n\n" + user_msg}],
                 max_tokens=4000, timeout_s=300.0, attempts=1, use_json_mode=False,
                 expected_model_id=expected_model_id)
    last_completion = raw.completion_tokens
    content = (raw.content or "").strip()
    verdict: dict = {}
    errors: list[str] = []
    stripped = False
    for attempt in ("initial", "repair"):
        try:
            body = content if attempt == "initial" else None
            if attempt == "repair":
                fix = I.chat(url, model, [{"role": "user",
                                           "content": prompt_text + "\n\n" + user_msg +
                                           f"\n\nYour previous reply failed validation: {errors}. " +
                                           f"Fix ONLY the schema violation (same conclusion)."}],
                             max_tokens=4000, timeout_s=300.0, attempts=1,
                             use_json_mode=False,
                             expected_model_id=expected_model_id)
                last_completion = fix.completion_tokens
                body = (fix.content or "").strip()
            verdict, stripped = _tolerant_json_loads(body)
            # Auto-normalize family ids: path:symbol -> path::symbol
            # (deterministic, recorded fix for single-colon emission).
            fam = verdict.get("recommended_family")
            if isinstance(fam, list):
                normed = []
                for f in fam:
                    if isinstance(f, str) and "::" not in f and ":" in f and "/" in f:
                        normed.append(f.replace(":", "::", 1))
                    else:
                        normed.append(f)
                if normed != fam:
                    verdict["recommended_family"] = normed
            errors = list(SC_validate(verdict))
            if not errors:
                content = body
                break
        except (ValueError, TypeError) as exc:
            errors = [f"verdict parse: {exc}"]
    return {"ok": not errors, "errors": errors, "verdict": verdict,
            "control_chars_stripped": stripped,
            "family_ids_normalized": fam != verdict.get("recommended_family") if 'fam' in dir() else False,
            "prompt_chars": len(prompt_text) + len(user_msg),
            "completion_tokens": last_completion,
            "wall_s": round(time.monotonic() - t0, 1)}


def _tolerant_json_loads(body: str) -> tuple[dict, bool]:
    """Strict first; on failure retry after stripping ASCII control
    characters that models copy verbatim from source text into JSON
    strings (T-php double-break: identical failure twice at char 446).
    Returns (value, stripped_flag). The strip is recorded, never silent.
    """
    try:
        return json.loads(body[body.find("{"):body.rfind("}") + 1]), False
    except (ValueError, TypeError):
        cleaned = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]", "", body)
        return json.loads(cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]), True


def SC_validate(verdict: dict) -> list[str]:
    from . import schema_check as sc
    errors = sc.validate(verdict, VERDICT_SCHEMA)
    if errors:
        return errors
    action = verdict.get("action")
    fam = verdict.get("recommended_family") or []
    if action in ("reuse_existing", "extract_shared_part", "share_contract"):
        if not fam or verdict.get("proposed_boundary") is None:
            return ["actionable requires non-empty family + non-null boundary"]
        for f in fam:
            if "::" not in str(f):
                return [f"family id must be path::symbol, got {f!r}"]
    elif fam or verdict.get("proposed_boundary") is not None:
        return ["keep/investigate requires family=[] and boundary=null"]
    return []


def resolve_family(root: Path, conn: sqlite3.Connection, family: list[str],
                   target: dict | None = None) -> tuple[list[str], list[dict]]:
    """Family-id guardrail (product): every member resolves or is dropped;
    the target itself is never its own family member (self-inclusion is
    arbiter imprecision, not evidence); empty family on actionable falls
    to investigate (fail-closed)."""
    kept, dropped = [], []
    tbase = f"{(target or {}).get('file', '').split('/')[-1]}::{(target or {}).get('symbol', '')}"
    for f in family or []:
        if tbase != "::" and f.split("/")[-1] == tbase:
            dropped.append({"member": f, "reason": "target itself, not a family member"})
            continue
        try:
            r = SR.read_symbol(root, conn, f)
            kept.append(r["id"])
        except (LookupError, ValueError, OSError):
            dropped.append({"member": f, "reason": "unresolvable to any indexed unit"})
    return kept, dropped


def challenge_and_report(root: Path, conn: sqlite3.Connection, target: dict,
                         verdict: dict, finalist_rows: list[dict],
                         url: str, model: str,
                         expected_model_id: str | None = None) -> dict:
    """Stage D: typed challenge via assess.challenge; narrow cap on
    refuting kinds; provenance-visible report (§19)."""
    members = [{"path": r["path"], "start_line": r["start_line"],
                "end_line": r["end_line"], "symbol": r["symbol"],
                "text": r["text"]} for r in finalist_rows
               if r["id"] in (verdict.get("recommended_family") or [])]
    packet = AS.build_packet(members)
    fam0 = (verdict.get("recommended_family") or [None])[0]
    proposal = {
        "target": None,
        "interface": str(verdict.get("proposed_boundary") or verdict.get("action")),
        "preserved": [verdict.get("target_responsibility", "")],
        "remain_separate": list(verdict.get("must_remain_separate") or []),
        "benefit": f"avoid duplicate maintenance of {verdict.get('target_responsibility', '')}",
        "behavior_changes": list(verdict.get("must_remain_separate") or []),
    }
    assessment = {
        "responsibility": verdict.get("target_responsibility", ""),
        "relationship": ACTION_REL.get(verdict.get("action"), "unknown"),
        "action": verdict.get("action"),
        "change_scenario": f"new/changed {target['file']}::{target['symbol']}",
        "observed_differences": list(verdict.get("must_remain_separate") or []),
        "member_notes": [{"member": {"path": m["path"], "start_line": m["start_line"],
                                     "end_line": m["end_line"]},
                          "note": m["symbol"]} for m in members],
        "proposal": proposal,
        "validation": {"level": "characterised", "unresolved": [], "artifacts": []},
    }
    # Enrich challenger with deterministic wrapper/use facts (§2):
    # If target text calls a family member, reuse would be wrapper
    # elimination, not independent implementation. This is the T-php
    # mechanism — generic, not heuristic, and only triggers on textual
    # call evidence.
    try:
        # GAP-4 (DEV-01): exact identity first, basename fallback second.
        target_text = conn.execute("SELECT text FROM units WHERE path=? AND symbol=?",
                                   (target["file"], target["symbol"])).fetchone()
        if target_text is None:
            target_text = conn.execute("SELECT text FROM units WHERE path LIKE ? AND symbol=?",
                                       (f"%{target['file'].split('/')[-1]}", target["symbol"])).fetchone()
        target_src = target_text[0] if target_text else ""
    except (sqlite3.OperationalError, TypeError, IndexError):
        target_src = ""
    wrapper_facts = []
    for m in members:
        sym = m["symbol"].split("#")[0].split("@")[0].lstrip("$")
        # Generic call detection: symbol appears as call in target source.
        # Language-agnostic substring check is sufficient for the product
        # wrapper/use class (callee name with '(').
        if sym and f"{sym}(" in target_src:
            # Also run the precise single-call delegation guard where
            # applicable (Python AST-precise, else unknown — safe direction).
            try:
                lang = conn.execute("SELECT language FROM files WHERE path=?",
                                    (m["path"],)).fetchone()
                lang = lang[0] if lang else "unknown"
            except (sqlite3.OperationalError, TypeError):
                lang = "unknown"
            is_del = DG.is_delegation(target_src, sym, lang)
            detail = "single-call delegation" if is_del else "caller/wrapper use"
            wrapper_facts.append(f"target {target['file']}::{target['symbol']} {detail} of {m['path']}::{m['symbol']}")
    if wrapper_facts:
        prompt_text = (PROMPTS / "challenger_v2.md").read_text() + "\n\nDeterministic facts (not model hypotheses):\n" + "\n".join(f"- {f}" for f in wrapper_facts) + "\nA wrapper/use call is not an independent re-implementation — consolidating it would be self-call elimination."
    else:
        prompt_text = (PROMPTS / "challenger_v2.md").read_text()
    # Challenger emits strict raw JSON (assess._parse_strict has no fence
    # handling): json_object mode, as in packet arbitration (M7).
    transport = _transport(url, model, use_json_mode=True,
                           expected_model_id=expected_model_id)
    try:
        final, claims, _ = AS.challenge(packet, assessment, prompt_text, transport)
    except AS.AssessError as exc:
        final = dict(assessment)
        final["action"] = "investigate"
        final["validation"] = {"level": "unresolved",
                               "unresolved": [f"challenge unreadable: {exc}"]}
        claims = [str(exc)]
    return {"assessment": final, "fatal_claims": claims,
            "challenged": assessment["action"] in AS.ACTIONABLE}


def investigate(root: Path, db_path: Path, target: dict,
                investigator: tuple[str, str], challenger: tuple[str, str] | None = None,
                explore_fn=None, card_variant: str = "v2",
                emb: dict | None = None,
                investigator_expected: str | None = None,
                challenger_expected: str | None = None) -> dict:
    """Full A->B->C->D product investigation. Returns the §19 report."""
    t0 = time.monotonic()
    url, model = investigator
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    trow = _unit_row(conn, target["file"], target["symbol"])
    file_hashes = {r[0]: r[1] for r in conn.execute("SELECT path, sha256 FROM files").fetchall()}
    rep: dict = {"target": target, "pipeline": "staged-v1",
                 "investigator": model, "card_contract": CD.CARD_VERSION}
    a = discover(conn, target, trow["id"], explore_fn, emb)
    rep["stage_a"] = a
    rep["retrieval_coverage"] = ("full" if emb is not None else
                                 "reduced: no embedding index (deterministic only)")
    cards = []
    for area in a["candidate_areas"]:
        c = investigate_area(root, conn, area, target, trow["text"],
                             url, model, card_variant,
                             expected_model_id=investigator_expected)
        cards.append(c)
    rep["stage_b"] = cards
    ok_cards = [c for c in cards if c["ok"]]
    if not ok_cards:
        # Fail-closed: no delegated evidence -> investigate, never a
        # transcript-free guess from retrieval alone.
        rep["verdict"] = {"target_responsibility": "", "inspected": [],
                          "related_implementations": [],
                          "best_existing_implementation": None,
                          "recommended_family": [], "action": "investigate",
                          "proposed_boundary": None,
                          "must_remain_separate": ["no valid evidence cards"],
                          "uncertainty": "high"}
        rep["finalists"] = []
        rep["wall_s"] = round(time.monotonic() - t0, 1)
        return rep
    # Finalists: actionable areas + union top-3 + dismissed-but-backed.
    det, _ = RT.all_channel_neighbors(conn, trow["id"], k=12, emb=emb)
    union_ids = []
    for e in det[:12]:
        r = conn.execute("SELECT path, symbol FROM units WHERE id=?", (e["id"],)).fetchone()
        if r:
            union_ids.append(f"{r[0]}::{r[1]}")
    final_ids: list[str] = []
    for c in ok_cards:
        if _rel(c.get("card") or {}) in ("equivalent", "shared_part", "diverged"):
            final_ids += [m for m in c.get("members", []) if m not in final_ids]
    for u in union_ids[:3]:
        if u not in final_ids:
            final_ids.append(u)
    for c in ok_cards:
        if _rel(c.get("card") or {}) not in ("related_only", "unknown"):
            continue
        area = next(x for x in a["candidate_areas"] if x["id"] == c["area"])
        backed = any(m in union_ids for m in c.get("members", [])) or \
            "fastcontext" in area.get("provenance", []) or "harrier" in area.get("provenance", [])
        if backed:
            for m in c.get("members", []):
                if m not in final_ids:
                    final_ids.append(m)
    finalist_rows, budget = [], FINALIST_CHAR_CAP
    for fid in final_ids:
        p, _, s = fid.partition("::")
        r = conn.execute("SELECT path, symbol, start_line, end_line, text FROM units "
                         "WHERE path=? AND symbol=?", (p, s)).fetchone()
        if not r:
            r = conn.execute("SELECT path, symbol, start_line, end_line, text FROM units "
                             "WHERE path LIKE ? AND symbol=?",
                             (f"%{p.split('/')[-1]}", s)).fetchone()
        if not r:
            continue
        chunk = f"### {r[0]}:{r[2]}-{r[3]}\n{r[4]}"
        if budget - len(chunk) < 0 and finalist_rows:
            rep.setdefault("finalist_truncated", []).append(fid)
            continue
        budget -= len(chunk)
        finalist_rows.append({"id": f"{r[0]}::{r[1]}", "path": r[0], "symbol": r[1],
                              "start_line": r[2], "end_line": r[3], "text": r[4]})
    rep["finalists"] = [f["id"] for f in finalist_rows]
    c = arbitrate(root, conn, target, trow["text"], ok_cards,
                  [{"id": f["id"], "text": f["text"]} for f in finalist_rows],
                  url, model, file_hashes,
                  expected_model_id=investigator_expected)
    rep["stage_c"] = {"ok": c["ok"], "errors": c["errors"],
                      "prompt_chars": c["prompt_chars"],
                      "completion_tokens": c["completion_tokens"], "wall_s": c["wall_s"]}
    verdict = c["verdict"] if c["ok"] else {"target_responsibility": "",
                 "inspected": [], "related_implementations": [],
                 "best_existing_implementation": None, "recommended_family": [],
                 "action": "investigate", "proposed_boundary": None,
                 "must_remain_separate": [f"arbitration unreadable: {c['errors'][:1]}"
                                          if c["errors"] else "arbitration failed"],
                 "uncertainty": "high"}
    if c["ok"]:
        kept, dropped = resolve_family(root, conn, verdict.get("recommended_family") or [],
                                         target)
        verdict["recommended_family"] = kept
        rep["family_guardrail"] = {"dropped": dropped, "fired": bool(dropped)}
        if verdict.get("action") in ("reuse_existing", "extract_shared_part",
                                     "share_contract") and not kept:
            verdict["action"] = "investigate"
            verdict["proposed_boundary"] = None
            verdict["must_remain_separate"] = list(verdict.get("must_remain_separate") or []) + [
                "family-id guardrail: no actionable family remains"]
        # Deterministic wrapper/use guard (generic, not T-php-specific):
        # If target text calls a family member, reuse would be self-call
        # elimination, not independent re-implementation. This is the T-php
        # mechanism (caller vs implementation). Only triggers on textual
        # call evidence, so legitimate independent re-implementations
        # (no call) are unaffected.
        if verdict.get("action") == "reuse_existing" and kept:
            tsrc = trow["text"] or ""
            wrapper_hits = []
            for fid in kept:
                sym = fid.split("::")[-1].split("#")[0].split("@")[0].lstrip("$")
                if sym and f"{sym}(" in tsrc:
                    wrapper_hits.append(fid)
            if wrapper_hits:
                verdict["action"] = "keep_separate"
                verdict["recommended_family"] = []
                verdict["proposed_boundary"] = None
                verdict["must_remain_separate"] = list(verdict.get("must_remain_separate") or []) + [
                    f"wrapper/use: target calls {', '.join(wrapper_hits)} — not independent re-implementation"]
                rep["wrapper_guard"] = {"triggered": True, "members": wrapper_hits}
            else:
                rep["wrapper_guard"] = {"triggered": False}
            # Deterministic ownership guardrails (product, tested):
            # G1 pure-forwarder member, G2 generated-artifact member —
            # never canonical reuse targets. Downgrades actionable to
            # investigate with stated reasons (never upgrades).
            if verdict.get("action") in ("reuse_existing", "extract_shared_part",
                                         "share_contract") and kept:
                fam_rows = [r for r in finalist_rows if r["id"] in kept]
                refs_by_symbol: dict[str, list] = {}
                for rr in conn.execute("SELECT symbol, refs FROM units"):
                    try:
                        refs_by_symbol.setdefault(rr[0], []).extend(
                            json.loads(rr[1] or "[]"))
                    except (ValueError, TypeError):
                        continue
                from . import ownership as OW
                verdict, own_reasons = OW.apply(fam_rows, refs_by_symbol,
                                                verdict, root)
                rep["ownership_guard"] = {"fired": bool(own_reasons),
                                          "reasons": own_reasons}
                if own_reasons:
                    verdict["recommended_family"] = []
                    verdict["proposed_boundary"] = None
            else:
                rep.setdefault("ownership_guard", {"fired": False})
        else:
            rep["wrapper_guard"] = {"triggered": False}
            rep.setdefault("ownership_guard", {"fired": False})
    rep["verdict"] = verdict
    if c["ok"] and challenger is not None:
        curl, cmodel = challenger
        d = challenge_and_report(root, conn, target, verdict, finalist_rows, curl, cmodel,
                                 expected_model_id=challenger_expected)
        rep["stage_d"] = {"challenged": d["challenged"], "fatal_claims": d["fatal_claims"]}
        rep["assessment"] = d["assessment"]
    conn.close()
    # §18 accounting roll-up.
    rep["accounting"] = {
        "stage_b_subagents": len(cards),
        "stage_b_peak_live_chars": max([x.get("peak_live_chars", 0) for x in cards] or [0]),
        "arbiter_prompt_chars": c.get("prompt_chars", 0),
        "finalist_source_chars": sum(len(f.get("text", "")) for f in finalist_rows),
        "candidates_active": len(a["candidate_areas"]),
        "candidates_discarded": a["n_dropped_by_cap"],
    }
    rep["wall_s"] = round(time.monotonic() - t0, 1)
    rep["report_sha256"] = hashlib.sha256(
        json.dumps(rep, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return rep
