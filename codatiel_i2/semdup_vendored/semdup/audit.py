"""Audit driver (M2 slice): index -> families -> assess -> challenge -> v1 JSON.

Report-only. Budgets bound the work; remainder is reported deferred with
`partial` status — never a clean negative. Raw prompts/responses live only
in the local ledger file, never in the JSON output or committed evidence.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from . import assess as A
from . import families as fam
from . import fast as FX
from . import feedback as fb
from . import findings as F
from . import inference as I
from . import index_cmd as idx
from . import ledger as L
from . import report as R
from . import retrieve as ret

DETECTOR_VERSION = "0.2.0-m2"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _load_prompt(name: str) -> tuple[str, str]:
    path = Path(__file__).parent / "prompts" / name
    text = path.read_text(encoding="utf-8")
    return text, _sha(text)


def _span(row: dict, sha: str) -> dict:
    span = {"path": row["path"], "start_line": row["start_line"],
            "end_line": row["end_line"], "source_sha256": sha}
    if row.get("symbol"):
        span["symbol"] = row["symbol"]
    return span


def _transport(base_url: str, model: str, api_key: str | None,
               expected: str | None, timeout_s: float, attempts: int):
    def run(messages):  # type: ignore[no-untyped-def]
        return I.chat(base_url, model, messages, api_key=api_key,
                      expected_model_id=expected, timeout_s=timeout_s, attempts=attempts)
    return run


@dataclasses.dataclass
class FamilyCtx:
    """Shared per-family pipeline state (audit and check both use it)."""

    conn: sqlite3.Connection
    judge_run: object
    challenge_run: object
    ledger: L.Ledger
    assessor_text: str
    assessor_hash: str
    challenger_text: str
    challenger_hash: str
    judge_url: str
    judge_model: str
    judge_expected: str | None
    c_url: str
    c_model: str
    c_expected: str | None
    same_endpoint: bool
    max_challenge_calls: int
    timeout_s: float
    attempts: int
    actionable_seen: int = 0
    challenges_spent: int = 0
    challenge_skipped_budget: int = 0
    errors: list = dataclasses.field(default_factory=list)


def member_row(conn: sqlite3.Connection, uid: int) -> dict:
    r = conn.execute(
        "SELECT u.path, u.symbol, u.kind, u.start_line, u.end_line, u.text,"
        " u.fingerprint, u.flags, f.sha256, d.text AS description,"
        " d.provenance AS provenance FROM units u"
        " JOIN files f ON f.path = u.path"
        " LEFT JOIN descriptions d ON d.unit_id = u.id WHERE u.id=?", (uid,)).fetchone()
    d = dict(r)
    d["source_sha256"] = d.pop("sha256")
    d["description"] = d.pop("description") or ""
    d["description_provenance"] = d.pop("provenance") or "none"
    d["flags"] = json.loads(d["flags"]) if isinstance(d["flags"], str) else []
    return d


def _judge_ledger(ctx: FamilyCtx, ts: float, prompt: str, prompt_version: str,
                  packet_sha: str, raw: object, failed: bool, error: str | None) -> None:
    ctx.ledger.add(L.CallRecord(
        ts=ts, role="judge", stage="assessment", endpoint=ctx.judge_url,
        served_model_id=getattr(raw, "served_model_id", None), expected_model_id=ctx.judge_expected,
        same_endpoint_as_judge=True, prompt_sha256=_sha(prompt),
        prompt_chars=len(prompt), prompt_version=prompt_version,
        packet_sha256=packet_sha,
        measured_prompt_tokens=getattr(raw, "prompt_tokens", None),
        measured_completion_tokens=getattr(raw, "completion_tokens", None),
        estimated_prompt_tokens=getattr(raw, "estimated_prompt_tokens",
                                        I.estimate_tokens(prompt)),
        wall_s=time.monotonic() - ts, attempts=getattr(raw, "attempts", ctx.attempts),
        retries=getattr(raw, "attempts", ctx.attempts) - 1, failed=failed, error=error))


def process_family(ctx: FamilyCtx, rank: int, member_ids: list[int],
                   channels: list[str], repo: Path) -> tuple[dict, dict | None]:
    """Run one family: packet -> assess (+repair) -> challenge -> finding.

    Returns (candidate_group, finding|None). Failures append to ctx.errors
    and yield (group, None): the hypothesis stays visible, no finding ships.
    Shared by audit (repo-wide) and check (introduced/worsened only).
    """
    rows = [member_row(ctx.conn, uid) for uid in member_ids]
    packet = A.build_packet(rows)
    group = {"id": f"g{rank}", "rank": rank, "channels": channels,
             "members": [_span(r, r["source_sha256"]) for r in rows]}
    prompt = A.render_prompt(packet, ctx.assessor_text)
    aversion = f"assessor_v1:{ctx.assessor_hash[:12]}"
    t0 = time.monotonic()
    verdict = None
    try:
        raw = ctx.judge_run([{"role": "user", "content": prompt}])  # type: ignore[operator]
        _judge_ledger(ctx, t0, prompt, aversion, packet["packet_sha256"], raw, False, None)
        try:
            verdict = A._parse_strict(raw.content)
            verrs = A._validate_assessment(verdict, packet)
            if verrs:
                raise A.AssessError("assessment rejected: " + "; ".join(verrs))
        except A.AssessError as verr:
            # Bounded schema repair: one retry with violations listed (provenance kept).
            rprompt = A.repair_prompt(raw.content[:4000], [str(verr)[:1500]])
            t0r = time.monotonic()
            rraw = ctx.judge_run([{"role": "user", "content": rprompt}])  # type: ignore[operator]
            ctx.ledger.add(L.CallRecord(
                ts=t0r, role="judge", stage="assessment-repair", endpoint=ctx.judge_url,
                served_model_id=rraw.served_model_id, expected_model_id=ctx.judge_expected,
                same_endpoint_as_judge=True, prompt_sha256=_sha(rprompt),
                prompt_chars=len(rprompt), prompt_version=aversion,
                packet_sha256=packet["packet_sha256"],
                measured_prompt_tokens=rraw.prompt_tokens,
                measured_completion_tokens=rraw.completion_tokens,
                estimated_prompt_tokens=rraw.estimated_prompt_tokens,
                wall_s=time.monotonic() - t0r, attempts=rraw.attempts,
                retries=rraw.attempts - 1, failed=False, error=None))
            verdict = A._parse_strict(rraw.content)
            verrs = A._validate_assessment(verdict, packet)
            if verrs:
                raise A.AssessError("assessment rejected after repair: " + "; ".join(verrs))
    except (A.AssessError, I.InferenceError, I.ModelIdentityError) as exc:
        _judge_ledger(ctx, t0, prompt, aversion, packet["packet_sha256"],
                       type("R", (), {"served_model_id": None, "prompt_tokens": None,
                                      "completion_tokens": None,
                                      "estimated_prompt_tokens": I.estimate_tokens(prompt),
                                      "attempts": ctx.attempts})(),
                       True, f"{type(exc).__name__}: {str(exc)[:280]}")
        ctx.errors.append(f"g{rank}: assessment failed ({type(exc).__name__})")
        return group, None
    # Deterministic ownership guardrails (M6.4b): G1 pure-forwarder and
    # G2 generated-artifact triggers downgrade ACTIONABLE findings to
    # investigate from facts — the judge demonstrably ignores even
    # explicitly provided delegation/provenance evidence (M6.4 ablation).
    # Never upgrades; never touches keep_separate/investigate.
    try:
        import json as _json

        refs_by_symbol: dict[str, list] = {}
        for r in ctx.conn.execute("SELECT symbol, refs FROM units"):
            try:
                refs_by_symbol.setdefault(r[0], [])
                refs_by_symbol[r[0]] += _json.loads(r[1] or "[]")
            except (ValueError, TypeError):
                continue
        from . import ownership as _own

        # Reasons are recorded in the finding itself
        # (observed_differences + validation.unresolved) — a guardrail
        # firing is normal operation, never a run error, so ctx.errors
        # stays clean and status is unaffected.
        verdict, _ = _own.apply(rows, refs_by_symbol, verdict, repo)
    except (sqlite3.Error, OSError, ValueError) as exc:
        ctx.errors.append(f"g{rank}: ownership guardrail skipped ({type(exc).__name__})")
    # Equivalence claims are always challenged; other actionable
    # proposals too. Terminal non-equivalence verdicts need no challenge.
    needs_challenge = verdict["action"] in A.ACTIONABLE or \
        verdict.get("relationship") in ("equivalent", "shared_part")
    if needs_challenge:
        ctx.actionable_seen += 1
        if ctx.challenges_spent >= ctx.max_challenge_calls:
            # Budget exhausted: fail closed — relationship unknown, the
            # actionable claim ships as investigate, never as kept.
            ctx.challenge_skipped_budget += 1
            verdict = A._downgrade_on_fatal(
                verdict, ["challenge budget exhausted; objections unknown"])
            verdict["relationship"] = "unknown"
            ctx.errors.append(f"g{rank}: challenge skipped (budget); downgraded")
        else:
            ctx.challenges_spent += 1
            # Single challenge implementation (assess.challenge); malformed
            # challenger output downgrades fail-closed — an unreadable
            # challenge is an unresolved objection, never a pass.
            t1 = time.monotonic()
            cversion = f"challenger_v2:{ctx.challenger_hash[:12]}"
            try:
                verdict, _claims, diag = A.challenge(packet, verdict, ctx.challenger_text,
                                                     ctx.challenge_run)
                craw = diag["result"]
                cprompt = diag["prompt"]
                ctx.ledger.add(L.CallRecord(
                    ts=t1, role="challenge", stage="challenge", endpoint=ctx.c_url,
                    served_model_id=craw.served_model_id, expected_model_id=ctx.c_expected,
                    same_endpoint_as_judge=ctx.same_endpoint,
                    prompt_sha256=_sha(cprompt), prompt_chars=len(cprompt),
                    prompt_version=cversion, packet_sha256=packet["packet_sha256"],
                    measured_prompt_tokens=craw.prompt_tokens,
                    measured_completion_tokens=craw.completion_tokens,
                    estimated_prompt_tokens=craw.estimated_prompt_tokens,
                    wall_s=time.monotonic() - t1, attempts=craw.attempts,
                    retries=craw.attempts - 1, failed=False, error=None))
            except (A.AssessError, I.InferenceError, I.ModelIdentityError) as exc:
                cprompt = A._challenge_prompt(packet, verdict, ctx.challenger_text)
                ctx.ledger.add(L.CallRecord(
                    ts=t1, role="challenge", stage="challenge", endpoint=ctx.c_url,
                    served_model_id=None, expected_model_id=ctx.c_expected,
                    same_endpoint_as_judge=ctx.same_endpoint,
                    prompt_sha256=_sha(cprompt), prompt_chars=len(cprompt),
                    prompt_version=cversion, packet_sha256=packet["packet_sha256"],
                    measured_prompt_tokens=None, measured_completion_tokens=None,
                    estimated_prompt_tokens=I.estimate_tokens(cprompt),
                    wall_s=time.monotonic() - t1, attempts=ctx.attempts,
                    retries=ctx.attempts - 1, failed=True, error=str(exc)[:300]))
                ctx.errors.append(f"g{rank}: challenge failed ({type(exc).__name__}); objection unresolved")
                verdict = A._downgrade_on_fatal(verdict, [f"challenge {type(exc).__name__}; objections unknown"])
                verdict["relationship"] = "unknown"
    try:
        return group, F.to_finding(verdict, packet, f"f{rank}", repo)
    except F.FindingError as exc:
        ctx.errors.append(f"g{rank}: finding rejected ({exc})")
        return group, None


def run_fast_phase(conn: sqlite3.Connection, id_rows: dict, ledger: L.Ledger,
                   fast_url: str, fast_model: str, fast_key: str | None,
                   fast_expected: str | None, max_fast_calls: int,
                   timeout_s: float) -> tuple[list, dict]:
    """Bounded explorer pass: one call per query unit, hints validated.

    Returns ([([id_a, id_b], ["fast"], subsource)] novel-or-not pairs,
    report). Never vetoes: pairs only added. Failures yield zero hints
    with a ledgered failure — never junk, never fabrication.
    """
    prompt_text = Path(__file__).parent.joinpath("prompts", "fast_v1.md").read_text(encoding="utf-8")
    prompt_hash = _sha(prompt_text)
    peer_index = [{"path": u["path"], "symbol": u["symbol"]} for u in sorted(
        id_rows.values(), key=lambda u: (u["path"], u["symbol"]))]
    pairs: list = []
    report: dict = {"enabled": True, "calls": 0, "failed_calls": 0,
                    "hints_validated": 0, "hints_dropped": 0, "novel_pairs": 0,
                    "corroborated_pairs": 0, "units": [],
                    "model": fast_model, "prompt_version": f"fast_v1:{prompt_hash[:12]}"}
    transport = _transport_fast(fast_url, fast_model, fast_key, fast_expected, timeout_s)
    for uid in sorted(id_rows):
        if report["calls"] >= max_fast_calls:
            break
        unit = member_row(conn, uid)
        unit_ref = f"{unit['path']}:{unit['symbol']}"
        unit_info = {"path": unit["path"], "symbol": unit["symbol"],
                     "start_line": unit["start_line"], "end_line": unit["end_line"],
                     "text": unit["text"], "description": unit["description"],
                     "description_provenance": unit["description_provenance"]}
        # Prompt text is deterministic in its inputs, so the fingerprint is
        # available even when the call itself fails.
        prompt_preview = FX.render_prompt(unit_info, prompt_text, peer_index)
        t0 = time.monotonic()
        try:
            hints, prompt, raw, shaped_dropped = FX.suggest(unit_info, peer_index, prompt_text, transport)
            resolved, stats = FX.resolve(conn, uid, hints)
            stats["shape_dropped"] = shaped_dropped
            ledger.add(L.CallRecord(
                ts=t0, role="fast", stage="exploration", endpoint=fast_url,
                served_model_id=raw.served_model_id, expected_model_id=fast_expected,
                same_endpoint_as_judge=False, prompt_sha256=_sha(prompt),
                prompt_chars=len(prompt), prompt_version=report["prompt_version"],
                packet_sha256=None, measured_prompt_tokens=raw.prompt_tokens,
                measured_completion_tokens=raw.completion_tokens,
                estimated_prompt_tokens=raw.estimated_prompt_tokens,
                wall_s=time.monotonic() - t0, attempts=raw.attempts,
                retries=raw.attempts - 1, failed=False, error=None,
                unit=unit_ref))
            report["calls"] += 1
            report["hints_validated"] += stats["hints_valid"] + stats["expansion_hits"] + stats["helpers_valid"]
            report["hints_dropped"] += stats["hints_dropped"] + shaped_dropped
            report["units"].append({"unit": unit_ref, "stats": stats})
            for peer_id, subsource in resolved:
                pairs.append(([uid, peer_id], ["fast"], subsource))
        except (ValueError, I.InferenceError, I.ModelIdentityError) as exc:
            ledger.add(L.CallRecord(
                ts=t0, role="fast", stage="exploration", endpoint=fast_url,
                served_model_id=None, expected_model_id=fast_expected,
                same_endpoint_as_judge=False, prompt_sha256=_sha(prompt_preview),
                prompt_chars=len(prompt_preview), prompt_version=report["prompt_version"],
                packet_sha256=None, measured_prompt_tokens=None,
                measured_completion_tokens=None, estimated_prompt_tokens=0,
                wall_s=time.monotonic() - t0, attempts=1,
                retries=0, failed=True, error=str(exc)[:300],
                unit=unit_ref))
            report["calls"] += 1
            report["failed_calls"] += 1
            report["units"].append({"unit": unit_ref,
                                    "error": type(exc).__name__})
    # Triples are (pair, channels, subsource); merge subsource into report, strip for families.
    merged: dict[frozenset, list] = {}
    sources: dict[frozenset, list] = {}
    for (a, b), ch, subsource in pairs:
        key = frozenset({a, b})
        merged[key] = sorted(set(merged.get(key, [])) | set(ch))
        sources.setdefault(key, []).append(subsource)
    report["pair_sources"] = {f"{sorted(k)}": sorted(set(v)) for k, v in sources.items()}
    return [(sorted(k), v) for k, v in merged.items()], report


def _transport_fast(base_url: str, model: str, api_key: str | None,
                    expected: str | None, timeout_s: float):
    def run(messages):  # type: ignore[no-untyped-def]
        # Single attempt: hints are enrichment, not load-bearing; a failed
        # call contributes nothing rather than costing a retry.
        # Grammar-constrained JSON (llama.cpp response_format), same
        # provider-constrained mechanism as the judge path.
        return I.chat(base_url, model, messages, api_key=api_key,
                      expected_model_id=expected, timeout_s=timeout_s, attempts=1,
                      max_tokens=512, use_json_mode=True)
    return run


def audit(
    repo: Path,
    db_path: Path,
    out_path: Path,
    ledger_path: Path,
    *,
    judge_url: str,
    judge_model: str,
    judge_key: str | None,
    judge_expected: str | None,
    challenge_url: str | None = None,
    challenge_model: str | None = None,
    challenge_key: str | None = None,
    challenge_expected: str | None = None,
    max_families: int = 10,
    max_challenge_calls: int = 20,
    neighbors_k: int = 30,
    feedback_path: Path | None = None,
    snapshot_scope: str = "selected-view",
    fast_url: str | None = None,
    fast_model: str = "",
    fast_key: str | None = None,
    fast_expected: str | None = None,
    max_fast_calls: int = 80,
    timeout_s: float = 180.0,
    attempts: int = 2,
) -> dict:
    started = time.monotonic()
    summary = idx.build(repo, db_path, snapshot_scope=snapshot_scope)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ledger = L.Ledger(ledger_path)
    assessor_text, assessor_hash = _load_prompt("assessor_v1.md")
    challenger_text, challenger_hash = _load_prompt("challenger_v2.md")
    config_sha = _sha(json.dumps({"judge": [judge_url, judge_model], "k": neighbors_k,
                                  "max_families": max_families, "prompts": [assessor_hash, challenger_hash],
                                  "snapshot_scope": snapshot_scope},
                                 sort_keys=True))

    # Families: retrieval pairs grown only with full pairwise support
    # (families.grow: clique steps, never transitive merges), then assessed.
    id_rows = {}
    for r in conn.execute(
            "SELECT id, path, symbol, kind FROM units WHERE kind IN ('function','closure','initializer')"):
        id_rows[r[0]] = {"id": r[0], "path": r[1], "symbol": r[2], "kind": r[3]}
    pair_families = fam.form_families(conn, neighbors_k)
    det_keys = {frozenset(p) for p, _ in pair_families}
    fast_report: dict = {"enabled": fast_url is not None, "calls": 0, "failed_calls": 0,
                         "hints_validated": 0, "hints_dropped": 0, "novel_pairs": 0,
                         "corroborated_pairs": 0, "units": []}
    fast_novel: set[frozenset] = set()
    if fast_url:
        fast_pairs, fast_report = run_fast_phase(
            conn, id_rows, ledger, fast_url, fast_model, fast_key, fast_expected,
            max_fast_calls, timeout_s)
        for (a, b), ch in fast_pairs:
            key = frozenset({a, b})
            if key in det_keys:
                # Corroboration, not a new pair: deterministic channels stand
                # (assessment order unchanged); confidence noted in sidecar.
                fast_report["corroborated_pairs"] += 1
                continue
            det_keys.add(key)
            fast_novel.add(key)
            pair_families.append(([a, b], ch))
    grown = fam.grow(pair_families)
    # Deterministic-first assessment order: families containing a novel
    # fast edge sort after pure-deterministic families (stable). A novel
    # family can therefore never displace a deterministic one from a
    # finite budget — no veto by displacement, by construction.
    grown.sort(key=lambda f: (any(frozenset({f[0][i], f[0][j]}) in fast_novel
                                 for i in range(len(f[0])) for j in range(i + 1, len(f[0]))),
                              -len(f[0])))
    families = [([id_rows[i] for i in ids], ch) for ids, ch in grown]

    same_endpoint = not challenge_url
    c_url = challenge_url or judge_url
    c_model = challenge_model or judge_model
    c_key = challenge_key if challenge_url else judge_key
    c_expected = challenge_expected if challenge_url else judge_expected
    judge_run = _transport(judge_url, judge_model, judge_key, judge_expected, timeout_s, attempts)
    challenge_run = _transport(c_url, c_model, c_key, c_expected, timeout_s, attempts)
    ctx = FamilyCtx(
        conn=conn, judge_run=judge_run, challenge_run=challenge_run, ledger=ledger,
        assessor_text=assessor_text, assessor_hash=assessor_hash,
        challenger_text=challenger_text, challenger_hash=challenger_hash,
        judge_url=judge_url, judge_model=judge_model, judge_expected=judge_expected,
        c_url=c_url, c_model=c_model, c_expected=c_expected, same_endpoint=same_endpoint,
        max_challenge_calls=max_challenge_calls, timeout_s=timeout_s, attempts=attempts)

    candidate_groups = []
    findings = []
    examined = 0
    rank = 0
    # Worklist: grown families first; a grown family whose assessment fails
    # falls back to its constituent retrieval pairs (no recall loss vs the
    # pair-only baseline). Budget caps assessed families; the remainder is
    # deferred, never silently dropped.
    worklist = [([m["id"] for m in members], channels) for members, channels in families]
    seen_keys = set()
    assessed = 0
    while worklist and assessed < max_families:
        member_ids, channels = worklist.pop(0)
        key = frozenset(member_ids)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rank += 1
        assessed += 1
        group, finding = process_family(ctx, rank, member_ids, channels, repo)
        candidate_groups.append(group)
        if finding is not None:
            findings.append(finding)
            examined += 1
        elif len(member_ids) > 2:
            for i in range(len(member_ids)):
                for j in range(i + 1, len(member_ids)):
                    worklist.append(([member_ids[i], member_ids[j]], channels))
    errors = ctx.errors
    conn.close()

    emitted = len(candidate_groups)
    deferred = len(worklist) + (emitted - examined)
    # Fast survival accounting: which novel fast pairs reached an examined
    # family, and which contributed to an actionable (accepted) finding.
    fast_survival = {"examined": 0, "accepted": 0}
    if fast_report.get("enabled"):
        group_sets = [{(m["path"], m.get("symbol")) for m in g["members"]} for g in candidate_groups]
        finding_sets = [{(m["path"], m.get("symbol")) for m in f["members"]}
                        for f in findings if f["action"] in A.ACTIONABLE]
        id_key = {uid: (u["path"], u["symbol"]) for uid, u in id_rows.items()}
        for key in fast_novel:
            members = {id_key[i] for i in key}
            if any(members <= g for g in group_sets):
                fast_survival["examined"] += 1
            if any(members <= g for g in finding_sets):
                fast_survival["accepted"] += 1
        fast_report["novel_pairs"] = len(fast_novel)
        fast_report["survival"] = fast_survival
    # Stable feedback: normalized responsibility + member hashes with an
    # owner keep_separate decision suppresses re-report; any content change
    # (or a genuinely reworded claim) reopens. Never file-wide.
    suppressed = 0
    if feedback_path is not None:
        records = fb.load(feedback_path)
        kept = []
        for finding in findings:
            hit = fb.match(records, finding["responsibility"], finding["members"])
            if hit is not None and hit.get("decision") == "keep_separate":
                suppressed += 1
            else:
                kept.append(finding)
        findings = kept
    coverage = {"files_eligible": summary["files_eligible"], "files_parsed": summary["files_parsed"],
                "files_failed": summary["files_failed"], "units_indexed": summary["units_indexed"],
                "units_searched": summary["units_searched"], "candidate_groups_emitted": emitted,
                "candidate_groups_examined": examined, "groups_deferred": deferred}
    status = "complete"
    if errors or summary["files_failed"] or deferred:
        status = "partial" if examined else "failed"
    result = {"schema_version": 1, "run_id": f"m2-{uuid.uuid4().hex[:8]}",
              "detector_version": DETECTOR_VERSION, "config_sha256": config_sha,
              "snapshot_sha256": summary["snapshot"], "status": status, "errors": errors,
              "coverage": coverage, "usage": ledger.rollup(),
              "candidate_groups": candidate_groups, "findings": findings}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    tmp.rename(out_path)
    fast_note = "fast explorer: off"
    if fast_report.get("enabled"):
        out_path.with_suffix(".fast.json").write_text(
            json.dumps(fast_report, indent=2, default=str) + "\n", encoding="utf-8")
        fast_note = (f"fast explorer: {fast_report['calls']} calls "
                     f"({fast_report['failed_calls']} failed), "
                     f"{fast_report['novel_pairs']} novel pairs "
                     f"(examined {fast_survival['examined']}, accepted {fast_survival['accepted']}), "
                     f"{fast_report['corroborated_pairs']} corroborated")
    report_text = R.render(f"audit {repo.name}", findings, coverage, result["usage"],
                           [f"ledger: {ledger_path} ({len(ledger.records)} calls)",
                            f"challenge coverage: {ctx.challenges_spent}/{ctx.actionable_seen} actionable challenged "
                            f"(budget {max_challenge_calls}, skipped {ctx.challenge_skipped_budget})",
                            f"feedback suppressed: {suppressed}",
                            fast_note,
                            f"wall: {time.monotonic() - started:.1f}s"])
    return {"result": result, "report": report_text, "ledger_records": len(ledger.records)}


def main(argv: list[str] | None = None) -> int:
    import os
    p = argparse.ArgumentParser(prog="semdup-audit")
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--max-families", type=int, default=10)
    p.add_argument("--snapshot-scope", type=str, default="selected-view")
    a = p.parse_args(argv)
    judge_url = os.environ.get("SEMDUP_JUDGE_URL", "")
    if not judge_url:
        print("no endpoint configured (SEMDUP_JUDGE_URL unset); refusing to fabricate")
        return 3
    out = audit(a.repo, a.db, a.output, a.ledger,
                judge_url=judge_url, judge_model=os.environ.get("SEMDUP_JUDGE_MODEL", ""),
                judge_key=os.environ.get("SEMDUP_JUDGE_KEY"),
                judge_expected=os.environ.get("SEMDUP_JUDGE_EXPECTED"),
                challenge_url=os.environ.get("SEMDUP_LARGE_URL"),
                challenge_model=os.environ.get("SEMDUP_LARGE_MODEL", ""),
                challenge_key=os.environ.get("SEMDUP_LARGE_KEY"),
                challenge_expected=os.environ.get("SEMDUP_LARGE_EXPECTED"),
                max_families=a.max_families,
                snapshot_scope=a.snapshot_scope,
                fast_url=os.environ.get("SEMDUP_FAST_URL"),
                fast_model=os.environ.get("SEMDUP_FAST_MODEL", ""),
                fast_key=os.environ.get("SEMDUP_FAST_KEY"),
                fast_expected=os.environ.get("SEMDUP_FAST_EXPECTED"))
    print(out["report"])
    return {"complete": 0, "partial": 2, "failed": 3}[out["result"]["status"]]
