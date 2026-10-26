"""Canonical Codatiel I2 review service.

One supported flow: PR/local pair -> pinned BASE/HEAD workspace -> change
inventory -> deterministic candidate prep -> SDK agent investigation ->
finding disposition -> trust validation -> immutable run record -> shadow
result. No custom review loop, no static fallback: a run with zero
model-directed tool calls is recorded as incomplete, never as a review.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from codatiel_i2.common.jsonio import write_json, write_jsonl
from codatiel_i2.core import diff as diff_mod
from codatiel_i2.core import files as files_mod
from codatiel_i2.core import runs as runs_mod
from codatiel_i2.core.coverage import empty_coverage
from codatiel_i2.core.evidence import replay_source_evidence
from codatiel_i2.core.spend import (
    CONFIRMED_USAGE,
    CONFIRMED_ZERO_USAGE,
    UNKNOWN_USAGE,
    SpendLedger,
)
from codatiel_i2.core.findings import (
    DETECTORS,
    is_verified_proof_kind,
)
from codatiel_i2 import final as final_mod
from codatiel_i2.core.workspace import (
    Workspace,
    fetch_github_pr_metadata,
    prepare_github_workspace,
    prepare_local_workspace,
)
from codatiel_i2.detectors.docs_code import detector as docs_code_detector
from codatiel_i2.verify import evidence_validation as evidence_validation_mod
from codatiel_i2.verify import trust as trust_mod

SERVICE_VERSION = "canonical-service-0.1"


def _sha256_text(text: str) -> str:
    """Content hash for prompt provenance (M9.2 P1)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def instructions_id_for(name: str, text: str) -> str:
    """Content-derived instructions ID: <name>@<sha256[:12]>."""
    return f"{name}@{_sha256_text(text)[:12]}"


def verify_playbook_containment(instructions: str, playbook_text: str,
                                playbook_sha256: str) -> bool:
    """True iff the submitted instructions contain the declared playbook
    content whose hash matches. Used by the P1 regression test and P3 arms."""
    if _sha256_text(playbook_text) != playbook_sha256:
        return False
    return playbook_text in instructions


# Current inventory strategy: deterministic changed-file list rendered by
# _render_inventory. Generic triage inventories get their own strategy name.
INVENTORY_STRATEGY = "file-list-0.1"

# Constants for trace artifacts — avoids Sonar literal duplication
_TRANSCRIPT_SCHEMA = "codatiel_i2.transcript.v1"
_SHADOW_SCHEMA = "codatiel_i2.shadow_result.v1"
_RUN_SCHEMA = "codatiel_i2.run.v1"

CANONICAL_INSTRUCTIONS = """You are Codatiel I2, a self-hosted semantic regression \
scanner. Scope: contracts, authorization, docs_code, operating_constraints, \
semantic_twins regressions only. No style, naming, or general advice.

Investigate with the available source tools. Changed files are listed in the \
task; fetch every span you rely on yourself with read_file/diff_window/\
search_code against the pinned BASE and HEAD revisions. Do not assert the \
contents of any file you have not read. Note uncertainty explicitly.

Final message must be one JSON object, no other text:
{"status": "finding"|"silence", "summary": "...", "findings": [{"detector": \
"contracts|authorization|docs_code|operating_constraints|semantic_twins", \
"title": "...", "path": "repo-relative path", "revision": "base|head", \
"line_start": 1, "line_end": 1, "claim": "what changed and why it matters", \
"evidence_ids": ["ev_... tool evidence ids you actually received"]}]}

For silence: {"status": "silence", "summary": "what you checked", "findings": []}.
A claim without tool evidence is not a finding."""

# M9 challenger: impact-path investigation. Same scope and tools, different
# investigative discipline. Organize around plausible behavioral regressions:
# Changed behavior → affected consumer/contract → triggering condition →
# observable failure → developer impact. Prioritize behavioral changes,
# changed contracts, callers/callees, state transitions, boundary conditions,
# and integration consequences over file-coverage. Form 2-3 concrete
# hypotheses ranked by materiality/plausibility, direct tools to confirm or
# falsify each, spend budget on promising paths, abandon weak hypotheses when
# contradictory evidence appears. For obvious, directly evidenced bugs, do not
# require an artificial minimum number of tool calls or cross-file investigation.
CHALLENGER_INSTRUCTIONS = """You are Codatiel I2, impact-path reviewer. Scope: \
contracts, authorization, docs_code, operating_constraints, semantic_twins \
only. No style or naming.

Investigate by forming and testing behavioral regression hypotheses. For each \
plausible risk, articulate: Changed behavior → affected consumer or contract \
→ triggering condition (input/state/path) → observable failure → \
developer-relevant impact. Prioritize behavioral changes, changed contracts, \
callers/callees, state transitions, boundary conditions, and integration \
consequences. Rank 2-3 concrete hypotheses by materiality and plausibility and \
direct read_file/diff_window/search_code/find_definition/find_callers at the \
pinned BASE and HEAD to confirm or falsify each. Spend budget on promising \
paths; abandon weak hypotheses when contradictory evidence appears. Do not \
require an artificial minimum number of tool calls or cross-file investigation \
for an obvious, directly evidenced defect. Do not assert file contents you have \
not read.

For each confirmed material regression, the finding must establish where \
relevant: what changed, the triggering condition, what breaks and why it \
matters, revision-bound evidence, and a useful location with correction \
direction.

Final message must be one JSON object, no other text:
{"status": "finding"|"silence", "summary": "...", "findings": [{"detector": \
"contracts|authorization|docs_code|operating_constraints|semantic_twins", \
"title": "...", "path": "repo-relative path", "revision": "base|head", \
"line_start": 1, "line_end": 1, "claim": "what changed, triggering condition, \
what breaks and why it matters", "evidence_ids": ["ev_... tool evidence ids"]}]}

For silence: {"status": "silence", "summary": "what you checked and why no \
material regression is present", "findings": []}. A claim without tool evidence \
is not a finding."""

CHALLENGER_INSTRUCTIONS_VERSION = (
    "challenger-impact-path-0.1 (AGENTS.md; REVIEWER_OUTPUT_CONTRACT "
    "v1.0-draft; REVIEWER_INVESTIGATION_GUIDEBOOK v1.0-draft; M9)"
)

# M9.2 P1: truthful, content-derived provenance. The historical
# INSTRUCTIONS_VERSION claimed (AGENTS.md; OUTPUT_CONTRACT; GUIDEBOOK) but
# none of those documents was ever sent — see PROVENANCE_CORRECTION.md.
# Labels below are derived from the exact bytes passed to the model.
CANONICAL_INSTRUCTIONS_ID = instructions_id_for(
    "canonical-minimal-0.1", CANONICAL_INSTRUCTIONS)
CHALLENGER_INSTRUCTIONS_ID = instructions_id_for(
    "challenger-impact-path-0.1", CHALLENGER_INSTRUCTIONS)
# Truthful replacement for the false historical label.
INSTRUCTIONS_VERSION = CANONICAL_INSTRUCTIONS_ID


def review_local(repo_root: Path | str, base: str, head: str, *, model: Any,
                 model_name: str, runs_root: Path | str,
                 work_root: Path | str, max_turns: int = 12,
                 model_settings: Any = None,
                 rate: dict[str, Any] | None = None,
                 rate_status: str = "unconfirmed",
                 max_usd_per_review: float | None = None,
                 tool_budget: int = 40,
                 variant: str = "baseline",
                 playbook_name: str | None = None,
                 playbook_text: str | None = None,
                 inventory_strategy: str = INVENTORY_STRATEGY, enable_final: bool = False) -> dict[str, Any]:
    """Run one canonical review over a local repository revision pair."""
    ws = prepare_local_workspace(Path(repo_root), base, head,
                                 Path(work_root))
    if ws.status != "ready":
        return {"status": "failed", "stage": "workspace", "error": ws.error,
                "service_version": SERVICE_VERSION}
    return _run_review(ws, diff_repo=Path(repo_root), model=model,
                       model_name=model_name, runs_root=Path(runs_root),
                       max_turns=max_turns, model_settings=model_settings,
                       rate=rate, rate_status=rate_status,
                       max_usd_per_review=max_usd_per_review,
                       tool_budget=tool_budget, variant=variant,
                       playbook_name=playbook_name,
                       playbook_text=playbook_text,
                       inventory_strategy=inventory_strategy,
                       enable_final=enable_final)


