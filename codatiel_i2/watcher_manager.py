"""Codatiel v0.1.1 production manager: eligibility -> queue -> dispatch.

One poll cycle:
  discovery (validated) -> upsert pairs -> eligibility (activation gate)
  -> queue admission (caps) -> dispatch (revalidate -> reserve -> run ->
  account -> revalidate -> deliver) -> save (every transition persists).

No inference and no email happens without a fresh revalidation at that
exact point. Failures fail closed; unknown spend blocks via reservation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codatiel_i2 import watcher_eligibility as E
from codatiel_i2 import watcher_store as S

HAIKU_TRIGGER = ("incomplete_budget_exhausted",
                 "unresolved_empty_final",
                 "unresolved_unparseable")
PER_REVIEW_RESERVE_USD = 1.0
REVIEW_TIMEOUT_S = 1200
_FETCH_PREFIX = "fetch: "
_NOT_FOUND = "not found"
_METRICS_JSON = "metrics.json"


@dataclass
class ManagerConfig:
    repos: list[str] | None = None
    exclude: list[str] = field(default_factory=list)
    per_poll_cap: int = 3
    max_queued: int = 100
    spend_total_cap_usd: float = 5.0
    spend_daily_cap_usd: float = 2.0
    spend_seed_usd: float = 0.4226
    recipients: list[str] = field(default_factory=list)
    sender: str = ""
    runs_root: Path = Path("runs")
    work_root: Path = Path(".codatiel-i2-work")


EMAIL_FINDING_CAP = 10
EMAIL_BODY_BUDGET = 65536
EMAIL_EXCERPTS_PER_FINDING = 5
# Free-text fields (silence rationales, failure diagnostics) are capped
# individually so a zero-finding or failed review can never exceed the
# body budget on its own.
EMAIL_FREE_TEXT_CAP = 4096


def _cap_free_text(value: object, *, source: str) -> str:
    """Bound one unbounded text field, loudly. Truncation is explicit
    (kept/total lengths + artifact pointer), never silent."""
    text = "" if value is None else str(value)
    raw = text.encode("utf-8")
    if len(raw) <= EMAIL_FREE_TEXT_CAP:
        return text
    kept = raw[:EMAIL_FREE_TEXT_CAP].decode("utf-8", "ignore")
    return (kept + f"... [truncated to {EMAIL_FREE_TEXT_CAP} of {len(raw)} "
            f"bytes; full text in run artifacts ({source})]")


def _finding_issue_key(finding: dict) -> object:
    """Underlying issue identity for delivery-time dedup. Detector rows
    carry the scan-computed issue_key; legacy rows fall back to their
    finding ID (no collapsing, never a wrong merge)."""
    key = finding.get("issue_key")
    if key:
        return ("issue", key)
    return ("finding", finding.get("finding_id"))


def build_email(identity: dict, run_dir: Path, *, stale: bool = False,
                second_pass: bool = False) -> tuple[str, str]:
    """Strict digest builder. Invalid identity blocks delivery (raises).

    Identity comes from the dispatch context (validated connector data),
    never from model output. Every claim in the body is labelled.

    Output safety bound (not a quality substitute): at most
    EMAIL_FINDING_CAP distinct findings and EMAIL_BODY_BUDGET bytes.
    Duplicates collapse by issue identity; anything omitted is counted
    explicitly with its IDs, never silently truncated. Full detail stays
    in the run artifacts (findings.jsonl).
    """
    repo = str(identity.get("repo") or "")
    number = identity.get("number")
    url = str(identity.get("url") or "")
    base = str(identity.get("base") or "")
    head = str(identity.get("head") or "")
    if not repo or not isinstance(number, int) or not number:
        raise ValueError("invalid PR identity: missing repo/number")
    if not (url.startswith("https://github.com/")
            and url.rstrip("/").endswith(f"/pull/{number}")):
        raise ValueError(f"invalid PR url {url!r}: delivery blocked")
    if not (E.FULL_SHA.match(base) and E.FULL_SHA.match(head)):
        raise ValueError("invalid base/head SHA: delivery blocked")
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    metrics = json.loads((run_dir / _METRICS_JSON).read_text(encoding="utf-8"))
    try:
        shadow = json.loads((run_dir / "shadow_result.json").read_text(
            encoding="utf-8"))
    except Exception:
        shadow = {}
    findings: list[dict] = []
    try:
        for line in (run_dir / "findings.jsonl").read_text(
                encoding="utf-8").splitlines():
            if line.strip():
                findings.append(json.loads(line))
    except Exception:
        findings = []
    ev: dict[str, dict] = {}
    try:
        for line in (run_dir / "evidence.jsonl").read_text(
                encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                ev[e.get("evidence_id")] = e
    except Exception:
        pass
    status = metrics.get("status", "?")
    model = metrics.get("model", "?")
    spend = metrics.get("spend", {})
    usd = spend.get("usd_estimate")
    cost = f"${usd:.4f}" if usd is not None else "unconfirmed (quota/unknown)"
    subj = (f"[codatiel-shadow] {repo}#{number} "
            f"{base[:8]}..{head[:8]} {status} {run.get('run_id')}")
    if second_pass:
        subj += " (conditional Haiku follow-up)"
    lines = [
        f"repo: {repo}  PR: #{number}",
        f"link: {url}",
        f"base: {base}  head: {head}",
        f"status: {status}{'  STALE-DURING-RUN' if stale else ''}",
        f"model: {model}  instructions: {metrics.get('instructions_id', '?')}",
        f"tool calls: {metrics.get('tool_calls')}  wall: "
        f"{metrics.get('wall_seconds', '?')}s  cost: {cost} "
        f"({spend.get('accounting_status', '?')})",
        f"run: {run.get('run_id')}",
        "",
    ]
    if status == "failed":
        lines += ["REVIEW FAILED before/at inference: no verdict rendered. "
                  "Absence of findings is meaningless; the failure reason "
                  "below is the only signal.",
                  "failure: " + _cap_free_text(
                      shadow.get("error") or shadow.get("stage") or "?",
                      source="shadow_result.json"),
                  ""]
    if second_pass:
        lines += ["Conditional Haiku follow-up to an incomplete/unresolved "
                  "Luna review, labelled independently.", ""]
    verified = [f for f in findings
                if f.get("disposition") == "verified_proof"]
    verified.sort(key=lambda f: -(f.get("confidence") or 0))
    # Delivery-time dedup by underlying issue identity (defense in depth:
    # the detector already dedupes pre-verification; the email never
    # repeats an issue no matter which path produced the rows).
    seen_keys: set[object] = set()
    distinct: list[dict] = []
    dupes = 0
    for f in verified:
        key = _finding_issue_key(f)
        if key in seen_keys:
            dupes += 1
            continue
        seen_keys.add(key)
        distinct.append(f)
    if status == "complete" and not distinct:
        lines.append("NO MATERIAL FINDINGS after a completed investigation.")
        sil = (shadow.get("silence") or {}).get("summary") or \
            "(no silence rationale recorded)"
        lines.append("Silence rationale (model-authored, unverified): "
                     + _cap_free_text(sil, source="shadow_result.json"))
    shown = distinct[:EMAIL_FINDING_CAP]
    omitted = distinct[EMAIL_FINDING_CAP:]
    for i, f in enumerate(shown, 1):
        anchor = f.get("changed_anchor") or {}
        lines.append(f"--- finding {i} (model-authored, UNVERIFIED) ---")
        lines.append(f"outcome: {f.get('bad_outcome', '(untitled)')}")
        lines.append(f"location: {anchor.get('path')} lines "
                     f"{anchor.get('line_start')}-{anchor.get('line_end')}")
        lines.append(f"trigger: {f.get('trigger', '?')}")
        lines.append(f"head behaviour: {f.get('head_state', '?')}")
        lines.append(f"base behaviour: {f.get('base_state', '?')}")
        lines.append(f"detector: {f.get('detector')}  model confidence: "
                     f"{f.get('confidence')}  finding: {f.get('finding_id')}")
        eids = f.get("evidence_ids") or []
        for eid in eids[:EMAIL_EXCERPTS_PER_FINDING]:
            e = ev.get(eid, {})
            excerpt = (e.get("excerpt") or "")[:400].replace("\n", " ")
            lines.append(f"evidence {eid} {e.get('path', '?')}@"
                         f"{str(e.get('head_sha') or e.get('base_sha') or '')[:12]}: "
                         f"{excerpt}")
        if len(eids) > EMAIL_EXCERPTS_PER_FINDING:
            lines.append(f"(+{len(eids) - EMAIL_EXCERPTS_PER_FINDING} further "
                         "evidence item(s) in run artifacts)")
    if status != "complete" and status != "failed" and not distinct:
        lines.append("INCOMPLETE INVESTIGATION: absence of findings is not "
                     "evidence of a clean change.")
    lines += ["",
              "Uncertain areas: anything above marked model-authored is a "
              "review candidate, not a confirmed defect.",
              f"Verified rows: {len(verified)} total, {len(distinct)} distinct "
              f"issue(s), {len(shown)} shown; {dupes} duplicate(s) collapsed "
              "by issue identity.",
              "Dismissed candidates: detector-verified rows surfaced above; "
              "dismissals are detector judgements recorded in run artifacts, "
              "not proof of cleanliness."]
    if omitted:
        omitted_ids = [str(f.get("finding_id", "?")) for f in omitted[:20]]
        lines.append(f"OMITTED {len(omitted)} additional distinct finding(s) "
                     f"beyond the {EMAIL_FINDING_CAP}-finding email cap; full "
                     "detail preserved in run artifacts (findings.jsonl). "
                     f"First omitted IDs: {', '.join(omitted_ids)}")
    lines.append("Private shadow artifact. Never post to GitHub.")
    text = "\n".join(lines)
    # Whole-finding granularity: drop trailing shown findings until the
    # body fits the budget, keeping header, counts and overflow notes.
    # The overflow line below always reports the true omitted count, so a
    # budget cut is never silent.
    while (len(text.encode("utf-8")) > EMAIL_BODY_BUDGET and shown):
        dropped = shown.pop()
        omitted = [dropped] + omitted
        marker = "--- finding "
        cut = text.rfind(marker)
        text = text[:cut] if cut != -1 else text
        omitted_ids = [str(f.get("finding_id", "?")) for f in omitted[:20]]
        overflow = (f"OMITTED {len(omitted)} finding(s): email body budget "
                    f"({EMAIL_BODY_BUDGET} bytes) reached; full detail "
                    "preserved in run artifacts (findings.jsonl). "
                    f"First omitted IDs: {', '.join(omitted_ids)}\n"
                    "Private shadow artifact. Never post to GitHub.")
        text = text.rstrip("\n") + "\n" + overflow
    if len(text.encode("utf-8")) > EMAIL_BODY_BUDGET:
        # Unconditional guarantee: reachable only when the fixed
        # header/footer themselves exceed the budget (no droppable
        # findings left). Preserve identity (repo/link/SHAs), status and
        # the omission notice; cut everything between.
        head = "\n".join(text.split("\n")[:7])
        notice = (f"BODY TRUNCATED to {EMAIL_BODY_BUDGET} bytes: fixed header "
                  "preserved; omitted middle content (findings, rationale, "
                  "diagnostics) remains in run artifacts (findings.jsonl, "
                  "shadow_result.json, metrics.json).")
        text = (head + "\n" + notice
                + "\nPrivate shadow artifact. Never post to GitHub.")
        if len(text.encode("utf-8")) > EMAIL_BODY_BUDGET:
            text = (text.encode("utf-8")[:EMAIL_BODY_BUDGET]
                    .decode("utf-8", "ignore"))
    return subj, text


class Manager:
    def __init__(self, cfg: ManagerConfig, store: S.Store, *,
                 now: Callable[[], float] = time.time,
                 day: Callable[[], str] | None = None,
                 discover: Callable = E.discover_open_prs,
                 fetch: Callable = E.fetch_pr,
                 list_repos: Callable[[], tuple[list[str] | None, str]] | None = None,
                 sender: Callable | None = None,
                 instance_id: str | None = None):
        self.cfg = cfg
        self.store = store
        self.now = now
        self.day = day or (lambda: time.strftime("%Y-%m-%d", time.gmtime()))
        self.discover = discover
        self.fetch = fetch
        self.list_repos = list_repos
        self.sender = sender
        self.instance_id = instance_id or f"mgr-{os.getpid()}-{uuid4()}"
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    # -- auth/lease -----------------------------------------------------
    def check_in(self) -> None:
        """Per-cycle lease heartbeat; second live instance is refused."""
        self.store.acquire_lease(self.instance_id)
        self.store.heartbeat_lease(self.instance_id)

    # -- discovery + admission ------------------------------------------
    def repo_list(self) -> list[str]:
        if self.list_repos is not None:
            repos, err = self.list_repos()
            if repos is None:
                raise S.StoreError(f"repo listing failed: {err}")
            repos = [r for r in repos if r not in set(self.cfg.exclude)]
        elif self.cfg.repos is not None:
            repos = [r for r in self.cfg.repos
                     if r not in set(self.cfg.exclude)]
        else:
            raise S.StoreError("auto-discovery needs list_repos wiring")
        paused = self.store.paused_repos()
        return [r for r in repos if r not in paused]

    def cycle(self) -> dict:
        """One full poll: discover, admit, dispatch up to cap, report."""
        activated = self.store.require_activated()
        self.check_in()
        today = self.day()
        out: dict[str, Any] = {"discovered": 0, "admitted": 0,
                               "dispatched": [], "emails": [],
                               "paused": False, "errors": []}
        if self.availability(today) <= 0:
            out["paused"] = True
            self.send_paused_notice(today)
            return out
        repos = self.repo_list()
        pairs: list[tuple[str, E.ValidatedPR]] = []
        seen: set[tuple[str, int, str, str]] = set()
        for repo in repos:
            if self._stop:
                break
            try:
                rows, err = self.discover(repo, 100)
            except Exception as exc:
                out["errors"].append({"repo": repo,
                                      "error": f"{type(exc).__name__}: {exc}"})
                continue
            if rows is None:
                out["errors"].append({"repo": repo, "error": err})
                continue
            if err:
                out["errors"].append({"repo": repo, "warning": err})
            for raw in rows:
                vpr, reason = E.validate_pr_metadata(repo, raw)
                if vpr is None:
                    out["errors"].append({"repo": repo, "pr": raw.get("number"),
                                          "rejected": reason})
                    # Lifecycle rejection retires queued rows for that PR:
                    # a closed/merged pair must never dispatch later.
                    if "state" in reason or "merged" in reason:
                        try:
                            number = int(raw.get("number") or 0)
                        except (TypeError, ValueError):
                            number = 0
                        if number:
                                    self.store.cx.execute(
                                        "UPDATE pairs SET status='suppressed',"
                                        " note=?, updated_ts=? WHERE repo=? "
                                        "AND pr=? AND status='queued'",
                                        (f"lifecycle at discovery: {reason}",
                                         time.time(), repo, number))
                                    self.store.cx.commit()
                    continue
                out["discovered"] += 1
                self.store.upsert_pair(
                    vpr.repo, vpr.number, vpr.base_sha, vpr.head_sha,
                    pr_url=vpr.url, pr_created_at=vpr.created_at)
                ok, why = E.check_eligible(vpr, activated)
                if not ok:
                    if why == "draft":
                        # Drafts are not terminal: keep eligible so a
                        # mark-ready flip is picked up; dispatch
                        # revalidation holds them until ready.
                        self.store.set_status(
                            vpr.repo, vpr.number, vpr.base_sha,
                            vpr.head_sha, "eligible",
                            note="draft: held until marked ready")
                        continue
                    self.store.set_status(vpr.repo, vpr.number,
                                          vpr.base_sha, vpr.head_sha,
                                          "suppressed", note=why)
                    continue
                ident = (vpr.repo, vpr.number, vpr.base_sha,
                         vpr.head_sha)
                if ident in seen:
                    continue  # same pair listed twice in one inventory
                seen.add(ident)
                cur = self.store.get_pair(vpr.repo, vpr.number,
                                          vpr.base_sha, vpr.head_sha)
                if cur and cur["status"] in ("queued", "running",
                                             "complete", "delivered",
                                             "failed", "suppressed"):
                    continue  # already handled; no duplicates
                self.store.set_status(vpr.repo, vpr.number,
                                      vpr.base_sha, vpr.head_sha, "eligible")
                out["admitted"] += 1
                pairs.append((repo, vpr))
        admitted = 0
        # Drain previously queued pairs first (oldest), revalidating at
        # dispatch; then admit newly eligible pairs into remaining cap.
        # Total queued work stays bounded: never queue beyond max_queued.
        backlog = self.store.queued_pairs(self.cfg.per_poll_cap)
        todo: list[tuple[str, int, str, str]] = [
            (b["repo"], b["pr"], b["base"], b["head"]) for b in backlog]
        for repo, vpr in pairs:
            if len(todo) >= self.cfg.max_queued:
                out["errors"].append(
                    {"queue": f"bound reached ({self.cfg.max_queued}); "
                              "remainder stays eligible"})
                break
            self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                                  vpr.head_sha, "queued")
            todo.append((vpr.repo, vpr.number, vpr.base_sha, vpr.head_sha))
        for repo, number, base, head in todo:
            if self._stop or admitted >= self.cfg.per_poll_cap:
                break
            if self.availability(today) < PER_REVIEW_RESERVE_USD:
                out["paused"] = True
                self.send_paused_notice(today)
                break
            rec = self.dispatch(repo, number, base, head, today)
            out["dispatched"].append(rec)
            admitted += 1
            try:
                self.check_in()
            except Exception:
                out["errors"].append({"lease": "heartbeat lost"})
                return out
        # Redelivery sweep: complete-but-undelivered pairs retry the same
        # bytes (never re-inference). Ambiguous receipts stay parked for
        # explicit replay-delivery only.
        for row in self.store.cx.execute(
                "SELECT repo, pr, base, head, run_dir FROM pairs "
                "WHERE status='complete' AND spend_unknown=0").fetchall():
            if self._stop or admitted >= self.cfg.per_poll_cap + 3:
                break
            repo2, number2, base2, head2, rd = row
            dl = self.store.cx.execute(
                "SELECT status FROM deliveries WHERE run_dir=? "
                "ORDER BY updated_ts DESC LIMIT 1", (rd,)).fetchone()
            if dl and dl[0] in ("delivered", "ambiguous"):
                continue
            live, reason = self.revalidate(repo2, number2, base2, head2)
            if live is None:
                if not reason.startswith(_FETCH_PREFIX):
                    self.store.set_status(repo2, number2, base2, head2,
                                          "suppressed",
                                          note=f"sweep: {reason}")
                continue
            try:
                subj, body = build_email(
                    {"repo": live.repo, "number": live.number,
                     "url": live.url, "base": live.base_sha,
                     "head": live.head_sha}, Path(rd))
            except Exception as exc:
                self.store.set_status(repo2, number2, base2, head2,
                                      "suppressed",
                                      note=f"sweep identity invalid: {exc}")
                continue
            import hashlib as _hl
            sha = _hl.sha256(body.encode()).hexdigest()
            if self.sender is None:
                continue
            try:
                receipt = self.sender(rd, subj, body)
            except Exception as exc:
                self.store.record_delivery(rd, sha, subj, "failed",
                                           {"error": f"{type(exc).__name__}: {exc}"})
                continue
            from codatiel_i2 import watcher as _W
            result = _W.classify_receipt(receipt)
            self.store.record_delivery(rd, sha, subj, result, receipt)
            out["emails"].append({"pr": f"{repo2}#{number2}",
                                  "result": result, "redelivery": True})
            if result == "delivered":
                self.store.set_status(repo2, number2, base2, head2,
                                      "delivered")
        return out

    # -- budget ---------------------------------------------------------
    def availability(self, day: str) -> float:
        total_left = self.cfg.spend_total_cap_usd - self.store.spend_total()
        day_left = self.cfg.spend_daily_cap_usd - self.store.spend_day(day)
        return min(total_left, day_left) - self.store.reserved_total()

    def send_paused_notice(self, day: str) -> None:
        if not self.cfg.recipients or not self.sender:
            return
        # paused notice sent at most once per activation (store flag)
        if self.store.meta_get("paused_notified"):
            return
        body = (f"Codatiel v0.1 paused: spend caps reached (total "
                f"${self.store.spend_total():.4f} / "
                f"${self.cfg.spend_total_cap_usd:.2f}, today "
                f"${self.store.spend_day(day):.4f} / "
                f"${self.cfg.spend_daily_cap_usd:.2f}, reserved "
                f"${self.store.reserved_total():.4f}). No new paid reviews "
                f"will start.")
        try:
            receipt = self.sender(None, "[codatiel-shadow] paused-by-budget",
                                  body)
            from codatiel_i2 import watcher as W
            if W.classify_receipt(receipt) == "delivered":
                self.store.meta_set("paused_notified", "1")
        except Exception:
            pass

    # -- dispatch -------------------------------------------------------
    def revalidate(self, repo: str, number: int, base: str,
                   head: str) -> tuple[E.ValidatedPR | None, str]:
        """Fresh fetch + validate + match the queued pair. Fail closed."""
        try:
            raw, err = self.fetch(repo, number)
        except Exception as exc:
            return None, f"{_FETCH_PREFIX}{type(exc).__name__}: {exc}"
        if raw is None:
            if (err or "") == _NOT_FOUND:
                return None, _NOT_FOUND
            return None, f"{_FETCH_PREFIX}{err or 'failed'}"
        vpr, reason = E.validate_pr_metadata(repo, raw)
        if vpr is None:
            return None, reason
        if (vpr.base_sha, vpr.head_sha) != (base, head):
            return None, "stale (head/base moved)"
        if vpr.is_draft or vpr.merged_at or vpr.state != "OPEN":
            return None, "lifecycle changed (draft/merged/closed)"
        return vpr, ""

    def dispatch(self, repo: str, number: int, base: str,
                 head: str, day: str) -> dict:
        rec: dict[str, Any] = {"pr": f"{repo}#{number}",
                               "base": base, "head": head}
        live, reason = self.revalidate(repo, number, base, head)
        if live is None:
            if reason.startswith(_FETCH_PREFIX) or _NOT_FOUND not in reason:
                # Transient fetch-layer failure: stay eligible, retry next
                # cycle. No inference, no suppression on a blip.
                self.store.set_status(repo, number, base, head, "eligible",
                                      note=f"revalidate retry: {reason}")
                rec["retry"] = reason
                return rec
            self.store.set_status(repo, number, base, head, "suppressed",
                                  note=reason)
            rec["suppressed"] = reason
            return rec
        # From here on, the freshly revalidated record is authoritative.
        vpr = live
        rec.update(self._run_one_pass(vpr, "luna", day, second_pass=False))
        if (rec.get("run_dir") and rec.get("status") in HAIKU_TRIGGER
                and self.availability(day) >= PER_REVIEW_RESERVE_USD):
            live2, reason2 = self.revalidate(vpr.repo, vpr.number,
                                             vpr.base_sha, vpr.head_sha)
            if live2 is None:
                if reason2.startswith(_FETCH_PREFIX):
                    rec["follow_up_retry"] = reason2
                else:
                    rec["follow_up_suppressed"] = reason2
            else:
                rec["follow_up"] = self._run_one_pass(
                    live2, "haiku", day, second_pass=True)
        return rec

    def _run_one_pass(self, vpr: ValidatedPR, backend: str, day: str,
                      second_pass: bool) -> dict:
        """One bounded review pass: reserve -> run -> account -> email."""
        if second_pass:
            cur = self.store.get_pair(vpr.repo, vpr.number, vpr.base_sha,
                                      vpr.head_sha) or {}
            reserve = float(cur.get("reserved_usd") or 0.0)
            reserve += PER_REVIEW_RESERVE_USD
        else:
            reserve = PER_REVIEW_RESERVE_USD
        self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                              vpr.head_sha, "running", reserved_usd=reserve)
        try:
            out = self.run_review(backend, vpr, PER_REVIEW_RESERVE_USD)
        except Exception as exc:
            out = {"status": "failed", "stage": "spawn",
                   "error": f"{type(exc).__name__}: {exc}"}
        self.account_run(vpr, out, day, second_pass=second_pass)
        res = self.maybe_email(vpr, out, second_pass=second_pass)
        res["status"] = out.get("status")
        if out.get("run_dir"):
            res["run_dir"] = out.get("run_dir")
        return res

    def run_review(self, backend: str, vpr: ValidatedPR,
                   ceiling: float) -> dict:
        """Subprocess-isolated review. Crash of the child cannot take the
        manager down (asyncio event-loop reuse class of bug contained)."""
        if self._stop:
            return {"status": "failed", "stage": "cancelled",
                    "error": "shutdown requested"}
        cmd = ["codatiel-i2", "review", "--repo", vpr.repo,
               "--pr", str(vpr.number), "--model", backend,
               "--expected-base", vpr.base_sha,
               "--expected-head", vpr.head_sha,
               "--runs-root", str(self.cfg.runs_root / backend / "A"),
               "--work-root", str(self.cfg.work_root / backend / "A"),
               "--max-turns", "12", "--max-spend-usd", str(ceiling)]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        except Exception as exc:
            return {"status": "failed", "stage": "spawn",
                    "error": f"{type(exc).__name__}: {exc}"}
        try:
            stdout, _ = proc.communicate(timeout=REVIEW_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=60)
            except Exception:
                pass
            return {"status": "failed", "stage": "timeout",
                    "error": f"review exceeded {REVIEW_TIMEOUT_S}s; killed"}
        if self._stop:
            return {"status": "failed", "stage": "cancelled",
                    "error": "shutdown during review"}
        try:
            payload = json.loads((stdout or "").strip().splitlines()[-1])
        except Exception:
            return {"status": "failed", "stage": "parse",
                    "error": "review subprocess produced no JSON payload"}
        if not isinstance(payload, dict):
            return {"status": "failed", "stage": "parse",
                    "error": "review payload not an object"}
        return payload

    PRE_INFERENCE_STAGES = ("pr_metadata", "sha_mismatch", "workspace",
                              "inventory_strategy", "spend_ledger", "model",
                              "budget_refused", "spawn")

    def account_run(self, vpr: ValidatedPR, out: dict, day: str,
                    second_pass: bool) -> None:
        run_dir = out.get("run_dir")
        stage = str(out.get("stage") or "")
        prev = self.store.get_pair(vpr.repo, vpr.number, vpr.base_sha,
                                   vpr.head_sha) or {}
        prev_usd = prev.get("usd") or 0.0
        prev_unknown = bool(prev.get("spend_unknown"))
        if stage in self.PRE_INFERENCE_STAGES and not run_dir:
            # Certain zero: the child never reached inference. Release the
            # reservation; spawn failures stay eligible (transient),
            # policy refusals suppress (operator backfill only).
            if stage == "spawn":
                self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                                      vpr.head_sha, "eligible",
                                      reserved_usd=0.0,
                                      note=f"spawn failed, will retry: "
                                           f"{out.get('error', '')[:200]}")
            else:
                self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                                      vpr.head_sha, "suppressed",
                                      reserved_usd=0.0,
                                      note=f"pre-inference stop: {stage}")
            return
        usd: float | None = None
        unknown = True
        if run_dir:
            try:
                m = json.loads((Path(run_dir) / _METRICS_JSON)
                                 .read_text(encoding="utf-8"))
                raw_usd = (m.get("spend") or {}).get("usd_estimate")
                unknown = (m.get("spend") or {}).get(
                    "accounting_status") == "UNKNOWN_USAGE"
                usd = float(raw_usd) if raw_usd is not None else 0.0
            except Exception:
                usd, unknown = None, True
        total_usd = prev_usd + (usd or 0.0)
        total_unknown = (prev_unknown or unknown) and True
        if not unknown and usd is not None:
            # Known pass spend is debited immediately, even when an older
            # pass on the same pair is still bounded-but-unknown.
            self.store.spend_add(day, usd)
        if unknown or usd is None:
            # Conservatively bounded: the dispatch-time reservation stands
            # (dispatch already accumulates $1 per pass). Operator
            # `reconcile` replaces reserves with provider actuals.
            kept = float(prev.get("reserved_usd") or 0.0)
            self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                                  vpr.head_sha,
                                  "failed" if out.get("status") == "failed"
                                  else "complete",
                                  run_dir=run_dir or "",
                                  model=out.get("model", ""),
                                  second_pass=int(second_pass),
                                  usd=total_usd, spend_unknown=1,
                                  reserved_usd=kept,
                                  note="accounting unknown; spend bounded "
                                       "by reservation")
            return
        kept = float(prev.get("reserved_usd") or 0.0)
        # Each pass releases its own $1 on known accounting; other passes'
        # bounds are untouched.
        kept = max(0.0, kept - PER_REVIEW_RESERVE_USD)
        self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                              vpr.head_sha,
                              "failed" if out.get("status") == "failed"
                              else "complete",
                              run_dir=run_dir or "",
                              model=out.get("model", ""),
                              second_pass=int(second_pass),
                              usd=total_usd, spend_unknown=1 if total_unknown
                              else 0, reserved_usd=kept)

    def maybe_email(self, vpr: ValidatedPR, out: dict,
                    second_pass: bool) -> dict:
        res: dict[str, Any] = {}
        run_dir = out.get("run_dir")
        if not run_dir:
            return {"emailed": False, "why": out.get("error", "no run_dir")}
        identity = {"repo": vpr.repo, "number": vpr.number,
                    "url": vpr.url, "base": vpr.base_sha,
                    "head": vpr.head_sha}
        live, reason = self.revalidate(vpr.repo, vpr.number, vpr.base_sha,
                                       vpr.head_sha)
        if live is None:
            if reason.startswith(_FETCH_PREFIX):
                # Transient failure after paid work: do NOT suppress the
                # result. Leave delivery pending; the redelivery sweep
                # retries the same bytes next cycle.
                return {"emailed": False, "delivery": "revalidate-retry"}
            self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                                  vpr.head_sha, "suppressed",
                                  note=f"pre-email: {reason}")
            return {"emailed": False, "suppressed": reason}
        stale = False
        try:
            subj, body = build_email(identity, Path(run_dir),
                                     second_pass=second_pass)
        except Exception as exc:
            self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                                  vpr.head_sha, "suppressed",
                                  note=f"email identity invalid: {exc}")
            return {"emailed": False, "suppressed": f"identity: {exc}"}
        sha = hashlib.sha256(body.encode()).hexdigest()
        existing = self.store.get_delivery(run_dir, sha)
        if existing and existing["status"] == "delivered":
            self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                                  vpr.head_sha, "delivered")
            return {"emailed": False, "duplicate_suppressed": True}
        from codatiel_i2 import service as S
        try:
            record = S.deliver_shadow(
                run_dir, to=self.cfg.recipients, sender=self.cfg.sender,
                subject=subj, text_body=body,
                transport=(lambda t, s, b: self.sender(run_dir, s, b))
                if self.sender else None)
            receipt = record.get("transport_receipt", {})
        except Exception as exc:
            self.store.record_delivery(run_dir, sha, subj, "failed",
                                       {"error": f"{type(exc).__name__}: {exc}"})
            return {"emailed": False, "delivery": "transport-error"}
        from codatiel_i2 import watcher as W
        result = W.classify_receipt(receipt)
        self.store.record_delivery(run_dir, sha, subj, result, receipt)
        if result in ("failed", "partial"):
            # Same-bytes retry happens via the redelivery sweep, never by
            # re-running inference. Ambiguous receipts are never
            # auto-retried (explicit replay-delivery only).
            return {"emailed": False, "delivery": result}
        if result == "delivered":
            self.store.set_status(vpr.repo, vpr.number, vpr.base_sha,
                                  vpr.head_sha, "delivered")
            return {"emailed": True}
        return {"emailed": False, "delivery": result}

    # -- recovery -------------------------------------------------------
    def recover(self) -> dict:
        """Adopt crash-orphaned work. Never repeats inference or delivery
        that already happened: run dirs and delivery receipts are the
        source of truth, not memory."""
        out: dict[str, Any] = {"adopted": 0, "requeued": 0,
                               "delivered_adopted": 0}
        rows = self.store.cx.execute(
            "SELECT repo, pr, base, head, run_dir FROM pairs "
            "WHERE status IN ('queued','running')").fetchall()
        for repo, pr, base, head, run_dir in rows:
            if run_dir and Path(run_dir, _METRICS_JSON).exists():
                try:
                    m = json.loads((Path(run_dir) / _METRICS_JSON)
                                     .read_text(encoding="utf-8"))
                    dl_path = Path(run_dir) / "delivery.json"
                    if dl_path.exists():
                        dl = json.loads(dl_path.read_text(encoding="utf-8"))
                        from codatiel_i2 import watcher as W
                        if W.classify_receipt(
                                dl.get("transport_receipt")) == "delivered":
                            self.store.record_delivery(
                                run_dir,
                                dl.get("body_sha256", ""),
                                dl.get("subject", ""), "delivered",
                                dl.get("transport_receipt", {}))
                            self.store.set_status(repo, pr, base, head,
                                                  "delivered")
                            out["delivered_adopted"] += 1
                            continue
                    self.store.set_status(
                        repo, pr, base, head,
                        "failed" if m.get("status") == "failed"
                        else "complete", run_dir=run_dir)
                    out["adopted"] += 1
                except Exception:
                    self.store.set_status(repo, pr, base, head, "eligible")
                    out["requeued"] += 1
            else:
                self.store.set_status(repo, pr, base, head, "eligible")
                out["requeued"] += 1
        return out


def uuid4() -> str:
    import uuid
    return uuid.uuid4().hex[:12]
