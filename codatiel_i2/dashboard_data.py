"""DASH-01 read-only data layer (FastAPI migration).

Every collector here is pure SQLite SELECTs (plus local run-file
reads) and never writes: connections open the production database in
``mode=ro`` with ``PRAGMA query_only=ON``. The authoritative
accounting stays in the event pipeline — this module reuses
``operator_config.spend_on_day`` and the effective-limit helpers
rather than re-deriving money, and labels every figure as
Codatiel-ledger-only.

Conventions shared by all collectors:

- money is rounded to 6 decimals; unknown cost is reported as a
  count plus ``None`` amounts, never as confirmed ``$0``;
- superseded inference is attributed separately from settled spend
  (same two-source formula as ``spend_on_day``);
- semdup pair counts and per-stage spend are memo components only —
  they are never summed into review totals (no double counting);
- anything that cannot be derived from existing records is returned
  as ``None`` with a reason and rendered as "Unavailable".
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import time
import types
from pathlib import Path
from typing import Any

from codatiel_i2 import operator_config as opcfg

LEDGER_LABEL = ("Codatiel ledger only — provider-confirmed usage "
                "recorded locally; NOT total OpenAI spend.")

GITHUB_URL = "https://github.com/{repo}/pull/{pr}"

#: Poller is 5-minute cron; three missed intervals reads as stale.
STALE_AFTER_S = 900.0

#: Decisions that mean "budget refused; retry is possible".
BUDGET_BLOCKED_DECISIONS = frozenset({
    "skipped:ceiling",
})

#: Decisions that mean "this head was superseded, never reviewed".
STALE_DECISIONS = frozenset({
    "skipped:stale_head",
    "skipped:stale_superseded",
})

#: Terminal failure shapes (review ran and failed, or attention needed).
FAILED_DECISIONS = frozenset({
    "failed",
    "needs_attention:interrupted",
    "needs_attention:unknown_endpoint",
    "needs_attention:repo_access",
    "needs_attention:identity",
    "needs_attention:compare_failed",
    "needs_attention:truncated",
    "needs_attention:unmapped_push",
})

REVIEW_STATES = (
    "pending",
    "running",
    "completed",
    "budget_blocked",
    "stale_superseded",
    "failed",
    "skipped_replayed",
)


def review_state(decision: str, original_decision: str = "") -> str:
    """Operator taxonomy for one delivery/review decision.

    A budget refusal is never presented as completed: ceiling-shaped
    decisions (directly or via a replay wrapper) always map to
    budget_blocked.
    """
    dec = str(decision or "")
    if dec == "skipped:replay":
        dec = str(original_decision or "")
    if dec in BUDGET_BLOCKED_DECISIONS:
        return "budget_blocked"
    if dec in STALE_DECISIONS:
        return "stale_superseded"
    if dec in FAILED_DECISIONS or dec.startswith("needs_attention:"):
        return "failed"
    if dec == "completed":
        return "completed"
    if dec in ("in_progress", "queued"):
        return "running"
    if dec in ("deferred", "draft"):
        return "pending"
    return "skipped_replayed"


def _parse_detail(detail: Any) -> dict:
    try:
        parsed = json.loads(detail or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _original_decision(row: dict) -> str:
    if str(row.get("decision") or "") != "skipped:replay":
        return ""
    return str(_parse_detail(row.get("detail")).get(
        "original_decision") or "")


# -- read-only connection ----------------------------------------------------


def open_ro(state_dir: Path | str) -> sqlite3.Connection | None:
    """Open events.sqlite read-only; None when absent/unreadable."""
    db = Path(state_dir) / "events.sqlite"
    if not db.exists():
        return None
    try:
        cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
        cx.execute("PRAGMA query_only=ON;")
        cx.execute("SELECT COUNT(*) FROM deliveries").fetchone()
        return cx
    except Exception:
        try:
            cx.close()
        except Exception:
            pass
        return None


def close_ro(cx: sqlite3.Connection | None) -> None:
    try:
        if cx is not None:
            cx.close()
    except Exception:
        pass


def _rows(cx: sqlite3.Connection, sql: str,
          params: tuple = ()) -> list[dict]:
    cur = cx.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# -- poller health -------------------------------------------------------------


def read_poll(state_dir: Path | str) -> dict[str, Any]:
    last = ""
    try:
        last = (Path(state_dir) / "poll.last").read_text(
            encoding="utf-8").strip().splitlines()[0:1][0]
    except Exception:
        last = ""
    tail: list[str] = []
    try:
        lines = (Path(state_dir) / "poll.log").read_text(
            encoding="utf-8").splitlines()
        tail = lines[-30:]
    except Exception:
        tail = []
    last_end = ""
    for line in reversed(tail):
        if " END " in line:
            last_end = line
            break
    ok: bool | None = ("END ok" in last_end) if last_end else None
    return {"last_successful_poll": last or None,
            "last_end_line": last_end or None,
            "last_end_ok": ok,
            "log_tail": tail}


def poll_status(poll: dict[str, Any],
                now: float | None = None) -> dict[str, Any]:
    """Classify poller health. An idle poller is not broken.

    Rules (shown in the UI): error when the last cycle failed;
    idle when no poll has ever recorded; stale when the last success
    is older than three cron intervals; running otherwise. A cycle
    with zero open PRs is a healthy idle tick, not a failure.
    """
    now = time.time() if now is None else now
    last = poll.get("last_successful_poll")
    ok = poll.get("last_end_ok")
    if ok is False:
        return {"state": "error",
                "note": "last poll cycle did not end ok — see log tail"}
    if not last:
        return {"state": "idle",
                "note": "no successful poll recorded yet"}
    try:
        ts = _dt.datetime.strptime(
            last, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=_dt.timezone.utc).timestamp()
    except Exception:
        return {"state": "error",
                "note": f"unparseable poll timestamp {last!r}"}
    age = now - ts
    if age < -60.0:
        return {"state": "running",
                "note": f"last success {last} (clock skew: timestamp "
                        f"is {-age / 60:.0f} min in the future)"}
    if age > STALE_AFTER_S:
        return {"state": "stale",
                "note": f"last success {last} ({age / 60:.0f} min ago; "
                        f"stale past {STALE_AFTER_S / 60:.0f} min)"}
    return {"state": "running",
            "note": f"last success {last} ({age / 60:.0f} min ago)"}


# -- overview ------------------------------------------------------------------


def collect_overview(state_dir: Path | str,
                     now: float | None = None) -> dict[str, Any]:
    """Top-level operator picture. Read-only; tolerates absence."""
    state_dir = Path(state_dir)
    settings, source = opcfg.load(state_dir)
    data: dict[str, Any] = {
        "state_dir": str(state_dir),
        "operator_source": source,
        "settings": settings.to_dict(),
        "spend_label": LEDGER_LABEL,
        "store_present": (state_dir / "events.sqlite").exists(),
    }
    poll = read_poll(state_dir)
    data["poll"] = poll
    data["poller"] = poll_status(poll, now=now)
    cx = open_ro(state_dir)
    if cx is None:
        data.update({
            "available": False,
            "reason": ("no readable events.sqlite in state dir; "
                       "worker may not be enabled yet"),
            "ledger": None, "decisions": {}, "reviews": {},
            "budget_blocked": [], "stale": [], "failed": [],
            "efficiency": None,
        })
        data["audit"] = opcfg.audit_trail(state_dir)
        data["env"] = _env_snapshot()
        return data
    try:
        data["available"] = True
        data["enabled_at"] = _meta(cx, "enabled_at")
        data["pipeline"] = _meta(cx, "pipeline")
        try:
            data["repos"] = json.loads(_meta(cx, "repos") or "[]")
        except Exception:
            data["repos"] = []
        data["enabled"] = bool(data["enabled_at"])
        data["ledger"] = _spend_totals(cx)
        data["decisions"] = _decisions(cx)
        data["reviews"] = _review_counts(cx)
        data["budget_blocked"] = _recent_by_state(
            cx, "budget_blocked", limit=10)
        data["stale"] = _recent_by_state(cx, "stale_superseded", limit=10)
        data["failed"] = _recent_by_state(cx, "failed", limit=10)
        data["recent"] = _recent_activity(cx)
        data["efficiency"] = _efficiency_summary(cx)
        try:
            light = _rows(cx, "SELECT run_dir, cost_usd, cost_unknown "
                              "FROM reviews")
        except Exception:
            light = []
        data["by_model"] = _spend_by_model(state_dir, light)
        data["email"] = _email_outcomes(state_dir, light)
        data["publications"] = _publications(state_dir, cx)
        data["budget_remaining"] = _budget_remaining(
            data.get("ledger") or {}, data.get("effective") or {}, cx,
            now=now)
    finally:
        close_ro(cx)
    data["effective"] = _effective_limits(state_dir)
    data["audit"] = opcfg.audit_trail(state_dir)
    data["env"] = _env_snapshot()
    return data


def _meta(cx: sqlite3.Connection, key: str) -> str | None:
    try:
        row = cx.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def _spend_totals(cx: sqlite3.Connection) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in ("settled_usd", "reserved_usd", "unknown_holds"):
        try:
            row = cx.execute(
                "SELECT value FROM spend WHERE key=?", (key,)).fetchone()
            out[key] = float(row[0]) if row and row[0] is not None else 0.0
        except Exception:
            out[key] = 0.0
    return out


def _decisions(cx: sqlite3.Connection) -> dict[str, int]:
    try:
        rows = cx.execute(
            "SELECT decision, COUNT(*) FROM deliveries "
            "GROUP BY decision").fetchall()
    except Exception:
        return {}
    return {str(d): int(n) for d, n in rows}


def _review_counts(cx: sqlite3.Connection) -> dict[str, Any]:
    try:
        rows = _rows(cx, "SELECT status FROM reviews")
    except Exception:
        return {"completed": 0, "failed": 0, "prs_reviewed": 0}
    done = [r for r in rows if r.get("status") in ("complete", "inconclusive")]
    failed = [r for r in rows if r not in done]
    try:
        prs = _rows(cx, "SELECT DISTINCT repo, pr FROM reviews "
                         "WHERE status IN ('complete','inconclusive')")
    except Exception:
        prs = []
    return {"completed": len(done), "failed": len(failed),
            "prs_reviewed": len(prs)}


def _recent_by_state(cx: sqlite3.Connection, state: str,
                     limit: int = 10) -> list[dict]:
    try:
        rows = _rows(
            cx, "SELECT delivery_id, repo, pr, head_sha, decision, "
                "reason, cost_usd, received_ts, detail FROM deliveries "
                "ORDER BY received_ts DESC LIMIT 500")
    except Exception:
        return []
    out = []
    for row in rows:
        if review_state(row.get("decision") or "",
                        _original_decision(row)) != state:
            continue
        out.append({"delivery_id": row.get("delivery_id"),
                    "repo": row.get("repo"), "pr": row.get("pr"),
                    "head_sha": (row.get("head_sha") or "")[:12],
                    "decision": row.get("decision"),
                    "reason": row.get("reason"),
                    "cost_usd": row.get("cost_usd"),
                    "github_url": GITHUB_URL.format(
                        repo=row.get("repo"), pr=row.get("pr"))})
        if len(out) >= limit:
            break
    return out


def _recent_activity(cx: sqlite3.Connection,
                      limit: int = 8) -> dict[str, Any]:
    try:
        drows = _rows(
            cx, "SELECT delivery_id, repo, pr, decision, reason, "
                "received_ts, detail FROM deliveries "
                "ORDER BY received_ts DESC LIMIT ?", (limit,))
        rrows = _rows(
            cx, "SELECT repo, pr, kind, pipeline, status, cost_usd, "
                "cost_unknown FROM reviews ORDER BY finished_ts DESC "
                "LIMIT ?", (limit,))
    except Exception:
        return {"deliveries": [], "reviews": []}
    return {
        "deliveries": [{
            "repo": d.get("repo"), "pr": d.get("pr"),
            "decision": d.get("decision"),
            "reason": (d.get("reason") or "")[:120],
            "state": review_state(
                d.get("decision") or "", _original_decision(d)),
            "github_url": GITHUB_URL.format(
                repo=d.get("repo"), pr=d.get("pr")),
        } for d in drows],
        "reviews": [{
            "repo": r.get("repo"), "pr": r.get("pr"),
            "kind": r.get("kind"), "status": r.get("status"),
            "cost_usd": r.get("cost_usd"),
            "cost_unknown": bool(r.get("cost_unknown")),
            "github_url": GITHUB_URL.format(
                repo=r.get("repo"), pr=r.get("pr")),
        } for r in rrows],
    }


def _efficiency_summary(cx: sqlite3.Connection) -> dict[str, Any]:
    """Measured cache/stale counts; avoided USD stays Unavailable."""
    try:
        rows = _rows(cx, "SELECT decision, detail FROM deliveries")
    except Exception:
        return {"available": False,
                "reason": "deliveries unreadable"}
    examined = reused = cache_hits = 0
    have_pairs = False
    for row in rows:
        det = _parse_detail(row.get("detail"))
        if det.get("provenance_version") != "cost01b-v1":
            continue
        for key, acc in (("pairs_examined", "e"), ("pairs_reused", "r"),
                         ("pairs_cache_hits", "h")):
            val = det.get(key)
            if isinstance(val, (int, float)):
                have_pairs = True
                if acc == "e":
                    examined += int(val)
                elif acc == "r":
                    reused += int(val)
                else:
                    cache_hits += int(val)
    stale_prevented = sum(
        1 for row in rows
        if review_state(row.get("decision") or "",
                        _original_decision(row)) == "stale_superseded")
    try:
        delivered = cx.execute(
            "SELECT COUNT(*) FROM finding_deliveries "
            "WHERE outcome='posted'").fetchone()[0]
    except Exception:
        delivered = None
    return {"available": True,
            "pairs_examined": examined if have_pairs else None,
            "pairs_reused": reused if have_pairs else None,
            "pairs_cache_hits": cache_hits if have_pairs else None,
            "cache_hit_pct": (round(100.0 * reused / examined, 1)
                              if have_pairs and examined else None),
            "stale_prevented": stale_prevented,
            "findings_delivered": delivered,
            "avoided_usd": None,
            "avoided_note": ("Unavailable: no per-pair pricing is "
                             "recorded; a USD counterfactual needs "
                             "assumptions the ledger does not make.")}


def _effective_limits(state_dir: Path) -> dict[str, Any]:
    try:
        from codatiel_i2 import event_pipeline as pipe_mod
        cfg = pipe_mod.config_from_env()
        cfg = opcfg.apply_to_event_config(cfg)
        return {"available": True,
                "monthly_effective_usd": cfg.spend_cap_usd,
                "daily_effective_usd": cfg.daily_cap_usd,
                "per_pr_effective_usd": (cfg.whole_pr_ceiling_usd
                                         if cfg.env == "dev" else None),
                "pipeline": cfg.pipeline,
                "repos": cfg.repos,
                "operator_source": cfg.operator_source,
                "reserve_usd": cfg.reserve_usd}
    except Exception as exc:
        return {"available": False,
                "note": f"live worker config unavailable: {exc}"}


def _budget_remaining(ledger: dict[str, Any],
                      effective: dict[str, Any],
                      cx: sqlite3.Connection | None = None,
                      now: float | None = None) -> dict[str, Any]:
    if not effective.get("available"):
        return {"available": False,
                "note": "effective caps unavailable"}
    settled = float(ledger.get("settled_usd") or 0.0)
    out: dict[str, Any] = {"available": True}
    monthly = effective.get("monthly_effective_usd")
    out["monthly_remaining_usd"] = (round(float(monthly) - settled, 6)
                                    if monthly is not None else None)
    # Daily remaining reuses the authoritative day ledger (SELECTs
    # only) via a shim exposing the read-only connection.
    daily = effective.get("daily_effective_usd")
    if daily is None or cx is None:
        out["daily_remaining_usd"] = None
    else:
        try:
            today = _dt.datetime.fromtimestamp(
                time.time() if now is None else now,
                tz=_dt.timezone.utc).strftime("%Y-%m-%d")
            spent = opcfg.spend_on_day(types.SimpleNamespace(cx=cx), today)
            out["daily_remaining_usd"] = round(
                float(daily) - float(
                    spent.get("codatiel_ledger_usd") or 0.0), 6)
            out["daily_day"] = today
        except Exception as exc:
            out["daily_remaining_usd"] = None
            out["daily_note"] = f"day ledger unreadable: {exc}"
    return out


def _env_snapshot() -> dict[str, Any]:
    """Non-secret env config: secret-sounding keys show presence only."""
    names = ["CODATIEL_ENV", "CODATIEL_DEV_STATE", "CODATIEL_TEST_STATE",
             "CODATIEL_DEV_REPOS", "CODATIEL_TEST_REPOS",
             "CODATIEL_DEV_SPEND_CAP_USD", "CODATIEL_TEST_SPEND_CAP_USD",
             "CODATIEL_PROJECT_SPEND_CAP_USD", "CODATIEL_DEV_TO",
             "CODATIEL_DEV_SENDER", "CODATIEL_TEST_TO",
             "CODATIEL_TEST_SENDER", "CODATIEL_DEV_REPO_PATHS",
             "CODATIEL_TEST_REPO_PATHS", "REVIEW_PIPELINE",
             "CODATIEL_DEV_WHOLE_PR_CEILING_USD", "CODATIEL_EVENTS_TRANSPORT",
             "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
             "ZAI_API_KEY", "SMTP2GO_API_KEY", "SEMDUP_API_KEY",
             "CODATIEL_WEBHOOK_SECRET", "GH_TOKEN"]
    out: dict[str, Any] = {}
    import os as _os
    for name in names:
        present = bool(_os.environ.get(name, ""))
        if opcfg.secret_like(name):
            out[name] = {"value": None,
                         "display": opcfg.mask_value(name, present)}
        else:
            out[name] = {"value": _os.environ.get(name, "") if present
                         else None,
                         "display": _os.environ.get(name, "") if present
                         else "unset"}
    return out


# -- repositories + review queue -----------------------------------------------


def collect_repos(state_dir: Path | str) -> dict[str, Any]:
    """Per-repo queue: enabled flag, tracked PRs, latest activity."""
    state_dir = Path(state_dir)
    settings, _ = opcfg.load(state_dir)
    data: dict[str, Any] = {"available": False, "repos": []}
    cx = open_ro(state_dir)
    if cx is None:
        data["reason"] = "no readable events.sqlite"
        return data
    try:
        try:
            from codatiel_i2 import event_pipeline as pipe_mod
            cfg = opcfg.apply_to_event_config(pipe_mod.config_from_env())
            effective = list(opcfg.effective_repos(
                settings, list(cfg.repos or [])))
        except Exception:
            try:
                effective = list(json.loads(
                    _meta(cx, "repos") or "[]"))
            except Exception:
                effective = []
        data["available"] = True
        data["effective_repos"] = effective
        for repo in effective:
            data["repos"].append(_repo_row(cx, repo, settings))
        # Tracked repos outside the effective set (informational).
        try:
            known = [r[0] for r in cx.execute(
                "SELECT DISTINCT repo FROM checkpoints").fetchall()]
        except Exception:
            known = []
        for repo in sorted(set(known or []) - set(effective)):
            data["repos"].append(_repo_row(cx, repo, settings,
                                           enabled=False))
    finally:
        close_ro(cx)
    return data


def _repo_row(cx: sqlite3.Connection, repo: str,
              settings: Any, enabled: bool = True) -> dict[str, Any]:
    try:
        cps = _rows(cx, "SELECT pr, status, last_reviewed_head, "
                         "last_cost_usd, cost_unknown, updated_ts, note "
                         "FROM checkpoints WHERE repo=? ORDER BY pr",
                    (repo,))
    except Exception:
        cps = []
    try:
        recent = _rows(
            cx, "SELECT pr, head_sha, decision, reason, received_ts, "
                "detail FROM deliveries WHERE repo=? "
                "ORDER BY received_ts DESC LIMIT 200", (repo,))
    except Exception:
        recent = []
    try:
        spend_rows = _rows(
            cx, "SELECT COALESCE(SUM(cost_usd),0) AS known_usd FROM reviews "
                "WHERE repo=? AND NOT cost_unknown AND cost_usd IS NOT NULL",
            (repo,))
        spend = float(spend_rows[0]["known_usd"]) if spend_rows else 0.0
    except Exception:
        spend = None
    states: dict[str, int] = {}
    for row in recent:
        st = review_state(row.get("decision") or "",
                          _original_decision(row))
        states[st] = states.get(st, 0) + 1
    latest = recent[0] if recent else None
    return {"repo": repo, "enabled": enabled,
            "github_repo_url": f"https://github.com/{repo}",
            "tracked_prs": sorted({int(r["pr"]) for r in cps
                                   if r.get("pr")}),
            "checkpoints": len(cps),
            "queue_by_state": states,
            "latest_decision": (latest or {}).get("decision"),
            "latest_reason": (latest or {}).get("reason"),
            "spend_known_usd": spend,
            "prs": [{"pr": int(r["pr"]),
                     "status": r.get("status"),
                     "last_reviewed_head":
                         (r.get("last_reviewed_head") or "")[:12],
                     "github_url": GITHUB_URL.format(
                         repo=repo, pr=r["pr"])}
                    for r in cps if r.get("pr")]}


# -- per-PR history + detail -----------------------------------------------------


def collect_pr(state_dir: Path | str, repo: str, pr: int) -> dict[str, Any]:
    """Chronological review history for one PR."""
    state_dir = Path(state_dir)
    data: dict[str, Any] = {"available": False, "repo": repo, "pr": pr,
                            "github_url": GITHUB_URL.format(
                                repo=repo, pr=pr)}
    cx = open_ro(state_dir)
    if cx is None:
        data["reason"] = "no readable events.sqlite"
        return data
    try:
        try:
            cps = _rows(cx, "SELECT * FROM checkpoints WHERE repo=? "
                             "AND pr=?", (repo, pr))
        except Exception:
            cps = []
        data["available"] = True
        data["checkpoint"] = _shorten(cps[0]) if cps else None
        try:
            deliveries = _rows(
                cx, "SELECT * FROM deliveries WHERE repo=? AND pr=? "
                    "ORDER BY received_ts", (repo, pr))
        except Exception:
            deliveries = []
        try:
            reviews = _rows(
                cx, "SELECT * FROM reviews WHERE repo=? AND pr=? "
                    "ORDER BY finished_ts", (repo, pr))
        except Exception:
            reviews = []
        try:
            pubs = _rows(cx, "SELECT * FROM publications WHERE repo=? "
                              "AND pr=? ORDER BY created_ts", (repo, pr))
        except Exception:
            pubs = []
        try:
            fdel = _rows(cx, "SELECT * FROM finding_deliveries "
                              "WHERE repo=? AND pr=? ORDER BY created_ts",
                         (repo, pr))
        except Exception:
            fdel = []
        data["found"] = bool(cps or deliveries or reviews)
        data["deliveries"] = [_delivery_view(d, state_dir) for d in deliveries]
        data["reviews"] = [_review_view(r) for r in reviews]
        data["publications"] = [_shorten(p) for p in pubs]
        data["finding_deliveries"] = [_shorten(f) for f in fdel]
        data["generated_vs_delivered"] = _generated_vs_delivered(
            reviews, pubs, fdel, deliveries, state_dir)
    finally:
        close_ro(cx)
    return data


def _shorten(row: dict) -> dict:
    out = dict(row)
    for key in ("head_sha", "base_sha", "last_reviewed_head",
                "last_reviewed_base", "baseline_head", "queued_head",
                "in_progress_head"):
        if out.get(key):
            out[key + "_short"] = str(out[key])[:12]
    return out


def _delivery_view(row: dict, state_dir: Path) -> dict:
    det = _parse_detail(row.get("detail"))
    view = _shorten(row)
    view["state"] = review_state(row.get("decision") or "",
                                 det.get("original_decision") or "")
    view["cache_reuse"] = {
        "pairs_examined": det.get("pairs_examined"),
        "pairs_reused": det.get("pairs_reused"),
        "pairs_cache_hits": det.get("pairs_cache_hits"),
        "semdup_status": det.get("semdup_status"),
        "semdup_published": det.get("semdup_published"),
    }
    view["spend_memo"] = {
        "total_cost_usd": det.get("total_cost_usd"),
        "terra_spend_usd": det.get("terra_spend_usd"),
        "luna_spend_usd": det.get("luna_spend_usd"),
        "semdup_terra_spend_usd": det.get("semdup_terra_spend_usd"),
        "semdup_luna_spend_usd": det.get("semdup_luna_spend_usd"),
        "cost_unknown": det.get("cost_unknown"),
    }
    view["run_findings"] = _run_findings(
        state_dir, det.get("run_dir") or "")
    return view


def _review_view(row: dict) -> dict:
    view = _shorten(row)
    view["state"] = ("completed"
                     if str(row.get("status") or "") in (
                         "complete", "inconclusive") else "failed")
    view["github_url"] = GITHUB_URL.format(
        repo=row.get("repo"), pr=row.get("pr"))
    return view


def _run_findings(state_dir: Path, run_dir: str) -> dict[str, Any]:
    """Published vs suppressed findings from a run's semdup.json."""
    if not run_dir:
        return {"available": False, "reason": "no run recorded"}
    sem = Path(run_dir) / "semdup.json"
    if not sem.exists() and not Path(run_dir).is_absolute():
        sem = state_dir / run_dir / "semdup.json"
    try:
        doc = json.loads(sem.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False, "reason": "semdup.json unreadable"}
    pubs = doc.get("published") or []
    sups = doc.get("suppressed") or []
    by_reason: dict[str, int] = {}
    for item in sups:
        reason = str((item or {}).get("reason") or "unspecified")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {"available": True,
            "published": len(pubs),
            "suppressed": len(sups),
            "suppressed_by_reason": by_reason}