def review_pr(repo: str, pr: int, *, model: Any, model_name: str,
              runs_root: Path | str, work_root: Path | str,
              max_turns: int = 12,
              model_settings: Any = None,
              rate: dict[str, Any] | None = None,
              rate_status: str = "unconfirmed",
              max_usd_per_review: float | None = None,
              tool_budget: int = 40,
               variant: str = "baseline",
               expected_base: str | None = None,
               expected_head: str | None = None,
               playbook_name: str | None = None,
               playbook_text: str | None = None,
               inventory_strategy: str = INVENTORY_STRATEGY, enable_final: bool = False) -> dict[str, Any]:
    """Run one canonical review over a GitHub PR (base/head from the PR).

    When ``expected_base``/``expected_head`` are given (frozen roster SHAs),
    the live PR metadata must match them exactly (full SHA, or unambiguous
    prefix); otherwise the run aborts as ``failed`` at stage
    ``sha_mismatch`` instead of silently reviewing drifted revisions.
    """
    metadata, error = fetch_github_pr_metadata(repo, pr)
    if metadata is None:
        return {"status": "failed", "stage": "pr_metadata", "error": error,
                "service_version": SERVICE_VERSION}
    live_base = str(metadata.get("baseRefOid") or "")
    live_head = str(metadata.get("headRefOid") or "")
    for label, expected, live in (("base", expected_base, live_base),
                                  ("head", expected_head, live_head)):
        if expected and live and not live.startswith(expected) and not expected.startswith(live):
            return {"status": "failed", "stage": "sha_mismatch",
                    "error": (f"frozen {label} {expected[:12]} != live PR {label} "
                              f"{live[:12]}; refusing to review drifted revision"),
                    "service_version": SERVICE_VERSION,
                    "repo": repo, "pr": pr,
                    "expected_base": expected_base, "expected_head": expected_head,
                    "live_base": live_base, "live_head": live_head}
    ws = prepare_github_workspace(repo, pr, Path(work_root))
    if ws.status != "ready":
        return {"status": "failed", "stage": "workspace", "error": ws.error,
                "service_version": SERVICE_VERSION}
    return _run_review(ws, diff_repo=Path(ws.head_dir), model=model,
                       model_name=model_name, runs_root=Path(runs_root),
                       max_turns=max_turns, model_settings=model_settings,
                       rate=rate, rate_status=rate_status,
                       max_usd_per_review=max_usd_per_review,
                       tool_budget=tool_budget,
                       pr_number=pr, variant=variant,
                       playbook_name=playbook_name,
                       playbook_text=playbook_text,
                       inventory_strategy=inventory_strategy,
                       enable_final=enable_final)


def _collect_provider_termination(model: Any) -> dict[str, str]:
    """Collect provider stop_reason map from model and wrapped model."""
    out: dict[str, str] = {}
    for mdl in (model, getattr(model, "_model", None) if model is not None else None):
        if mdl is None:
            continue
        for attr in ("_termination_by_response", "termination_by_response"):
            bucket = getattr(mdl, attr, None)
            if isinstance(bucket, dict) and bucket:
                out.update(bucket)
    return out


def _guidebook_port_text() -> str:
    """Guidebook prompt text: repo checkout first, packaged copy fallback.

    The v0.1.0 image installs only the package, so the repo-relative file
    is absent there; the packaged copy (same bytes, content-addressed by
    GUIDEBOOK_PORT_ID at import) keeps imports working everywhere.
    """
    repo_path = (Path(__file__).resolve().parent.parent / "docs"
                 / "reviewer_methodology" / "guidebook_port.md")
    if repo_path.exists():
        return repo_path.read_text(encoding="utf-8")
    import importlib.resources as _resources
    return (_resources.files("codatiel_i2") / "guidebook_port.md"
            ).read_text(encoding="utf-8")


GUIDEBOOK_PORT_INSTRUCTIONS = _guidebook_port_text()
GUIDEBOOK_PORT_ID = instructions_id_for(
    "guidebook-port-0.1", GUIDEBOOK_PORT_INSTRUCTIONS)


def _prompt_provenance(*, instructions: str, instructions_id: str,
                       user_input: str, inventory: str,
                       inventory_strategy: str = INVENTORY_STRATEGY,
                       playbook_name: str | None = None,
                       playbook_text: str | None = None,
                       boundary_sha256: str | None = None) -> dict[str, Any]:
    """M9.2 P1 prompt-provenance record: exact rendered bytes + hashes.

    Labels are content-derived. When a playbook is declared, the record
    carries its hash and whether the submitted instructions contain it.
    """
    instructions_sha = _sha256_text(instructions)
    playbook_sha = _sha256_text(playbook_text) if playbook_text else None
    return {
        "schema_version": "codatiel_i2.prompt_provenance.v1",
        "instructions_id": instructions_id,
        "instructions_sha256": instructions_sha,
        "instructions": instructions,
        "user_input_first_message": user_input,
        "user_input_sha256": _sha256_text(user_input),
        "inventory_strategy": inventory_strategy,
        "inventory": inventory,
        "inventory_sha256": _sha256_text(inventory),
        "declared_playbook": playbook_name,
        "declared_playbook_sha256": playbook_sha,
        "playbook_content_match": (
            verify_playbook_containment(instructions, playbook_text,
                                        playbook_sha)
            if playbook_text and playbook_sha else None),
        "sdk_boundary_instructions_sha256": boundary_sha256,
        "boundary_match": (boundary_sha256 == instructions_sha
                           if boundary_sha256 else None),
    }


