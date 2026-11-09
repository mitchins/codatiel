"""REVIEW-DELEGATE-01: minimal bounded multi-agent review mode.

Roles: Terra leads (owns the consolidated review); Luna investigates in a
persistent PR-scoped session (independent sweep first, then directed
follow-ups); Haiku optionally does the same for security-sensitive work.

Mechanics: each subagent is an SDK ``Agent`` with its own model,
instructions, read-only I2 tools, independent ``ReviewContext`` (own tool
budget), own ``SpendLedger`` (own spend ceiling) and a file-backed
``SQLiteSession`` scoped to one (case, arm, rep, role). The lead reaches
the subagent through one async function tool per subagent that runs a
nested ``Runner.run`` against the SAME session object, so working context
persists across sweep and follow-ups without any orchestration rewrite.

Independence: the sweep prompt contains only the PR inventory, never the
lead's candidate findings (the lead has not run yet when the sweep runs).
Every delegated task explicitly permits reporting additional unrelated
PR-introduced defects. All subagent outputs are appended to an append-only
shared evidence ledger; the lead can challenge/deduplicate only inside its
own final output with reasons, never by editing the ledger.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Subagent turn/tool/time/spend bounds. Sweeps are cheaper than full
# reviews (fewer turns); follow-ups are small directed questions.
DEFAULT_SPEC = {
    "sweep_max_turns": 8,
    "sweep_tool_budget": 20,
    "followup_max_turns": 6,
    "followup_tool_budget": 15,
    "max_followups": 3,
    "sub_ceiling_usd": 0.25,
    "sub_max_tokens": 8000,
    "tool_timeout_s": 600.0,
    "followup_timeout_s": 300.0,
}

HAIKU_SPEC = {
    "sweep_max_turns": 8,
    "sweep_tool_budget": 20,
    "followup_max_turns": 6,
    "followup_tool_budget": 15,
    "max_followups": 2,
    "sub_ceiling_usd": 0.50,
    "tool_timeout_s": 600.0,
    "followup_timeout_s": 300.0,
}


@dataclass
class DelegationSpec:
    """One delegated arm's configuration (B: luna; C: luna+haiku)."""

    tag: str  # e.g. "B-FW3-r1": unique per (arm, case, rep)
    artifact_dir: str | Path  # runner-owned dir for session DB + ledger
    subs: tuple[str, ...] = ("luna",)
    per_role: dict[str, dict[str, Any]] = field(default_factory=dict)

    def role_spec(self, role: str) -> dict[str, Any]:
        base = dict(HAIKU_SPEC if role == "haiku" else DEFAULT_SPEC)
        base.update(self.per_role.get(role, {}))
        return base


SWEEP_INSTRUCTIONS = """You are an investigative code-review subagent working for a lead reviewer. \
You have NOT seen the lead reviewer's conclusions (none exist yet): perform an INDEPENDENT sweep of \
the PR described below.

Investigate contracts, code paths, configuration, edge cases and tests in the changed code. \
Use your tools to read exact lines at explicit revisions. Report every genuine defect YOU find, \
including defects unrelated to any particular hypothesis: contracts violated by the change, \
broken callers, wrong conditionals, resource/secret mishandling, test gaps that hide behavior \
changes, and docs_code drift. Every claim needs a file:line anchor and quoted evidence.

Respond with a compact structured report: one section per finding with (1) title, (2) severity \
guess (P0 = breaks/crashes/data-loss/security, P1 = wrong behavior, P2 = edge-case/missing \
handling, P3 = docs/minor), (3) file:line anchor, (4) quoted evidence, (5) why it is \
PR-introduced (base vs head). End with a one-line coverage note listing the areas you \
checked and any you could not reach within budget. If the change is clean, say so explicitly \
with the areas checked. Do not invent defects to fill space."""

FOLLOWUP_PREAMBLE = """Follow-up assignment from the lead reviewer. Answer the question below using \
your tools; your earlier investigation in this same session remains available and you SHOULD \
build on it. As before, you MUST also report any additional unrelated PR-introduced defects \
you notice while investigating, with file:line anchors and quoted evidence. Keep the same \
structured format."""