def _generated_vs_delivered(reviews: list[dict], pubs: list[dict],
                            fdel: list[dict], deliveries: list[dict],
                            state_dir: Path) -> dict[str, Any]:
    """Three-way distinction: generated, published, previously delivered.

    - generated: findings the pipeline approved internally
      (terra findings + semdup published + semdup suppressed);
    - delivered: finding_deliveries rows with outcome posted, and
      publications rows with outcome posted;
    - suppressed-as-delivered: semdup suppressed rows whose reason is
      an already-delivered marker (never counted as delivered).
    """
    terra_total = sum(int(r.get("terra_findings") or 0) for r in reviews)
    sem_pub = sem_sup = already = 0
    seen_runs: set[str] = set()
    for row in deliveries:
        run_dir = _parse_detail(row.get("detail")).get("run_dir") or ""
        if not run_dir or run_dir in seen_runs:
            continue
        seen_runs.add(run_dir)
        got = _run_findings(state_dir, run_dir)
        if not got.get("available"):
            continue
        sem_pub += int(got.get("published") or 0)
        sem_sup += int(got.get("suppressed") or 0)
        by_reason = got.get("suppressed_by_reason") or {}
        already += sum(n for reason, n in by_reason.items()
                       if "already_delivered" in reason)
    posted_findings = sum(1 for f in fdel
                          if str(f.get("outcome") or "") == "posted")
    posted_pubs = sum(1 for p in pubs
                      if str(p.get("outcome") or "") == "posted")
    return {"terra_findings": terra_total,
            "semdup_published": sem_pub,
            "semdup_suppressed": sem_sup,
            "generated_total": terra_total + sem_pub + sem_sup,
            "finding_deliveries_posted": posted_findings,
            "publications_posted": posted_pubs,
            "suppressed_as_previously_delivered": already}