def _run_review(ws: Workspace, *, diff_repo: Path, model: Any,
               model_name: str, runs_root: Path, max_turns: int,
               model_settings: Any,
               rate: dict[str, Any] | None = None,
               rate_status: str = "unconfirmed",
               max_usd_per_review: float | None = None,
               tool_budget: int = 40,
               pr_number: int | None = None,
               variant: str = "baseline",
               playbook_name: str | None = None,
               playbook_text: str | None = None,
               inventory_strategy: str = INVENTORY_STRATEGY, enable_final: bool = False) -> dict[str, Any]:
    import os

    from codatiel_i2 import providers as providers_mod
    from codatiel_i2 import runtime_sdk

    runtime_sdk.ensure_local_tracing()
    # FINAL-01: reserve one turn/tool for final disposition
    if enable_final:
        # Reserve 1 tool and 1 turn for final; investigation gets 39/11
        tool_budget = max(1, tool_budget - 1)
        max_turns = max(1, max_turns - 1)

    started = time.time()
    base_sha, head_sha = ws.base_sha, ws.head_sha
    base_dir, head_dir = Path(ws.base_dir), Path(ws.head_dir)
    changes = diff_mod.changed_files(diff_repo, base_sha, head_sha)
    classification = files_mod.classify_changes(changes)

    det_evidence: list[dict] = []
    det_verifications: list[dict] = []
    det_findings: list[dict] = []
    det_candidates: list[dict] = []
    det_facts: list[dict] = []
    det_proofs: list[dict] = []
    det_coverage: dict[str, Any] = {}
    try:
        scan = docs_code_detector.scan_pair(
            base_dir=base_dir, head_dir=head_dir, base_sha=base_sha,
            head_sha=head_sha, repo=ws.repo, changes=changes)
        det_evidence = scan.get("evidence", [])
        det_verifications = scan.get("verifications", [])
        det_findings = scan.get("findings", [])
        det_candidates = scan.get("candidates", [])
        det_facts = scan.get("facts", [])
        det_proofs = scan.get("proofs", [])
        det_coverage = scan.get("coverage", {})
    except Exception as exc:
        det_coverage = {"docs_code": {"error": f"{type(exc).__name__}: {exc}"}}

    ctx = runtime_sdk.ReviewContext(
        repo=ws.repo, base_sha=base_sha, head_sha=head_sha,
        base_dir=base_dir, head_dir=head_dir, diff_repo=diff_repo,
        tool_budget=tool_budget)
    tools = runtime_sdk.build_i2_tools(ctx)
    # Variant-aware instructions and inventory: challenger uses impact-path
    # discipline while baseline keeps the original file-coverage prompt.
    # M9.2 P1: the recorded ID is derived from the exact submitted bytes;
    # an optional playbook is prepended verbatim and declared with its hash.
    if variant == "challenger":
        base_instructions = CHALLENGER_INSTRUCTIONS
        base_name = "challenger-impact-path-0.1"
    else:
        base_instructions = CANONICAL_INSTRUCTIONS
        base_name = "canonical-minimal-0.1"
    if playbook_text:
        instructions = playbook_text + "\n\n" + base_instructions
        instructions_id = instructions_id_for(
            playbook_name or "playbook", instructions)
    else:
        instructions = base_instructions
        instructions_id = instructions_id_for(base_name, instructions)
    instructions_version = instructions_id
    # M9.2 P3 arms: A/B use the file-list inventory; C uses the generic
    # deterministic triage inventory. Strategy recorded in provenance.
    # CONTEXT-01 arms: bundle-0.1 (prepared change bundle, Feature A)
    # and bundle-cite-0.1 (bundle plus citable changed-line evidence,
    # Feature B). The bundle replaces the inventory in the first message
    # (stable prefix, cache-friendly); prompt instructions, tools and
    # budgets are identical across arms.
    bundle_evidence: list[dict] = []
    bundle_meta: dict[str, Any] | None = None
    if inventory_strategy in ("bundle-0.1", "bundle-cite-0.1"):
        from codatiel_i2 import context_bundle as bundle_mod
        with_ev = inventory_strategy == "bundle-cite-0.1"
        bundle = bundle_mod.build_change_bundle(
            diff_repo=diff_repo, base_sha=base_sha, head_sha=head_sha,
            changes=changes, classification=classification,
            base_dir=base_dir, head_dir=head_dir, with_evidence=with_ev)
        inventory = bundle.text
        inventory_strategy_name = inventory_strategy
        bundle_evidence = list(bundle.evidence)
        bundle_meta = {"schema": bundle_mod.BUNDLE_SCHEMA,
                       "chars": len(bundle.text),
                       "evidence_count": len(bundle.evidence),
                       "evidence_total": bundle.evidence_total,
                       "evidence_omitted": bundle.evidence_omitted,
                       "omitted_files": len(bundle.omitted_files),
                       "omitted_runs": bundle.omitted_runs,
                       "truncated": bundle.truncated}
    elif inventory_strategy == "triage-0.1":
        from codatiel_i2 import triage as triage_mod
        inventory = triage_mod.render_triage_inventory(
            changes, classification, det_candidates, diff_repo=diff_repo,
            head_dir=head_dir, base_sha=base_sha, head_sha=head_sha)
        inventory_strategy_name = triage_mod.TRIAGE_INVENTORY_STRATEGY
    elif inventory_strategy == INVENTORY_STRATEGY:
        inventory = _render_inventory(changes, classification, det_candidates,
                                      variant=variant)
        inventory_strategy_name = INVENTORY_STRATEGY
    else:
        return {"status": "failed", "stage": "inventory_strategy",
                "error": f"unknown inventory_strategy {inventory_strategy!r}; "
                         f"expected {INVENTORY_STRATEGY!r}, 'triage-0.1', "
                         "'bundle-0.1' or 'bundle-cite-0.1'",
                "service_version": SERVICE_VERSION}
    user_input = ("Review this change between BASE and HEAD. "
                  f"Repository: {ws.repo}. {inventory}")
    ceiling = max_usd_per_review
    if ceiling is None:
        try:
            ceiling = float(os.environ.get("CODATIEL_I2_SPEND_CEILING_USD", "5.0"))
        except ValueError:
            ceiling = 5.0
    preflight = providers_mod.estimate_cost_usd(
        len(instructions) + len(inventory), max_turns, rate or {})
    if preflight is not None and preflight > ceiling:
        return {"status": "budget_refused", "stage": "preflight",
                "error": f"preflight estimate ${preflight:.4f} exceeds ceiling "
                         f"${ceiling:.2f}; refusing paid inference",
                "service_version": SERVICE_VERSION}
    if (preflight is None and _model_is_paid_backend(model_name)
            and not _rate_is_quota(rate_status)):
        # No confirmed rates means no accountable ceiling: refuse paid
        # inference rather than spending blind. Quota-billed backends
        # (subscription, no per-token charge) are accountable through
        # recorded tokens, not USD: allowed with usd unconfirmed.
        return {"status": "budget_refused", "stage": "preflight",
                "error": "rates unconfirmed for a paid backend; refusing paid "
                         "inference without an accountable ceiling",
                "service_version": SERVICE_VERSION}
    ledger = SpendLedger(rate=rate or {}, rate_status=rate_status, ceiling_usd=ceiling,
                         zero_cost_authoritative=_model_is_zero_cost(model_name, rate_status))
    try:
        Path(runs_root).mkdir(parents=True, exist_ok=True)
        sink = tempfile.NamedTemporaryFile(mode="w", dir=str(runs_root),
                                           prefix="pending-spend-", suffix=".jsonl",
                                           delete=False)
        sink.close()
        ledger.attach_sink(Path(sink.name))
    except OSError as exc:
        return {"status": "failed", "stage": "spend_ledger",
                "error": f"cannot open durable spend sink: {type(exc).__name__}: {exc}",
                "service_version": SERVICE_VERSION}
    if ledger.sink_error:
        return {"status": "failed", "stage": "spend_ledger",
                "error": f"durable spend sink unavailable: {ledger.sink_error}",
                "service_version": SERVICE_VERSION}
    recorded_model = runtime_sdk.UsageRecordingModel(model, ledger)
    # CACHE-01: stable per-review OpenAI prompt-cache partition for Luna.
    # The SDK default mints a fresh key per Runner invocation, which
    # guarantees zero cross-turn cache reuse. The key is metadata only;
    # instructions, tools and evidence are byte-identical either way.
    # Other backends are untouched (Haiku uses explicit Anthropic
    # breakpoints; the custom adapters ignore extra_args).
    prompt_cache_key = None
    if str(model_name or "").startswith("luna:"):
        prompt_cache_key = runtime_sdk.prompt_cache_key_for(
            repo=ws.repo, base_sha=base_sha, head_sha=head_sha,
            instructions_id=instructions_id, model_name=model_name)
        model_settings = runtime_sdk.with_prompt_cache_key(
            model_settings, prompt_cache_key)
    try:
        result = runtime_sdk.run_review(
            instructions=instructions,
            user_input=user_input,
            tools=tools, model=recorded_model, max_turns=max_turns,
            model_settings=model_settings, usage_ledger=ledger)
        boundary_sha256 = getattr(
            result, "sdk_boundary_instructions_sha256", None)
    except Exception as exc:
        # Turn-budget exhaustion is a controlled incomplete state, not a
        # failure. Preserve incremental trace (tool calls, evidence, etc.)
        # instead of replacing it with an abbreviated record.
        is_budget_exhausted = type(exc).__name__ == "MaxTurnsExceeded"
        if is_budget_exhausted:
            if enable_final:
                try:
                    _pr = getattr(exc, "run_data", None)
                    avail_ev = []
                    try:
                        avail_ev = _harvest_tool_evidence(_pr) if _pr is not None else []
                    except Exception:
                        avail_ev = []
                    try:
                        if 'bundle_evidence' in locals() and bundle_evidence:
                            avail_ev = avail_ev + list(bundle_evidence)
                    except Exception:
                        pass
                    avail = []
                    seen=set()
                    for e in avail_ev:
                        eid=e.get("evidence_id")
                        if eid and eid not in seen:
                            seen.add(eid)
                            avail.append(eid)
                    final_user = final_mod.final_prompt(avail)
                    from codatiel_i2 import runtime_sdk as _rs2
                    final_res = _rs2.run_review(
                        instructions=instructions,
                        user_input=final_user,
                        tools=[], model=recorded_model, max_turns=1,
                        model_settings=model_settings, usage_ledger=ledger)
                    f_parsed, f_errors = _parse_model_output(final_res.final_output)
                    ok, _ = final_mod.validate_final(f_parsed, avail)
                    if ok:
                        f_status = f_parsed.get("status")
                        final_status = "inconclusive" if f_status == "inconclusive" else "complete"
                        class _Synth:
                            def __init__(self, orig, f_parsed):
                                self.orig = orig
                                self.f_parsed = f_parsed
                                self.new_items = getattr(orig, "new_items", []) if orig else []
                                self.final_output = __import__('json').dumps(f_parsed)
                        synth = _Synth(_pr, f_parsed)
                        return _write_failed_run(
                            runs_root, ws, changes, classification, model_name, max_turns,
                            rate or {}, rate_status, ceiling, ledger, stage="budget_exhausted_final",
                            error=f"turn budget exhausted after {max_turns} turns; final disposition: {f_status}",
                            status=final_status,
                            ctx=ctx, partial_result=synth, model_obj=recorded_model,
                            variant=variant, instructions=instructions,
                            instructions_id=instructions_id, inventory=inventory,
                            user_input=user_input, playbook_name=playbook_name,
                            playbook_text=playbook_text,
                            inventory_strategy_name=inventory_strategy_name,
                            pr_number=pr_number)
                except Exception:
                    pass
            partial_result = getattr(exc, "run_data", None)
            return _write_failed_run(
                runs_root, ws, changes, classification, model_name, max_turns,
                rate or {}, rate_status, ceiling, ledger, stage="budget_exhausted",
                error=f"turn budget exhausted after {max_turns} turns; "
                      "investigation incomplete, not a review",
                status="incomplete_budget_exhausted",
                ctx=ctx, partial_result=partial_result, model_obj=recorded_model,
                variant=variant, instructions=instructions,
                instructions_id=instructions_id, inventory=inventory,
                user_input=user_input, playbook_name=playbook_name,
                playbook_text=playbook_text,
                inventory_strategy_name=inventory_strategy_name,
                pr_number=pr_number)
        # R1.1 pricing halts are controlled incomplete states with the same
        # partial-trace preservation. PricingUnbounded: the ledger holds a
        # PRICING_PARTIAL request whose true cost is unbounded above, so no
        # further model call was sent. BudgetCeilingExceeded: the next
        # request's conservatively estimated cost exceeded the remaining
        # budget, so it was never sent. Never silence, never complete.
        # Neither triggers a Haiku follow-up: a follow-up run cannot cure a
        # pricing-blind ledger (and would halt the same way).
        is_pricing_unbounded = type(exc).__name__ == "PricingUnbounded"
        if is_pricing_unbounded:
            partial_result = getattr(exc, "run_data", None)
            return _write_failed_run(
                runs_root, ws, changes, classification, model_name, max_turns,
                rate or {}, rate_status, ceiling, ledger, stage="pricing_unbounded",
                error=f"{type(exc).__name__}: {exc}",
                status="incomplete_pricing_unbounded",
                ctx=ctx, partial_result=partial_result, model_obj=recorded_model,
                variant=variant, instructions=instructions,
                instructions_id=instructions_id, inventory=inventory,
                user_input=user_input, playbook_name=playbook_name,
                playbook_text=playbook_text,
                inventory_strategy_name=inventory_strategy_name,
                pr_number=pr_number)
        is_budget_ceiling = type(exc).__name__ == "BudgetCeilingExceeded"
        if is_budget_ceiling:
            partial_result = getattr(exc, "run_data", None)
            return _write_failed_run(
                runs_root, ws, changes, classification, model_name, max_turns,
                rate or {}, rate_status, ceiling, ledger, stage="budget_ceiling",
                error=f"{type(exc).__name__}: {exc}",
                status="incomplete_budget_ceiling",
                ctx=ctx, partial_result=partial_result, model_obj=recorded_model,
                variant=variant, instructions=instructions,
                instructions_id=instructions_id, inventory=inventory,
                user_input=user_input, playbook_name=playbook_name,
                playbook_text=playbook_text,
                inventory_strategy_name=inventory_strategy_name,
                pr_number=pr_number)
        # For other agent failures, try to preserve partial trace if available
        partial_result = getattr(exc, "run_data", None)
        return _write_failed_run(
            runs_root, ws, changes, classification, model_name, max_turns,
            rate or {}, rate_status, ceiling, ledger, stage="agent",
            error=f"{type(exc).__name__}: {exc}",
            ctx=ctx, partial_result=partial_result, model_obj=recorded_model,
            variant=variant, instructions=instructions,
            instructions_id=instructions_id, inventory=inventory,
            user_input=user_input, playbook_name=playbook_name,
            playbook_text=playbook_text,
            inventory_strategy_name=inventory_strategy_name,
            pr_number=pr_number)

    tool_evidence = _harvest_tool_evidence(result)
    # CONTEXT-01 arm C: the bundle's citable line evidence was returned
    # in this run (first message) and is run-scoped like any tool
    # evidence; merge it into the citation ledger so M7.1 validation
    # sees it. Foreign IDs still fail NOT_RETURNED; exact base/head
    # binding and byte replay are unchanged.
    if bundle_evidence:
        tool_evidence = tool_evidence + list(bundle_evidence)
    tool_outputs = _harvest_tool_outputs(result)
    spend = _spend_record(ledger, model_name)
    changed_paths = {c.get("path", "") for c in changes}
    parsed, parse_errors = _parse_model_output(result.final_output)
    model_findings, model_rows = _dispose_model_findings(
        parsed, ctx, tool_evidence, changed_paths)
    detector_rows, observations = _dispose_detector_findings(
        det_findings, det_evidence, det_verifications, det_facts, det_proofs,
        det_candidates, base_dir, head_dir, changed_paths, base_sha, head_sha)

    findings = detector_rows + model_rows
    evidence = det_evidence + tool_evidence
    verifications = det_verifications
    tool_calls = [{"tool": e["tool"], "args": e["args"],
                   "summary": e["summary"]} for e in ctx.tool_log]
    zero_tools = not tool_calls
    # Terminal-state propagation: empty/unparseable/invalid finals must not
    # silently become `complete` or `correct silence`. They are `unresolved`
    # with explicit parse_errors preserved.
    has_empty_final = any("empty final output" in e for e in parse_errors)
    has_unparseable = any("unparseable" in e for e in parse_errors)
    has_repaired = any("repaired_trailing_json" in e for e in parse_errors)
    is_unresolved_final = parsed.get("status") not in ("finding", "silence")
    if has_empty_final:
        # Empty final is always unresolved, regardless of tool count
        # (zero-tools case is already incomplete_no_tool_use, but empty with
        # tools should not be complete)
        if not zero_tools:
            status = "unresolved_empty_final"
        else:
            status = "incomplete_no_tool_use"
    elif is_unresolved_final:
        # Invalid status, array, or missing findings: always unresolved,
        # even if repaired (repaired invalid JSON is still invalid)
        status = "unresolved_unparseable"
    elif has_unparseable and not has_repaired:
        status = "unresolved_unparseable"
    elif zero_tools:
        status = "incomplete_no_tool_use"
    else:
        status = "complete"
    if enable_final:
        # Only finalize if the investigation did not already produce a clean disposition
        at_risk = status in ("incomplete_no_tool_use", "unresolved_empty_final", "unresolved_unparseable", "incomplete_budget_exhausted")
        if at_risk:
            try:
                avail_ev2 = tool_evidence
                avail2 = []
                seen2=set()
                for e in avail_ev2:
                    eid=e.get("evidence_id")
                    if eid and eid not in seen2:
                        seen2.add(eid)
                        avail2.append(eid)
                final_user2 = final_mod.final_prompt(avail2)
                from codatiel_i2 import runtime_sdk as _rs3
                final_res2 = _rs3.run_review(
                    instructions=instructions,
                    user_input=final_user2,
                    tools=[], model=recorded_model, max_turns=1,
                    model_settings=model_settings, usage_ledger=ledger)
                f2_parsed, f2_errors = _parse_model_output(final_res2.final_output)
                ok2, _ = final_mod.validate_final(f2_parsed, avail2)
                if ok2:
                    f2_status = f2_parsed.get("status")
                    if f2_status == "inconclusive":
                        status = "inconclusive"
                    elif f2_status in ("finding", "silence"):
                        status = "complete"
                    parsed, parse_errors = f2_parsed, f2_errors
                    model_findings, model_rows = _dispose_model_findings(
                        parsed, ctx, tool_evidence, changed_paths)
                    detector_rows, observations = _dispose_detector_findings(
                        det_findings, det_evidence, det_verifications, det_facts, det_proofs,
                        det_candidates, base_dir, head_dir, changed_paths, base_sha, head_sha)
                    findings = detector_rows + model_rows
                    evidence = det_evidence + tool_evidence
                    final_tag_success = "finalized_under_budget"
                else:
                    final_tag_success = None
            except Exception:
                final_tag_success = None
        else:
            final_tag_success = None
    else:
        final_tag_success = None
    provenance = _prompt_provenance(
        instructions=instructions, instructions_id=instructions_id,
        user_input=user_input, inventory=inventory,
        inventory_strategy=inventory_strategy_name,
        playbook_name=playbook_name, playbook_text=playbook_text,
        boundary_sha256=boundary_sha256)
    metrics = {"service_version": SERVICE_VERSION,
               "instructions_version": instructions_version,
               "instructions_id": instructions_id,
                 "model": model_name, "max_turns": max_turns,
                 "tool_calls": len(tool_calls),
                 "status": status,
                 "parse_errors": parse_errors,
                 "spend": spend,
                 "wall_seconds": round(time.time() - started, 2),
                "variant": variant}
    if bundle_meta is not None:
        metrics["bundle"] = bundle_meta
    _ft = None
    if 'final_tag' in locals() and locals().get('final_tag'):
        _ft = locals().get('final_tag')
    elif 'final_tag_success' in locals() and locals().get('final_tag_success'):
        _ft = locals().get('final_tag_success')
    if _ft:
        metrics["final_tag"] = _ft
        metrics["finalized_under_budget"] = (_ft == "finalized_under_budget")
    config = {"service": SERVICE_VERSION, "model": model_name,
              "max_turns": max_turns,
              "instructions": instructions_version,
              "instructions_id": instructions_id,
              "inventory_strategy": inventory_strategy_name,
              "variant": variant}
    scan_key = runs_mod.scan_key_for(ws.repo, base_sha, head_sha, config)
    run_id = runs_mod.new_run_id()
    run_dir = runs_mod.new_run_dir(Path(runs_root), scan_key, run_id)
    coverage = empty_coverage()
    coverage.update(det_coverage)
    candidates = {d: [] for d in runs_mod.CANDIDATE_FILES}
    candidates["docs_code"] = det_candidates
    # Provider termination reasons (end_turn, tool_use, max_tokens, etc.)
    # are recorded as local metadata from the adapter, not inferred from usage.
    termination_by_response = _collect_provider_termination(model)
    pr_url = (f"https://github.com/{ws.repo}/pull/{pr_number}"
              if pr_number else None)
    runs_mod.write_run(
        run_dir=run_dir, scan_key=scan_key, run_id=run_id, repo=ws.repo,
        base_sha=base_sha, head_sha=head_sha, config=config,
        pr_number=pr_number, pr_url=pr_url,
        workspace=ws.to_dict(), changes=changes,
        classification=classification, coverage=coverage,
        candidates=candidates, evidence=evidence,
        verifications=verifications, findings=findings, metrics=metrics,
        tool_calls=tool_calls,
        model_calls=[{"model": model_name, "max_turns": max_turns,
                      "prompt_cache_key": prompt_cache_key,
                      "scripted": bool(getattr(model, "is_scripted", False)),
                      "usage": spend.get("usage", {}),
                      "thinking_blocks_seen": int(getattr(
                          model, "thinking_blocks_seen", 0) or 0),
                      "provider_termination_by_response": termination_by_response,
                      "reasoning_summary": None,
                      "reasoning_summary_note": "no backend exposes provider "
                      "reasoning summaries; provider-private thinking is "
                      "counted, never claimed"}])
    _finalize_spend_ledger(ledger, run_dir)
    write_json(run_dir / "prompt_provenance.json", provenance)
    write_json(run_dir / "transcript.json",
               {"schema_version": "codatiel_i2.transcript.v1",
                "items": _serialize_items(result)})
    write_jsonl(run_dir / "observations.jsonl", observations)
    shadow = {"schema_version": "codatiel_i2.shadow_result.v1",
              "mode": "shadow", "post_to_github": False,
              "status": status,
              "advisory": [f for f in findings
                           if f.get("disposition") == "advisory_grounded"],
              "unresolved": [f for f in findings
                             if f.get("disposition") == "unresolved"],
              "observations": observations,
              "silence": ({"summary": parsed.get("summary", ""),
                           "tool_calls": len(tool_calls)}
                          if parsed.get("status") == "silence" else None),
              "investigation_notes": parse_errors,
              "counts": {d: sum(1 for f in findings
                                if f.get("disposition") == d)
                         for d in ("verified_proof", "advisory_grounded",
                                   "observed_not_material", "unresolved",
                                   "refuted_unsupported", "rejected_invalid")}}
    shadow["counts"]["observed_not_material"] = sum(
        1 for o in observations
        if o.get("disposition") == "observed_not_material")
    shadow["counts"]["unresolved"] += sum(
        1 for o in observations if o.get("disposition") == "unresolved")
    write_json(run_dir / "shadow_result.json", shadow)
    write_jsonl(run_dir / "model_findings_raw.jsonl", model_findings)
    write_jsonl(run_dir / "tool_outputs.jsonl", tool_outputs)
    if bundle_evidence:
        write_jsonl(run_dir / "bundle_evidence.jsonl", bundle_evidence)
    return {"status": status, "run_dir": str(run_dir), "run_id": run_id,
            "repo": ws.repo, "base_sha": base_sha, "head_sha": head_sha,
            "tool_calls": len(tool_calls), "findings": len(findings),
            "shadow": shadow["counts"],
            "service_version": SERVICE_VERSION}