LEAD_ADDENDUM = """You have {n_sub} specialist subagent(s) available as tools: {tools}. \
Each subagent already performed an INDEPENDENT sweep of this PR (summaries in the shared \
evidence ledger appended below); its working session persists, so follow-up questions \
continue the same investigation.

Use delegation when a suspicious code region, contract, or security question deserves deeper \
checking than your own budget allows: call the tool with a hypothesis, a precise question, \
and scope paths. At most {max_fu} follow-up call(s) per subagent; do not call for \
reassurance on findings you already verified yourself. The subagent may return ADDITIONAL \
unrelated defects it noticed: you MUST include genuine ones in your final review with their \
anchors and evidence. You may challenge or deduplicate a subagent finding only with an \
evidence-backed reason stated in your final output; you cannot remove it from the ledger. \
Your own independent analysis remains required: verify the highest-risk paths yourself."""


class SubagentSession:
    """One PR-scoped persistent subagent: model + instructions + tools + session."""

    def __init__(self, *, role: str, backend: str, tag: str,
                 artifact_dir: Path, repo: str, base_sha: str, head_sha: str,
                 base_dir: Path, head_dir: Path, diff_repo: Path,
                 model: Any, model_settings: Any, rate: dict[str, Any],
                 rate_status: str, spec: dict[str, Any]) -> None:
        from codatiel_i2 import runtime_sdk

        agents = runtime_sdk.require_sdk()
        self.role = role
        self.backend = backend
        self.spec = spec
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.artifact_dir / f"session-{role}.db"
        # Session id binds one (tag, role): a separate PR/rep gets a
        # separate file, so context can never leak across PRs.
        self.session_id = f"d01-{tag}-{role}"
        # Fresh session per run: remove any stale DB from a previous
        # attempt with the same tag so a retry never inherits context.
        try:
            if self.session_path.exists():
                self.session_path.unlink()
        except OSError:
            pass
        self.session = agents.SQLiteSession(
            self.session_id, db_path=str(self.session_path))
        self.ctx = runtime_sdk.ReviewContext(
            repo=repo, base_sha=base_sha, head_sha=head_sha,
            base_dir=base_dir, head_dir=head_dir, diff_repo=diff_repo,
            tool_budget=int(spec["sweep_tool_budget"]))
        self.tools = runtime_sdk.build_i2_tools(self.ctx)
        self.instructions = (SWEEP_INSTRUCTIONS if role == "luna"
                             else SWEEP_INSTRUCTIONS)
        self.agent = agents.Agent(
            name=f"codatiel-i2-sub-{role}", instructions=self.instructions,
            tools=self.tools, model=model, model_settings=model_settings)
        self.model = model
        self.model_settings = model_settings
        self.rate = rate
        self.rate_status = rate_status
        self.followups_used = 0
        self.calls: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def _item_count(self) -> int:
        try:
            return len(await self.session.get_items())
        except Exception:
            return -1

    def _record_call(self, entry: dict[str, Any]) -> None:
        self.calls.append(entry)
        try:
            with open(self.artifact_dir / "ledger.jsonl", "a",
                       encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    async def sweep(self, user_input: str, ledger: Any) -> dict[str, Any]:
        """Independent blind sweep (first use of the session).

        Pure coroutine: awaited in the review's single event loop. Never
        run this in an executor + inner asyncio.run (that strands the
        SDK's shared HTTP pool on a dead loop).
        """
        from codatiel_i2 import runtime_sdk

        agents = runtime_sdk.require_sdk()
        before = await self._item_count()
        started = time.time()
        try:
            result = await asyncio.wait_for(
                agents.Runner.run(
                    self.agent, user_input, session=self.session,
                    max_turns=int(self.spec["sweep_max_turns"])),
                timeout=float(self.spec["tool_timeout_s"]))
            status, text = "ok", str(getattr(result, "final_output", "") or "")
        except Exception as exc:  # never fail the lead on a sub error
            status, text = f"error:{type(exc).__name__}", str(exc)[:2000]
        after = await self._item_count()
        entry = {"seq": len(self.calls), "author": self.role, "kind": "sweep",
                 "tag": self.session_id, "status": status,
                 "session_items_before": before, "session_items_after": after,
                 "resumption_na_first_call": True,
                 "wall_seconds": round(time.time() - started, 2),
                 "tool_calls": len(self.ctx.tool_log),
                 "result_chars": len(text)}
        self._record_call(entry)
        return {"status": status, "text": text, "entry": entry}

    async def ask(self, question: str, hypothesis: str, scope: str,
                  ledger: Any) -> dict[str, Any]:
        """Directed follow-up in the SAME session (context retained).

        Pure coroutine: awaited (via the lead's delegation tool) in the
        review's single event loop, so the subagent's HTTP pool stays
        valid. Never executor-wrapped.
        """
        from codatiel_i2 import runtime_sdk

        agents = runtime_sdk.require_sdk()
        async with self._lock:
            if self.followups_used >= int(self.spec["max_followups"]):
                return {"status": "cap_refused", "text": "",
                        "entry": {"author": self.role, "kind": "followup_refused",
                                  "reason": "max_followups reached"}}
            self.followups_used += 1
            # Follow-ups get a fresh (smaller) tool budget; the turn
            # history stays in the session.
            self.ctx.tool_budget = int(self.spec["followup_tool_budget"])
            prompt = (f"{FOLLOWUP_PREAMBLE}\n\nHypothesis: {hypothesis}\n"
                      f"Question: {question}\nScope hints: {scope}")
            before = await self._item_count()
            started = time.time()
            try:
                inner = await asyncio.wait_for(
                    agents.Runner.run(
                        self.agent, prompt, session=self.session,
                        max_turns=int(self.spec["followup_max_turns"])),
                    timeout=float(self.spec["followup_timeout_s"]))
                status = "ok"
                text = str(getattr(inner, "final_output", "") or "")
            except Exception as exc:
                status, text = f"error:{type(exc).__name__}", str(exc)[:2000]
            after = await self._item_count()
            entry = {"seq": len(self.calls), "author": self.role,
                     "kind": "followup", "tag": self.session_id,
                     "status": status, "question": question[:500],
                     "session_items_before": before,
                     "session_items_after": after,
                     "resumption_ok": (before >= 0 and after > before),
                     "wall_seconds": round(time.time() - started, 2),
                     "tool_calls": len(self.ctx.tool_log),
                     "result_chars": len(text)}
            self._record_call(entry)
            return {"status": status, "text": text, "entry": entry}


def prepare_delegation(*, ws: Any, diff_repo: Path, inventory: str,
                       spec: DelegationSpec, parent_ctx: Any | None = None,
                       models: dict[str, tuple[Any, Any]] | None = None
                       ) -> dict[str, Any]:
    """Synchronous half: create subagent sessions, ledgers and lead tools.

    Runs NO inference (safe in sync service code). The returned state's
    ``"sweeps"`` list is filled by ``arun_sweeps`` inside the review's
    single event loop; the lead tools are usable once sweeps complete.
    ``models`` optionally overrides ``{role: (model, settings)}`` for
    tests (scripted models, no inference); production passes None.
    """
    from codatiel_i2 import providers as providers_mod
    from codatiel_i2 import runtime_sdk
    from codatiel_i2.core.spend import SpendLedger

    agents = runtime_sdk.require_sdk()
    artifact_dir = Path(spec.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # Fresh attempt = fresh ledger: stale sweep/ledger/spend files from a
    # previous attempt with the same tag are removed (already checkpointed
    # in traces/ by the runner). The session DB is reset per session.
    for stale in (list(artifact_dir.glob("ledger.jsonl")) +
                  list(artifact_dir.glob("sweep-*.txt")) +
                  list(artifact_dir.glob("spend-*.jsonl"))):
        try:
            stale.unlink()
        except OSError:
            pass
    sessions: list[SubagentSession] = []
    ledgers: dict[str, Any] = {}
    tools: list[Any] = []
    tool_names: list[str] = []
    sweep_input = ("Review this change between BASE and HEAD. "
                   f"Repository: {ws.repo}. {inventory}")
    for role in spec.subs:
        resolved = providers_mod.resolve_backend(role)
        if models is not None and role in models:
            model, settings = models[role]
        else:
            model, settings = providers_mod.build_model(role)
            if role == "luna":
                from agents.model_settings import ModelSettings
                settings = ModelSettings(
                    store=False,
                    max_tokens=int(rsp.get("sub_max_tokens", 8000)))
        rsp = spec.role_spec(role)
        sub_ledger = SpendLedger(
            rate=resolved["rate_usd_per_mtok"],
            rate_status=resolved["rate_status"],
            ceiling_usd=float(rsp["sub_ceiling_usd"]))
        try:
            sub_ledger.attach_sink(artifact_dir / f"spend-{role}.jsonl")
        except OSError:
            pass
        recorded = runtime_sdk.UsageRecordingModel(model, sub_ledger)
        sess = SubagentSession(
            role=role, backend=role, tag=spec.tag,
            artifact_dir=artifact_dir, repo=ws.repo,
            base_sha=ws.base_sha, head_sha=ws.head_sha,
            base_dir=Path(ws.base_dir), head_dir=Path(ws.head_dir),
            diff_repo=diff_repo, model=recorded,
            model_settings=settings,
            rate=resolved["rate_usd_per_mtok"],
            rate_status=resolved["rate_status"], spec=rsp)
        sessions.append(sess)
        ledgers[role] = sub_ledger
        tools.append(_delegation_tool(agents, sess, sub_ledger, role,
                                      parent_ctx))
        tool_names.append(tools[-1].name if hasattr(tools[-1], "name")
                          else f"delegate_{role}")
    return {"tools": tools, "tool_names": tool_names,
            "sessions": sessions, "ledgers": ledgers,
            "sweeps": [], "sweep_input": sweep_input, "spec": spec,
            "repo": ws.repo, "base_sha": ws.base_sha,
            "head_sha": ws.head_sha}


async def arun_sweeps(state: dict[str, Any]) -> dict[str, Any]:
    """Async half: run each subagent's blind sweep (single event loop).

    Must run BEFORE the lead: sweeps stay blind to the lead's findings
    by construction. Fills ``state["sweeps"]``, writes per-role sweep
    texts to the artifact dir, and finalizes the lead addendum.
    """
    from codatiel_i2 import runtime_sdk  # noqa: F401 (keeps SDK bound)

    spec = state["spec"]
    sweep_input = state["sweep_input"]
    artifact_dir = Path(spec.artifact_dir)
    sweeps: list[dict[str, Any]] = []
    for sess in state["sessions"]:
        sweep = await sess.sweep(sweep_input, state["ledgers"][sess.role])
        sweeps.append({"role": sess.role, "status": sweep["status"],
                       "chars": len(sweep["text"]),
                       "tool_calls": sweep["entry"].get("tool_calls", 0)})
        try:
            with open(artifact_dir / f"sweep-{sess.role}.txt", "w",
                       encoding="utf-8") as fh:
                fh.write(sweep["text"])
        except OSError:
            pass
    state["sweeps"] = sweeps
    # The lead sees concise sweep digests, not raw dumps: full texts stay
    # in the artifact dir for attribution analysis.
    digest_lines = [
        f"[{s['role']} sweep: {s['status']}, "
        f"{s['tool_calls']} tool calls; full text in "
        f"{spec.tag}/sweep-{s['role']}.txt" for s in sweeps]
    max_fu = max(int(spec.role_spec(r)["max_followups"])
                 for r in spec.subs)
    state["addendum"] = (
        LEAD_ADDENDUM.format(
            n_sub=len(state["sessions"]),
            tools=", ".join(state["tool_names"]), max_fu=max_fu) +
        "\nSubagent sweep digests:\n" + "\n".join(digest_lines))
    return state


async def arun_delegated(*, instructions: str, user_input: str,
                         tools: list[Any], model: Any, max_turns: int,
                         model_settings: Any, usage_ledger: Any,
                         model_name: str, state: dict[str, Any],
                         instructions_label: str) -> tuple[Any, dict[str, Any]]:
    """Run sweeps + lead review in ONE event loop (single HTTP pool).

    Finalizes the lead instructions (base + addendum with sweep digests),
    applies the Luna prompt-cache partition when relevant, and awaits the
    parent review. Follow-up delegation tools await nested subagent runs
    in this same loop. Returns (result, state).
    """
    from codatiel_i2 import runtime_sdk
    from codatiel_i2 import service as _service

    await arun_sweeps(state)
    final_instructions = (instructions + "\n\n" +
                          state.get("addendum", ""))
    final_id = _service.instructions_id_for(
        f"{instructions_label}+delegate-{'-'.join(state['spec'].subs)}",
        final_instructions)
    settings_eff = model_settings
    cache_key = None
    if str(model_name or "").startswith("luna:"):
        cache_key = runtime_sdk.prompt_cache_key_for(
            repo=state.get("repo", ""), base_sha=state.get("base_sha", ""),
            head_sha=state.get("head_sha", ""),
            instructions_id=final_id, model_name=model_name)
        settings_eff = runtime_sdk.with_prompt_cache_key(
            model_settings, cache_key)
    result = await runtime_sdk.arun_review(
        instructions=final_instructions, user_input=user_input,
        tools=tools, model=model, max_turns=max_turns,
        model_settings=settings_eff, usage_ledger=usage_ledger)
    state["instructions_final"] = final_instructions
    state["instructions_id_final"] = final_id
    state["prompt_cache_key"] = cache_key
    return result, state


def _delegation_tool(agents: Any, sess: SubagentSession, ledger: Any,
                     role: str, parent_ctx: Any | None) -> Any:
    """One async function tool closing over a persistent subagent session."""
    tool_name = (f"delegate_investigation" if role == "luna"
                 else f"delegate_security_investigation")
    description = (
        f"Ask the persistent {role} investigative subagent to dig into a "
        f"suspicious region (hypothesis + precise question + scope paths). "
        f"The same session continues across calls. The subagent MUST also "
        f"report unrelated PR-introduced defects it notices. "
        f"At most {sess.spec['max_followups']} follow-ups per review.")

    async def _impl(question: str, hypothesis: str,
                    scope_paths: str = "") -> str:
        out = await sess.ask(question, hypothesis, scope_paths, ledger)
        if parent_ctx is not None:
            # Parent-side accounting: the delegation call consumed a
            # parent turn. Logged post-hoc (own follow-up caps are
            # enforced in ask()); never gated by the i2 tool budget.
            try:
                parent_ctx.tool_log.append(
                    {"tool": tool_name,
                     "args": {"question": question[:200],
                              "hypothesis": hypothesis[:200],
                              "scope_paths": scope_paths[:200]},
                     "summary": {"delegated": role,
                                 "status": out["status"]}})
            except Exception:
                pass
        return json.dumps({"subagent": role, "status": out["status"],
                           "resumption": out.get("entry", {}),
                           "report": out["text"][:6000]})

    _impl.__name__ = tool_name
    return agents.function_tool(
        _impl, name_override=tool_name,
        description_override=description,
        timeout=float(sess.spec["followup_timeout_s"]))


def delegation_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Small JSON-serializable summary for metrics.json."""
    from codatiel_i2 import service as _service  # local import: no cycle at runtime

    out: dict[str, Any] = {"subs": {}}
    for sess in state.get("sessions", []):
        ledger = state["ledgers"][sess.role]
        rec = _service._spend_record(ledger, f"{sess.role}:delegated",
                                     completed=True)
        out["subs"][sess.role] = {
            "session_id": sess.session_id,
            "followups_used": sess.followups_used,
            "sub_tool_calls": len(sess.ctx.tool_log),
            "spend": rec,
            "calls": [{"kind": c.get("kind"), "status": c.get("status"),
                       "items_before": c.get("session_items_before"),
                       "items_after": c.get("session_items_after"),
                       "resumption_ok": c.get("resumption_ok"),
                       "wall_seconds": c.get("wall_seconds")}
                      for c in sess.calls],
        }
    out["sweeps"] = state.get("sweeps", [])
    return out