# -- spending analytics ----------------------------------------------------------


def collect_spend(state_dir: Path | str, days: int = 30,
                  now: float | None = None) -> dict[str, Any]:
    """Ledger reconciliation: totals, daily series, splits, caps."""
    state_dir = Path(state_dir)
    now = time.time() if now is None else now
    data: dict[str, Any] = {"available": False, "days": int(days)}
    cx = open_ro(state_dir)
    if cx is None:
        data["reason"] = "no readable events.sqlite"
        return data
    try:
        data["available"] = True
        data["ledger"] = _spend_totals(cx)
        data["daily"] = _daily_series(cx, state_dir, int(days), now)
        data["by_repo"] = _spend_by_repo(cx)
        data["by_pr"] = _spend_by_pr(cx)
        data["stage_memo"] = _stage_memo(cx)
        data["superseded"] = _superseded(cx)
        data["unknown"] = _unknown(cx)
        data["effective"] = _effective_limits(state_dir)
    finally:
        close_ro(cx)
    return data


def _daily_series(cx: sqlite3.Connection, state_dir: Path, days: int,
                  now: float) -> list[dict]:
    """Reuse the authoritative day ledger for each of the last N days."""
    shim = types.SimpleNamespace(cx=cx)
    out = []
    for back in range(max(1, days)):
        day = _dt.datetime.fromtimestamp(
            now - back * 86400, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        try:
            row = opcfg.spend_on_day(shim, day)
        except Exception as exc:
            row = {"day": day, "error": str(exc)}
        out.append(row)
    return out


def _spend_by_repo(cx: sqlite3.Connection) -> list[dict]:
    try:
        known = _rows(
            cx, "SELECT repo, COUNT(*) AS n, "
                "COALESCE(SUM(cost_usd),0) AS known_usd FROM reviews "
                "WHERE NOT cost_unknown AND cost_usd IS NOT NULL "
                "GROUP BY repo ORDER BY repo")
        unk = _rows(cx, "SELECT repo, COUNT(*) AS n FROM reviews "
                         "WHERE cost_unknown OR cost_usd IS NULL "
                         "GROUP BY repo")
    except Exception:
        return []
    unk_by = {r["repo"]: r["n"] for r in unk}
    return [{"repo": r["repo"], "reviews": int(r["n"]),
             "known_usd": round(float(r["known_usd"]), 6),
             "unknown_cost_reviews": int(unk_by.get(r["repo"], 0))}
            for r in known]


def _spend_by_pr(cx: sqlite3.Connection) -> list[dict]:
    try:
        rows = _rows(
            cx, "SELECT repo, pr, COUNT(*) AS n, "
                "COALESCE(SUM(CASE WHEN cost_unknown=0 THEN "
                "COALESCE(cost_usd,0) ELSE 0 END),0) AS known_usd, "
                "COALESCE(SUM(cost_unknown),0) AS unk_n FROM reviews "
                "GROUP BY repo, pr ORDER BY repo, pr")
    except Exception:
        return []
    return [{"repo": r["repo"], "pr": r["pr"],
             "reviews": int(r["n"]),
             "known_usd": round(float(r["known_usd"]), 6),
             "unknown_cost_reviews": int(r["unk_n"]),
             "github_url": GITHUB_URL.format(
                 repo=r["repo"], pr=r["pr"])}
            for r in rows]


def _model_of_run(state_dir: Path, run_dir: str) -> str:
    try:
        path = Path(run_dir)
        metrics = path if path.is_absolute() else state_dir / path
        doc = json.loads((metrics / "metrics.json").read_text(
            encoding="utf-8"))
    except Exception:
        return "unknown"
    model = doc.get("model")
    return str(model) if model else "unknown"


def _spend_by_model(state_dir: Path,
                    light: list[dict]) -> list[dict]:
    agg: dict[str, dict[str, Any]] = {}
    for row in light[:5000]:
        model = _model_of_run(state_dir, str(row.get("run_dir") or ""))
        slot = agg.setdefault(model, {"model": model, "reviews": 0,
                                      "codatiel_ledger_usd": 0.0,
                                      "unknown_cost_reviews": 0})
        slot["reviews"] += 1
        if int(row.get("cost_unknown") or 0):
            slot["unknown_cost_reviews"] += 1
        else:
            slot["codatiel_ledger_usd"] = round(
                float(slot["codatiel_ledger_usd"])
                + float(row.get("cost_usd") or 0.0), 6)
    return sorted(agg.values(), key=lambda d: d["model"])


def _classify_receipt(receipt: Any) -> str:
    if not isinstance(receipt, dict):
        return "attempted-no-receipt"
    if receipt.get("ok") is True:
        return "delivered"
    try:
        failed = int(receipt.get("failed") or 0)
        succeeded = int(receipt.get("succeeded") or 0)
    except (TypeError, ValueError):
        failed, succeeded = 1, 0
    if receipt.get("parsed") and failed == 0 and succeeded > 0:
        return "delivered"
    return "failed"


def _email_outcomes(state_dir: Path, light: list[dict]) -> dict[str, Any]:
    counts = {"delivered": 0, "failed": 0, "attempted-no-receipt": 0,
              "no-delivery-record": 0}
    for row in light[:5000]:
        run_dir = str(row.get("run_dir") or "")
        if not run_dir:
            counts["no-delivery-record"] += 1
            continue
        path = Path(run_dir)
        record = path if path.is_absolute() else state_dir / path
        try:
            payload = json.loads((record / "delivery.json").read_text(
                encoding="utf-8"))
        except Exception:
            counts["no-delivery-record"] += 1
            continue
        outcome = _classify_receipt(payload.get("transport_receipt"))
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"by_outcome": counts,
            "note": "Shadow digest emails only — this pipeline never "
                    "posts PR comments."}