def _render_inventory(changes: list[dict], classification: dict,
                      det_candidates: list[dict],
                      variant: str = "baseline") -> str:
    lines = [f"{len(changes)} changed files:"]
    for c in changes[:60]:
        lines.append(f"- {c.get('path','')} "
                     f"(+{c.get('additions',0)}/-{c.get('deletions',0)})")
    if len(changes) > 60:
        lines.append(f"- ... and {len(changes) - 60} more")
    lines.append(f"Deterministic docs_code candidates: {len(det_candidates)}.")
    for cand in det_candidates[:10]:
        anchor = cand.get("changed_anchor") or cand.get("anchor") or {}
        lines.append(f"- candidate {cand.get('candidate_id', '?')} "
                     f"at {anchor.get('path','?')}"
                     f":{anchor.get('line_start','?')}")
    if variant == "challenger":
        # Impact-path guidance: surface behavioral signals for hypothesis ranking
        lines.append("Focus on behavioral impact: changed return values, contracts,")
        lines.append("callers/callees, state transitions, boundary conditions, and")
        lines.append("integration consequences. Rank hypotheses by materiality;")
        lines.append("abandon weak when evidence contradicts. For obvious bugs,")
        lines.append("do not require cross-file investigation.")
        # Hint at large or high-churn files that often hide cross-file effects
        large = [c for c in changes if (c.get("additions", 0) + c.get("deletions", 0)) > 100]
        if large:
            lines.append(f"High-churn files ({len(large)} with >100 lines changed):")
            for c in large[:5]:
                lines.append(f"  - {c.get('path','')} (+{c.get('additions',0)}/-{c.get('deletions',0)})")
    lines.append("Fetch every span you rely on with the source tools.")
    return "\n".join(lines)