def _publications(state_dir: Path,
                  cx: sqlite3.Connection) -> dict[str, Any]:
    """Publication record: authoritative DB rows + legacy file stores."""
    try:
        db_rows = _rows(cx, "SELECT outcome, COUNT(*) AS n FROM "
                             "publications GROUP BY outcome")
    except Exception:
        db_rows = []
    by_outcome = {str(r["outcome"] or "unknown"): int(r["n"])
                  for r in db_rows}
    file_store: dict[str, Any] = {"configured": False,
                                  "published_total": 0, "source": None}
    json_store = state_dir / "publications.json"
    if json_store.exists():
        try:
            doc = json.loads(json_store.read_text(encoding="utf-8"))
            items = doc if isinstance(doc, list) else doc.get("items", [])
            file_store = {"configured": True,
                          "published_total": len(items),
                          "source": "publications.json"}
        except Exception as exc:
            file_store = {"configured": True, "published_total": None,
                          "source": "publications.json",
                          "error": f"unreadable: {exc}"}
    else:
        sqlite_store = state_dir / "publications.sqlite"
        if sqlite_store.exists():
            file_store = {"configured": True,
                          "published_total": None,
                          "source": "publications.sqlite",
                          "note": "legacy file store present; "
                                  "authoritative counts come from the "
                                  "event database"}
    return {"by_outcome": by_outcome,
            "posted": int(by_outcome.get("posted", 0)),
            "refused": int(by_outcome.get("refused", 0)),
            "file_store": file_store,
            "note": "GitHub comment record from the event database; "
                    "only outcome=posted rows count as delivered."}