def _harvest_tool_evidence(result: Any) -> list[dict]:
    ledger: list[dict] = []
    for item in getattr(result, "new_items", []) or []:
        if getattr(item, "type", "") != "tool_call_output_item":
            continue
        try:
            payload = json.loads(str(item.output))
        except (ValueError, TypeError):
            continue
        ev = payload.get("evidence")
        if isinstance(ev, dict):
            ledger.append(ev)
        elif isinstance(ev, list):
            ledger.extend(e for e in ev if isinstance(e, dict))
    return ledger


def _extract_trailing_json(text: str) -> tuple[dict | None, str | None]:
    """Deterministic repair: extract a valid JSON object from trailing prose+JSON.

    Only for demonstrably equivalent representations where the provider returned
    a valid JSON object after prose (e.g., "I checked...\\n\\n{\"status\": ...}").
    Returns (parsed_dict, raw_json_text) or (None, None) if no valid trailing
    JSON object found. Never invents findings or evidence.

    Uses JSONDecoder to respect string contents (braces inside strings are
    not counted as structure), and only repairs the trailing JSON object,
    not an earlier one when the final is malformed.
    """
    decoder = json.JSONDecoder()
    # Find the last occurrence of a JSON object start that looks like a review
    # final (must contain "status" to avoid matching incidental braces in prose)
    last_start = text.rfind('{"status"')
    if last_start == -1:
        last_start = text.rfind('{"status" :')
        if last_start == -1:
            last_start = text.rfind("{")
            if last_start == -1:
                return None, None
    candidate = text[last_start:].strip()
    # Try to parse candidate directly (covers prose+JSON where JSON is at end)
    try:
        parsed, end = decoder.raw_decode(candidate)
        if isinstance(parsed, dict) and "status" in parsed:
            trailing = candidate[end:].strip()
            if not trailing:
                return parsed, candidate[:end].strip()
            # If there is trailing content after JSON, this is not a clean
            # trailing repair (e.g., earlier example + truncated final)
            return None, None
    except ValueError:
        pass
    # Fallback: scan for the last valid JSON object that is at the end (trailing)
    last_valid = None
    idx = 0
    while idx < len(text):
        try:
            parsed, end = decoder.raw_decode(text, idx)
            if isinstance(parsed, dict) and "status" in parsed:
                remaining = text[idx + end :].strip()
                if not remaining:
                    last_valid = (parsed, text[idx : idx + end].strip())
            idx += end
        except ValueError:
            idx += 1
            continue
        while idx < len(text) and text[idx].isspace():
            idx += 1
    if last_valid is not None:
        return last_valid
    return None, None


def _parse_model_output(final: Any) -> tuple[dict, list[str]]:
    errors: list[str] = []
    transformation: str | None = None
    if not isinstance(final, str) or not final.strip():
        return {"status": "incomplete", "findings": []}, ["empty final output"]
    text = final.strip()
    raw_text = text  # Preserve raw for transformation record
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
        text = text.strip()
        transformation = "fence_strip"
    try:
        parsed = json.loads(text)
        if transformation:
            errors.append(f"fence_strip: removed ``` fences (raw length {len(raw_text)}, stripped length {len(text)})")
    except ValueError as exc:
        # Deterministic repair: try trailing JSON extraction (prose+JSON)
        trailing_parsed, trailing_raw = _extract_trailing_json(text)
        if trailing_parsed is not None:
            parsed = trailing_parsed
            errors.append(f"repaired_trailing_json: extracted valid JSON object from prose+JSON (raw length {len(raw_text)}, json length {len(trailing_raw or '')})")
            if transformation:
                errors.append(f"fence_strip: removed ``` fences (raw length {len(raw_text)}, stripped length {len(text)})")
            # Do not treat as unparseable; the repair is explicit and preserved
        else:
            return {"status": "unresolved", "findings": []}, [f"unparseable: {exc}"]
    if not isinstance(parsed, dict):
        return {"status": "unresolved", "findings": []}, ["final output not an object"]
    if parsed.get("status") not in ("finding", "silence", "inconclusive"):
        errors.append(f"unknown status {parsed.get('status')!r} treated as unresolved")
        parsed["status"] = "unresolved"
    if not isinstance(parsed.get("findings", []), list):
        errors.append("findings not a list; dropped")
        parsed["findings"] = []
    return parsed, errors