def _stage_memo(cx: sqlite3.Connection) -> dict[str, Any]:
    """Per-stage spend as memo components (informational, not additive).

    Terra/Luna/semdup figures describe parts of the same review
    spend already counted in review totals; summing them with totals
    would double-count. Unknown stays unknown.
    """
    try:
        rows = _rows(cx, "SELECT cost_usd, detail FROM deliveries")
    except Exception:
        return {"available": False,
                "reason": "deliveries unreadable"}
    memo = {"available": True, "terra_usd": 0.0, "luna_usd": 0.0,
            "semdup_terra_usd": 0.0, "semdup_luna_usd": 0.0,
            "semdup_usd": 0.0, "unknown_components": 0,
            "note": ("Memo breakdown of review spend already counted "
                     "above; components are not additive with totals.")}
    for row in rows:
        det = _parse_detail(row.get("detail"))
        if det.get("provenance_version") != "cost01b-v1":
            continue
        if det.get("cost_unknown"):
            memo["unknown_components"] += 1
            continue
        for key in ("terra_usd", "luna_usd", "semdup_terra_usd",
                    "semdup_luna_usd", "semdup_usd"):
            val = det.get({"terra_usd": "terra_spend_usd",
                           "luna_usd": "luna_spend_usd",
                           "semdup_terra_usd": "semdup_terra_spend_usd",
                           "semdup_luna_usd": "semdup_luna_spend_usd",
                           "semdup_usd": "semdup_spend_usd"}[key])
            if isinstance(val, (int, float)):
                memo[key] = round(float(memo[key]) + float(val), 6)
    return memo


def _superseded(cx: sqlite3.Connection) -> dict[str, Any]:
    try:
        rows = _rows(
            cx, "SELECT cost_usd, detail FROM deliveries "
                "WHERE decision='skipped:stale_superseded' OR "
                "(decision='skipped:replay' AND json_extract(detail, "
                "'$.original_decision')='skipped:stale_superseded')")
    except Exception:
        return {"count": 0, "known_usd": 0.0, "unknown": 0}
    known = sum(float(r["cost_usd"] or 0.0) for r in rows
                if r.get("cost_usd") is not None)
    unknown = sum(1 for r in rows if r.get("cost_usd") is None)
    return {"count": len(rows), "known_usd": round(known, 6),
            "unknown": unknown,
            "note": ("Superseded inference attributed separately; "
                     "never merged into settled review spend.")}


def _unknown(cx: sqlite3.Connection) -> dict[str, Any]:
    try:
        n_rev = cx.execute("SELECT COUNT(*) FROM reviews WHERE "
                           "cost_unknown OR cost_usd IS NULL").fetchone()[0]
        n_sup = cx.execute("SELECT COUNT(*) FROM deliveries WHERE "
                           "(decision='skipped:stale_superseded' OR "
                           "(decision='skipped:replay' AND "
                           "json_extract(detail, '$.original_decision')="
                           "'skipped:stale_superseded')) "
                           "AND cost_usd IS NULL").fetchone()[0]
    except Exception:
        return {"reviews": None, "superseded": None}
    return {"reviews": int(n_rev), "superseded": int(n_sup),
            "note": ("Unknown cost is counted, never priced as $0; "
                     "the ledger holds reserves for unknown spend.")}