def _model_is_paid_backend(model_name: str) -> bool:
    return model_name.split(":")[0] in ("luna", "haiku", "deepseek", "glm")


def _rate_is_quota(rate_status: str | None) -> bool:
    """True for subscription-quota billing (no per-token charge).

    Quota backends record confirmed tokens with USD unconfirmed; the
    subscription is sunk, the marginal review cost is quota draw, and
    per-request output bounds cap that draw.
    """
    return str(rate_status or "").startswith("quota_")


def _model_is_zero_cost(model_name: str, rate_status: str) -> bool:
    return model_name.startswith("scripted:") or rate_status == "local_zero_cost"


def _finalize_spend_ledger(ledger: SpendLedger, run_dir: Path) -> None:
    """Move the durable per-response sink into the run record.

    The sink already holds every response recorded before completion; moving
    (not rewriting) preserves the crash-durable bytes verbatim.
    """
    target = run_dir / "spend_ledger.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    if ledger.sink_path and Path(ledger.sink_path).exists():
        shutil.move(ledger.sink_path, target)
        ledger.sink_path = str(target)
    else:
        write_jsonl(target, ledger.to_record().get("request_ledger", []))


def _model_proof_manufactured(finding: dict) -> bool:
    proof = finding.get("proof")
    if proof == "verified":
        return True
    if isinstance(proof, dict):
        return proof.get("level") == "verified" or is_verified_proof_kind(proof.get("kind"))
    return False


def _summarize_validation(validation: Any) -> str:
    parts: list[str] = []
    for check in validation.citation_checks:
        parts.extend(message for _, message in check.failures)
    parts.extend(message for _, message in validation.anchor_failures)
    parts.extend(validation.blockers)
    return "; ".join(parts)


def _model_row(fid: str, finding: dict, disposition: str, reason: str,
               verification_status: str,
               validation: Any | None = None) -> dict:
    anchor = finding.get("changed_anchor")
    if not isinstance(anchor, dict):
        anchor = {"path": finding.get("path", ""),
                  "line_start": finding.get("line_start", 0),
                  "line_end": finding.get("line_end", 0)}
    row = {"finding_id": fid, "detector": finding.get("detector"),
           "subclass": "agent_advisory",
           "changed_anchor": anchor, "counterpart_anchors": [],
           "invariant": finding.get("claim", ""), "base_state": "",
           "head_state": finding.get("claim", ""), "trigger": finding.get("title", ""),
           "bad_outcome": finding.get("claim", ""),
           "evidence_ids": finding.get("evidence_ids") if isinstance(
               finding.get("evidence_ids"), list) else [],
           "proof": {"level": "candidate", "kind": "agent_advisory.v1"},
           "verification": {"status": verification_status},
           "confidence": 0.0, "publish_eligible": False,
           "public_comment_eligible": False,
           "raw": finding,
           "disposition": disposition, "reason": reason}
    if validation is not None:
        row["evidence_attestation"] = {
            "validator": evidence_validation_mod.VALIDATOR_VERSION,
            "state": validation.state,
            "result_code": validation.code,
            "citation_results": [{
                "index": check.index, "evidence_id": check.evidence_id,
                "status": check.status, "failures": [list(item) for item in check.failures]} for check in validation.citation_checks],
            "anchor_failures": [list(item) for item in validation.anchor_failures],
            "blockers": list(validation.blockers)}
    if disposition == "advisory_grounded":
        row["substantive_assessment"] = "UNASSESSED"
        row["assessment_note"] = ("Evidence authenticity only: the citations were "
                                  "returned in this run, are internally consistent, bind "
                                  "to the pinned revisions, and replay. Interpretation "
                                  "is UNASSESSED; this is not a confirmed defect.")
    return row


def _dispose_model_findings(parsed: dict, ctx: Any, tool_evidence: list[dict],
                            changed_paths: set[str]) -> tuple[list[dict], list[dict]]:
    raw = parsed.get("findings", [])
    rows: list[dict] = []
    evidence_by_id = {e.get("evidence_id"): e for e in tool_evidence
                      if isinstance(e, dict) and e.get("evidence_id")}
    for i, f in enumerate(raw):
        if not isinstance(f, dict):
            continue
        fid = f"agent-{i:03d}"
        if f.get("detector") not in DETECTORS:
            rows.append(_model_row(
                fid, f, "rejected_invalid",
                f"{evidence_validation_mod.DETECTOR_OUT_OF_SCOPE}: detector "
                f"{f.get('detector')!r} not in scope; findings must name one of the five classes",
                "uncertain"))
            continue
        proof_manufactured = _model_proof_manufactured(f)
        validation = evidence_validation_mod.validate_model_finding(
            f, evidence_by_id, base_sha=ctx.base_sha, head_sha=ctx.head_sha,
            base_checkout=Path(ctx.base_dir), head_checkout=Path(ctx.head_dir),
            changed_paths=changed_paths)
        summary = _summarize_validation(validation)
        if proof_manufactured:
            reason = (f"{evidence_validation_mod.MODEL_VERIFIED_PROOF_REJECTED}: model-claimed "
                      "verified proof rejected: synthetic raw-model JSON cannot manufacture proof")
            if validation.state != evidence_validation_mod.ATTESTED:
                reason += f"; evidence {validation.code}: {summary}" if summary else ""
            rows.append(_model_row(fid, f, "rejected_invalid", reason, "uncertain", validation))
        elif validation.state == evidence_validation_mod.ATTESTED:
            rows.append(_model_row(
                fid, f, "advisory_grounded",
                "tool evidence authenticated at the pinned revisions; human judgement required",
                "survives", validation))
        elif validation.state == evidence_validation_mod.BLOCKED:
            rows.append(_model_row(
                fid, f, "unresolved",
                f"{validation.code}: {summary}" if summary else str(validation.code),
                "uncertain", validation))
        else:
            rows.append(_model_row(
                fid, f, "rejected_invalid",
                f"{validation.code}: {summary}" if summary else str(validation.code),
                "uncertain", validation))
    return raw, rows


def _dispose_detector_findings(det_findings: list[dict],
                               det_evidence: list[dict],
                               det_verifications: list[dict],
                               det_facts: list[dict],
                               det_proofs: list[dict],
                               det_candidates: list[dict],
                               base_dir: Path, head_dir: Path,
                               changed_paths: set[str], base_sha: str,
                               head_sha: str) -> tuple[list[dict], list[dict]]:
    """Single route for deterministic output.

    Finding rows (schema-valid) carry one of verified_proof /
    advisory_grounded / unresolved / refuted_unsupported / rejected_invalid.
    Candidates that never became findings are NOT promoted to findings;
    they become observations: observed_not_material (evidence replays, no
    concern), unresolved (evidence problems), or refuted_unsupported.
    """
    rows: list[dict] = []
    evidence_by_id = {e.get("evidence_id"): e for e in det_evidence
                      if isinstance(e, dict) and e.get("evidence_id")}
    ver_by_cand: dict[str, dict] = {}
    for v in det_verifications:
        if isinstance(v, dict) and v.get("candidate_id"):
            ver_by_cand[v["candidate_id"]] = v
    fact_by_id = {f.get("fact_id"): f for f in det_facts
                  if isinstance(f, dict) and f.get("fact_id")}
    proof_by_candidate = {p.get("candidate_id"): p.get("record")
                          for p in det_proofs
                          if isinstance(p, dict) and p.get("candidate_id")}
    for f in det_findings:
        if not isinstance(f, dict):
            continue
        row = dict(f)
        # Product publish story (shadow-only until M7 qualification passes the
        # precision gate): detector-local publish_eligible is a claim about
        # the detector's own proof, not a product decision. The service
        # forces False on every row; publication requires a measured gate
        # that does not exist yet. verified_proof rows are mechanically
        # verified findings routed to the shadow channel, not releases.
        row["publish_eligible"] = False
        row["public_comment_eligible"] = False
        errors = trust_mod.validate_verified_finding(
            row,
            evidence_by_id=evidence_by_id,
            verification_by_candidate=ver_by_cand,
            proof_by_candidate=proof_by_candidate, fact_by_id=fact_by_id,
            base_checkout=base_dir, head_checkout=head_dir,
            changed_paths=changed_paths, expected_base_sha=base_sha,
            expected_head_sha=head_sha)
        proof = row.get("proof") or {}
        if not errors and proof.get("level") == "verified" \
                and is_verified_proof_kind(proof.get("kind")):
            row["disposition"] = "verified_proof"
            row["verification"] = {"status": "survives"}
            row["reason"] = "registered mechanical validator replayed the proof"
        elif not errors and (row.get("verification") or {}).get("status") == "survives":
            row["disposition"] = "advisory_grounded"
            row["substantive_assessment"] = "UNASSESSED"
            row["assessment_note"] = ("Evidence authenticity only: the deterministic detector "
                                      "evidence replayed at the pinned revisions. Interpretation "
                                      "is UNASSESSED and this is not a confirmed defect.")
            row["reason"] = ("deterministic detector evidence replayed; "
                             "interpretation is UNASSESSED and human judgement is required "
                             "(no registered proof)")
        elif (row.get("verification") or {}).get("status") == "refuted":
            row["disposition"] = "refuted_unsupported"
            row["reason"] = "detector verification refuted the candidate"
        else:
            row["disposition"] = "unresolved"
            row["reason"] = "; ".join(errors[:3]) if errors else \
                "verification uncertain"
        rows.append(row)
    observations = _observe_rejected_candidates(
        det_candidates, rows, det_evidence, ver_by_cand,
        base_dir, head_dir, changed_paths)
    return rows, observations