# -- efficiency diagnostics ------------------------------------------------------


def collect_efficiency(state_dir: Path | str) -> dict[str, Any]:
    """COST-01B benefits from measured records only."""
    cx = open_ro(state_dir)
    if cx is None:
        return {"available": False,
                "reason": "no readable events.sqlite"}
    try:
        summary = _efficiency_summary(cx)
    finally:
        close_ro(cx)
    summary["method"] = ("pairs_examined/reused/cache_hits summed from "
                         "cost01b-v1 delivery provenance; stale_prevented "
                         "counts stale-head/superseded decisions; "
                         "findings_delivered counts posted finding "
                         "receipts.")
    return summary


# -- operational diagnostics -----------------------------------------------------


def collect_ops(state_dir: Path | str, limit: int = 50,
                now: float | None = None) -> dict[str, Any]:
    """Polls, ingestions, failures, refusals, config changes."""
    state_dir = Path(state_dir)
    data: dict[str, Any] = {"available": False, "limit": int(limit)}
    poll = read_poll(state_dir)
    data["poll"] = poll
    data["poller"] = poll_status(poll, now=now)
    data["audit"] = opcfg.audit_trail(state_dir)
    cx = open_ro(state_dir)
    if cx is None:
        data["reason"] = "no readable events.sqlite"
        return data
    try:
        data["available"] = True
        try:
            data["deliveries"] = _rows(
                cx, "SELECT delivery_id, repo, pr, head_sha, decision, "
                    "reason, cost_usd, received_ts, detail FROM deliveries "
                    "ORDER BY received_ts DESC LIMIT ?",
                (max(1, int(limit)),))
            for row in data["deliveries"]:
                row["state"] = review_state(
                    row.get("decision") or "",
                    _original_decision(row))
                row["head_short"] = (row.get("head_sha") or "")[:12]
        except Exception:
            data["deliveries"] = []
        try:
            data["failures"] = _rows(
                cx, "SELECT repo, pr, kind, pipeline, status, "
                    "terra_findings, luna_added, cost_usd, cost_unknown, "
                    "run_dir, finished_ts FROM reviews "
                    "WHERE status NOT IN ('complete','inconclusive') "
                    "ORDER BY finished_ts DESC LIMIT ?",
                (max(1, int(limit)),))
        except Exception:
            data["failures"] = []
        data["budget_refusals"] = [
            d for d in data["deliveries"]
            if d.get("state") == "budget_blocked"][:int(limit)]
        try:
            pubs = _rows(cx, "SELECT repo, pr, head_sha, run_id, "
                              "comment_id, outcome, reason, created_ts "
                              "FROM publications ORDER BY created_ts DESC "
                              "LIMIT ?", (max(1, int(limit)),))
        except Exception:
            pubs = []
        data["publication_failures"] = [
            p for p in pubs if str(p.get("outcome") or "") != "posted"]
        data["publications_posted"] = sum(
            1 for p in pubs if str(p.get("outcome") or "") == "posted")
        data["budget_retries"] = _budget_retry_notes(data["deliveries"])
    finally:
        close_ro(cx)
    return data


def _budget_retry_notes(deliveries: list[dict]) -> list[dict]:
    out = []
    for row in deliveries:
        det = _parse_detail(row.get("detail"))
        if "budget_retry_attempts" not in det:
            continue
        out.append({"repo": row.get("repo"), "pr": row.get("pr"),
                    "delivery_id": row.get("delivery_id"),
                    "decision": row.get("decision"),
                    "attempts": det.get("budget_retry_attempts"),
                    "from": det.get("budget_retry_from_decision")})
    return out