def _observe_rejected_candidates(det_candidates: list[dict],
                                 rows: list[dict],
                                 det_evidence: list[dict],
                                 ver_by_cand: dict[str, dict],
                                 base_dir: Path, head_dir: Path,
                                 changed_paths: set[str]) -> list[dict]:
    """Candidate-level observations for candidates with no finding row.

    A candidate is never promoted to a finding here. If its linked evidence
    replays cleanly the change is real but immaterial
    (observed_not_material); if evidence fails it is unresolved; if its
    verification refuted the candidate it is refuted_unsupported.
    """
    claimed = set()
    for f in rows:
        claimed.add(f.get("candidate_id") or "")
    linked_by_path: dict[str, list[dict]] = {}
    for e in det_evidence:
        if isinstance(e, dict) and e.get("path"):
            linked_by_path.setdefault(e["path"], []).append(e)
    observations: list[dict] = []
    for cand in det_candidates:
        if not isinstance(cand, dict):
            continue
        cid = cand.get("candidate_id", "")
        if not cid or cid in claimed:
            continue
        ver = ver_by_cand.get(cid, {})
        anchor = cand.get("changed_anchor") or {}
        cps = cand.get("counterpart_anchors") or []
        paths = {anchor.get("path", "")}
        paths.update(a.get("path", "") for a in cps if isinstance(a, dict))
        linked = [e for p in paths for e in linked_by_path.get(p, [])]
        replay_errs: list[str] = []
        for e in linked:
            replay_errs.extend(replay_source_evidence(
                e, base_checkout=base_dir, head_checkout=head_dir,
                changed_paths=changed_paths))
        if (ver.get("status") == "refuted"):
            disposition = "refuted_unsupported"
            reason = "candidate verification refuted it; not an allegation"
        elif replay_errs or not linked:
            disposition = "unresolved"
            reason = ("candidate evidence did not replay: "
                      + "; ".join(replay_errs[:2]) if replay_errs
                      else "candidate has no linked evidence")
        else:
            disposition = "observed_not_material"
            reason = ("deterministic evidence replays; no concern articulated "
                      f"(proof: {cand.get('reason') or cand.get('validator') or 'none'})")
        observations.append({"candidate_id": cid,
                             "detector": cand.get("detector", "docs_code"),
                             "changed_anchor": anchor,
                             "verification_status": ver.get("status", "unknown"),
                             "disposition": disposition, "reason": reason})
    return observations


def scripted_model_from_scenario(path: Path | str) -> Any:
    """Build a ScriptedModel from a mock scenario file (no inference).

    Scenario JSON: {"turns": [{"tool": name, "args": {...}}, ...],
    "final": {...object...} | "..."}. Tool turns are dispatched by the real
    Runner to the real I2 tools. For runtime smoke tests and M1/M2 gates only;
    outputs are non-evidence by construction (ScriptedModel.is_scripted).
    """
    from codatiel_i2 import runtime_sdk

    scenario = json.loads(Path(path).read_text(encoding="utf-8"))
    script: list[Any] = []
    for i, turn in enumerate(scenario.get("turns", [])):
        script.append(runtime_sdk.scripted_tool_call(
            call_id=f"scn_{i}", name=turn["tool"],
            arguments=turn.get("args", {}),
            response_id=f"scn_r{i}"))
    final = scenario.get("final", {"status": "silence", "summary": "",
                                   "findings": []})
    script.append(runtime_sdk.scripted_final(
        final if isinstance(final, str) else json.dumps(final),
        response_id="scn_final"))
    return runtime_sdk.ScriptedModel(script)


def _serialize_items(result: Any) -> list[dict]:
    items: list[dict] = []
    for item in getattr(result, "new_items", []) or []:
        try:
            raw = getattr(item, "raw_item", None)
            items.append({"type": getattr(item, "type", "?"),
                          "call_id": getattr(item, "call_id", None),
                          "output": str(getattr(item, "output", ""))[:8000],
                          "raw": str(raw)[:8000]})
        except Exception as exc:
            items.append({"type": "unserializable",
                          "error": f"{type(exc).__name__}"})
    return items


def _harvest_tool_outputs(result: Any) -> list[dict]:
    """Full parsed tool payloads for forensic logging (bounded per item).

    The transcript keeps truncated previews; this ledger is the complete
    local forensic record of what each tool returned.
    """
    out: list[dict] = []
    for item in getattr(result, "new_items", []) or []:
        if getattr(item, "type", "") != "tool_call_output_item":
            continue
        raw = str(item.output)
        truncated = len(raw) > 200_000
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = {"unparseable_output_head": raw[:2000]}
        out.append({"call_id": getattr(item, "call_id", None),
                    "truncated": truncated,
                    "payload": payload if not truncated else
                    {"truncated_head": str(payload)[:2000]}})
    return out


def _accounting_status(ledger: SpendLedger, model_name: str, *, completed: bool) -> str:
    """Classify one execution without inventing unknown costs.

    Zero spend is confirmed only for scripted/local-zero-cost executions (or
    when a run dies before any inference can start). Any failed paid-backend
    execution without authoritative provider usage is unknown, even when the
    ledger is empty: absence of a local record is not proof of absence of a
    billed provider call.
    """
    record = ledger.to_record()
    scripted_or_local = _model_is_zero_cost(model_name, ledger.rate_status)
    if not completed and not scripted_or_local:
        return UNKNOWN_USAGE
    if record["unknown_requests"] or ledger.recording_errors or ledger.sink_error:
        return UNKNOWN_USAGE if not scripted_or_local else CONFIRMED_ZERO_USAGE
    if scripted_or_local and (record["usd_estimate"] or 0.0) == 0.0:
        return CONFIRMED_ZERO_USAGE
    if not scripted_or_local and not ledger.has_confirmed_tokens():
        # A paid-backend execution with no confirmed usage tokens at all
        # cannot be confirmed: the provider may not have reported usage.
        return UNKNOWN_USAGE
    return CONFIRMED_USAGE


def _spend_record(ledger: SpendLedger, model_name: str, *, completed: bool = True) -> dict[str, Any]:
    """Per-response usage + spend. Unknown tokens stay unknown; USD is a lower
    bound whenever any request lacks confirmed usage."""
    record = ledger.to_record()
    record["accounting_status"] = _accounting_status(ledger, model_name, completed=completed)
    return record


def _write_failed_run(runs_root: Path, ws: Workspace, changes: list[dict],
                      classification: dict, model_name: str, max_turns: int,
                      rate: dict[str, Any], rate_status: str, ceiling: float,
                      ledger: SpendLedger | None, *, stage: str,
                      error: str,
                      status: str = "failed",
                      ctx: Any | None = None,
                      partial_result: Any | None = None,
                      model_obj: Any | None = None,
                      variant: str = "baseline",
                      instructions: str | None = None,
                      instructions_id: str | None = None,
                      inventory: str | None = None,
                      user_input: str | None = None,
                      playbook_name: str | None = None,
                      playbook_text: str | None = None,
                      inventory_strategy_name: str | None = None,
                      pr_number: int | None = None) -> dict[str, Any]:
    """Minimal immutable record for runs that die before disposition.

    When a partial trace is available (e.g., MaxTurnsExceeded with tool calls
    and evidence), finalize it instead of replacing it with an abbreviated
    record. Preserves tool calls, evidence, and investigation progress.
    """
    from codatiel_i2.common import time as time_mod

    active = ledger if ledger is not None else SpendLedger(
        rate=rate, rate_status=rate_status, ceiling_usd=ceiling)
    if instructions is None:
        instructions = (CHALLENGER_INSTRUCTIONS if variant == "challenger"
                        else CANONICAL_INSTRUCTIONS)
    if instructions_id is None:
        instructions_id = instructions_id_for(
            "challenger-impact-path-0.1" if variant == "challenger"
            else "canonical-minimal-0.1", instructions)
    instr_version = instructions_id
    if inventory is None:
        inventory = ""
    if user_input is None:
        user_input = ""
    provenance = _prompt_provenance(
        instructions=instructions, instructions_id=instructions_id,
        user_input=user_input, inventory=inventory,
        inventory_strategy=inventory_strategy_name or INVENTORY_STRATEGY,
        playbook_name=playbook_name, playbook_text=playbook_text,
        boundary_sha256=None)
    config = {"service": SERVICE_VERSION, "model": model_name,
              "max_turns": max_turns,
              "instructions": instr_version,
              "instructions_id": instructions_id,
              "inventory_strategy": inventory_strategy_name or INVENTORY_STRATEGY,
              "variant": variant}
    scan_key = runs_mod.scan_key_for(ws.repo, ws.base_sha, ws.head_sha, config)
    run_id = runs_mod.new_run_id()
    run_dir = runs_mod.new_run_dir(Path(runs_root), scan_key, run_id)

    # Try to harvest partial trace if available (incremental persistence)
    tool_evidence: list[dict] = []
    tool_outputs: list[dict] = []
    tool_calls: list[dict] = []
    transcript_items: list[dict] = []
    if ctx is not None:
        tool_calls = [{"tool": e["tool"], "args": e["args"],
                       "summary": e["summary"]} for e in getattr(ctx, "tool_log", [])]
        # Harvest from partial_result if available (contains new_items with tool outputs)
        if partial_result is not None:
            try:
                tool_evidence = _harvest_tool_evidence(partial_result)
                tool_outputs = _harvest_tool_outputs(partial_result)
                transcript_items = _serialize_items(partial_result)
            except Exception:
                pass
        # Fallback: try to harvest from ctx if partial_result not available
        if not tool_evidence and hasattr(ctx, "tool_log"):
            # Evidence is already in tool_log summaries, but we need full evidence
            # The ledger's evidence is in tool_outputs, which we already tried
            pass

    metrics = {"service_version": SERVICE_VERSION,
               "instructions_version": instr_version,
                "model": model_name, "status": status, "stage": stage,
                "error": error,
                  "spend": _spend_record(active, model_name, completed=False),
               "variant": variant}
    if stage == "budget_exhausted_final":
        metrics["final_tag"] = "finalized_under_budget"
        metrics["finalized_under_budget"] = True
    # Preserve investigation progress: tool calls and evidence count
    if tool_calls:
        metrics["tool_calls"] = len(tool_calls)
        metrics["evidence_count"] = len(tool_evidence)
    # Preserve provider termination reasons even on failed runs
    termination_by_response = _collect_provider_termination(model_obj)
    _finalize_spend_ledger(active, run_dir)

    pr_url = (f"https://github.com/{ws.repo}/pull/{pr_number}"
              if pr_number else None)
    write_json(run_dir / "run.json",
               {"schema_version": _RUN_SCHEMA,
                "run_id": run_id, "scan_key": scan_key, "config": config,
                "repo": ws.repo, "base_sha": ws.base_sha,
                "head_sha": ws.head_sha,
                "pr_number": pr_number, "pr_url": pr_url,
                "created_at": time_mod.utc_now_iso(),
                "workspace": ws.to_dict(), "status": status,
                "stage": stage, "error": error})
    write_json(run_dir / "prompt_provenance.json", provenance)
    write_json(run_dir / "metrics.json", metrics)
    # Write model_calls with provider termination even on failed runs
    # (for Haiku, this distinguishes end_turn vs tool_use vs max_tokens)
    model_calls = [{"model": model_name, "max_turns": max_turns,
                    "scripted": model_name.startswith("scripted:"),
                    "usage": _spend_record(active, model_name, completed=False).get("usage", {}),
                    "thinking_blocks_seen": int(getattr(model_obj, "thinking_blocks_seen", 0) or 0) if model_obj is not None else 0,
                    "provider_termination_by_response": termination_by_response,
                    "reasoning_summary": None,
                    "reasoning_summary_note": "no backend exposes provider reasoning summaries; provider-private thinking is counted, never claimed"}]
    # Also check wrapped model for thinking_blocks_seen
    if model_obj is not None and hasattr(model_obj, "_model"):
        wrapped_thinking = getattr(model_obj._model, "thinking_blocks_seen", None)
        if isinstance(wrapped_thinking, int) and wrapped_thinking:
            model_calls[0]["thinking_blocks_seen"] = wrapped_thinking
    write_jsonl(run_dir / "model_calls.jsonl", model_calls)
    # Finalize partial trace instead of replacing with abbreviated record
    # Always write trace files, even if empty (legitimate for immediate failure)
    write_jsonl(run_dir / "evidence.jsonl", tool_evidence)
    write_jsonl(run_dir / "tool_calls.jsonl", tool_calls)
    if transcript_items:
        write_json(run_dir / "transcript.json",
                   {"schema_version": _TRANSCRIPT_SCHEMA,
                    "items": transcript_items})
    else:
        write_json(run_dir / "transcript.json",
                   {"schema_version": _TRANSCRIPT_SCHEMA,
                    "items": []})
    if tool_outputs:
        write_jsonl(run_dir / "tool_outputs.jsonl", tool_outputs)
    else:
        write_jsonl(run_dir / "tool_outputs.jsonl", [])
    # Write empty findings/observations to indicate incomplete, not silence
    write_jsonl(run_dir / "findings.jsonl", [])
    write_jsonl(run_dir / "observations.jsonl", [])
    write_json(run_dir / "shadow_result.json",
                   {"schema_version": _SHADOW_SCHEMA,
                    "mode": "shadow", "post_to_github": False,
                    "status": status, "stage": stage, "error": error,
                    "advisory": [], "unresolved": [], "observations": [],
                    "silence": None,
                    "investigation_notes": [error],
                    "counts": {"verified_proof": 0, "advisory_grounded": 0,
                               "observed_not_material": 0, "unresolved": 0,
                               "refuted_unsupported": 0, "rejected_invalid": 0}})
    return {"status": status, "stage": stage, "error": error,
            "run_dir": str(run_dir), "run_id": run_id,
            "service_version": SERVICE_VERSION}


def deliver_shadow(run_dir: Path | str, *, to: list[str], sender: str,
                   transport: Any = None,
                   subject: str | None = None,
                   text_body: str | None = None) -> dict[str, Any]:
    """Deliver the shadow digest for a completed run. No PR comments ever:
    the only outbound channel is the digest transport (SMTP2GO in ops,
    fixture transport in tests/gates).

    `text_body`, when given, replaces the built-in terse digest with an
    exact pre-rendered body (v0.1.0 watcher emails). The given bytes are
    hashed and persisted pre-send exactly as with the default body."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    shadow = json.loads((run_dir / "shadow_result.json").read_text(
        encoding="utf-8"))
    lines = ["Codatiel I2 shadow digest",
             f"repo: {run.get('repo')}",
             f"base: {run.get('base_sha', '')[:12]} head: {run.get('head_sha', '')[:12]}",
             f"run: {run.get('run_id')} status: {shadow.get('status')}",
             f"model: {run.get('config', {}).get('model', '? ')}"
             f"{' (SCRIPTED mock run — non-evidence)' if 'scripted:' in str(run.get('config', {}).get('model', '')) else ''}",
              "channel: shadow-only; no PR comments; nothing published",
              "assessment: advisory findings are unverified review candidates; "
              "none is a confirmed defect",

             f"counts: {json.dumps(shadow.get('counts', {}))}"]
    for f in shadow.get("advisory", [])[:10]:
        if not isinstance(f, dict):
            continue
        anchor = f.get("changed_anchor", {}) if isinstance(f.get("changed_anchor"), dict) else {}
        lines.append(f"- unverified review candidate [{f.get('detector')}] "
                     f"{anchor.get('path')}:{anchor.get('line_start')} "
                     f"{f.get('finding_id')} "
                     f"(substantive assessment: {f.get('substantive_assessment', 'UNASSESSED')})")
    if shadow.get("silence"):
        lines.append(f"- silence: {shadow['silence'].get('summary', '')} "
                     f"({shadow['silence'].get('tool_calls', 0)} tool calls)")
    text = "\n".join(lines)
    subj = subject or (f"[codatiel-shadow] {run.get('repo')} "
                       f"{run.get('head_sha', '')[:8]} {shadow.get('status')}")
    if text_body is not None:
        text = text_body
    import hashlib as _hashlib

    body_sha256 = _hashlib.sha256(text.encode("utf-8")).hexdigest()
    # Durable pre-send record: the exact body and its hash are persisted
    # BEFORE transport, so an accepted-but-unpreserved digest cannot recur.
    record = {"to": to, "sender": sender, "subject": subj,
              "run_id": run.get("run_id"), "repo": run.get("repo"),
              "body_sha256": body_sha256, "body": text,
              "transport_receipt": None}
    write_json(run_dir / "delivery.json", record)
    send = transport or (lambda t, s, b: _smtp2go_send(t, sender, s, b))
    receipt = send(to, subj, text)
    record["transport_receipt"] = receipt
    write_json(run_dir / "delivery.json", record)
    return record



def _smtp2go_send(to: list[str], sender: str, subject: str,
                  text_body: str) -> dict:
    from codatiel_i2.delivery.smtp2go import send_email
    return send_email(to, sender, subject, text_body)
