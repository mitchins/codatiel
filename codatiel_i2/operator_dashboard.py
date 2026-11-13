"""Minimal operator dashboard (PROD-01 Track B).

Stdlib-only HTTP service (no framework, no new dependencies, no new
analytics DB, no telemetry). Server-rendered HTML with two views:

* ``/`` — status: service/poll health, spend vs Codatiel limits, PRs
  reviewed, digest-email outcomes, deferred/skipped/failed grouped by
  reason, recent activity with GitHub links.
* ``/settings`` — every ``operator.json`` control with validation
  errors shown and the audit trail visible.

Spending figures are derived from the Codatiel ledger ONLY (the env
``events.sqlite`` spend table plus per-review recorded costs) and are
labelled as such everywhere: they are never presented as total OpenAI
spend. The upstream OpenAI project limit is an operator-entered,
display-only field with a billing link; it is never fetched, never
enforced, never conflated with the ledger.

Secrets: this module never reads ``*_KEY``/``*_TOKEN``/``*_SECRET``
values. When env config is shown, secret-sounding keys display presence
only (set/unset). Secrets stay in env files.

Binds 127.0.0.1 only (SSH tunnel access); non-loopback hosts are
refused. Port configurable (default 8487).
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from codatiel_i2 import event_store as store_mod
from codatiel_i2 import operator_config as opcfg

DEFAULT_PORT = 8487
DEFAULT_HOST = "127.0.0.1"

GITHUB_URL = "https://github.com/{repo}/pull/{pr}"

LEDGER_LABEL = ("Codatiel ledger only — provider-confirmed usage "
                "recorded locally; NOT total OpenAI spend.")


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# -- data collection (pure queries; HTTP-free and unit-testable) ----------


def _open_store(state_dir: Path) -> store_mod.Store | None:
    db = Path(state_dir) / "events.sqlite"
    if not db.exists():
        return None
    try:
        return store_mod.Store(db)
    except store_mod.StoreError:
        return None


def _read_poll(state_dir: Path) -> dict[str, Any]:
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
    ok = ("END ok" in last_end) if last_end else None
    return {"last_successful_poll": last or None,
            "last_end_line": last_end or None,
            "last_end_ok": ok,
            "log_tail": tail}


def _period_spend(store: store_mod.Store) -> dict[str, Any]:
    try:
        day_rows = store.cx.execute(
            "SELECT date(finished_ts, 'unixepoch'), "
            "SUM(CASE WHEN cost_unknown=0 THEN COALESCE(cost_usd,0) "
            "ELSE 0 END), COUNT(*), COALESCE(SUM(cost_unknown),0) "
            "FROM reviews GROUP BY 1 ORDER BY 1 DESC LIMIT 62"
        ).fetchall()
        month_rows = store.cx.execute(
            "SELECT strftime('%Y-%m', finished_ts, 'unixepoch'), "
            "SUM(CASE WHEN cost_unknown=0 THEN COALESCE(cost_usd,0) "
            "ELSE 0 END), COUNT(*) "
            "FROM reviews GROUP BY 1 ORDER BY 1 DESC LIMIT 13"
        ).fetchall()
        repo_rows = store.cx.execute(
            "SELECT repo, COUNT(*), "
            "SUM(CASE WHEN cost_unknown=0 THEN COALESCE(cost_usd,0) "
            "ELSE 0 END), COALESCE(SUM(cost_unknown),0) "
            "FROM reviews GROUP BY repo ORDER BY repo"
        ).fetchall()
    except Exception as exc:
        raise opcfg.OperatorConfigError(
            f"spend query failed: {exc}") from exc
    daily = [{"day": d, "codatiel_ledger_usd": round(float(s or 0.0), 6),
              "reviews": int(n or 0), "unknown_cost_reviews": int(u or 0)}
             for d, s, n, u in day_rows]
    monthly = [{"month": m, "codatiel_ledger_usd": round(float(s or 0.0), 6),
                "reviews": int(n or 0)} for m, s, n in month_rows]
    by_repo = [{"repo": r, "reviews": int(n or 0),
                "codatiel_ledger_usd": round(float(s or 0.0), 6),
                "unknown_cost_reviews": int(u or 0)}
               for r, n, s, u in repo_rows]
    return {"daily": daily, "monthly": monthly, "by_repo": by_repo,
            "today": daily[0] if daily else None,
            "this_month": monthly[0] if monthly else None}


def _model_of_run(run_dir: str) -> str:
    try:
        metrics = json.loads(
            (Path(run_dir) / "metrics.json").read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    model = metrics.get("model")
    return str(model) if model else "unknown"


def _spend_by_model(reviews: list[dict]) -> list[dict]:
    agg: dict[str, dict[str, Any]] = {}
    for row in reviews[:5000]:
        model = _model_of_run(str(row.get("run_dir") or ""))
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


def _email_outcomes(reviews: list[dict]) -> dict[str, Any]:
    counts = {"delivered": 0, "failed": 0, "attempted-no-receipt": 0,
              "no-delivery-record": 0}
    for row in reviews[:5000]:
        run_dir = str(row.get("run_dir") or "")
        if not run_dir:
            counts["no-delivery-record"] += 1
            continue
        try:
            record = json.loads(
                (Path(run_dir) / "delivery.json").read_text(
                    encoding="utf-8"))
        except Exception:
            counts["no-delivery-record"] += 1
            continue
        outcome = _classify_receipt(record.get("transport_receipt"))
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"by_outcome": counts,
            "note": "Shadow digest emails only — this pipeline never "
                    "posts PR comments."}


def _publications(state_dir: Path) -> dict[str, Any]:
    json_store = Path(state_dir) / "publications.json"
    if json_store.exists():
        try:
            doc = json.loads(json_store.read_text(encoding="utf-8"))
            items = doc if isinstance(doc, list) else doc.get("items", [])
            return {"configured": True, "published_total": len(items),
                    "source": "publications.json"}
        except Exception as exc:
            return {"configured": True, "published_total": None,
                    "source": "publications.json",
                    "error": f"unreadable: {exc}"}
    sqlite_store = Path(state_dir) / "publications.sqlite"
    if sqlite_store.exists():
        import sqlite3
        try:
            cx = sqlite3.connect(f"file:{sqlite_store}?mode=ro", uri=True)
            try:
                (n,) = cx.execute(
                    "SELECT COUNT(*) FROM publications").fetchone()
            finally:
                cx.close()
            return {"configured": True, "published_total": int(n),
                    "source": "publications.sqlite"}
        except Exception as exc:
            return {"configured": True, "published_total": None,
                    "source": "publications.sqlite",
                    "error": f"unreadable: {exc}"}
    return {"configured": False, "published_total": 0,
            "source": None,
            "note": "No publications store present (tolerated). The "
                    "event pipeline is shadow-only: digest emails, "
                    "never GitHub PR comments."}


def _recent(store: store_mod.Store, limit: int = 20) -> dict[str, Any]:
    try:
        drows = store.cx.execute(
            "SELECT delivery_id, received_ts, event_type, action, repo,"
            " pr, head_sha, decision, reason, cost_usd FROM deliveries "
            "ORDER BY received_ts DESC LIMIT ?", (limit,)).fetchall()
        rrows = store.cx.execute(
            "SELECT repo, pr, kind, pipeline, status, terra_findings,"
            " luna_added, cost_usd, cost_unknown, run_dir, finished_ts"
            " FROM reviews ORDER BY finished_ts DESC LIMIT ?",
            (limit,)).fetchall()
    except Exception as exc:
        raise opcfg.OperatorConfigError(
            f"recent-activity query failed: {exc}") from exc
    deliveries = [{
        "delivery_id": d, "received_ts": ts, "event": e, "action": a,
        "repo": r, "pr": p, "head_sha": (h or "")[:12],
        "decision": dec, "reason": reason, "cost_usd": c}
        for d, ts, e, a, r, p, h, dec, reason, c in drows]
    reviews = []
    for r, p, kind, pipe, status, terra, luna, c, unk, rd, ts in rrows:
        reviews.append({
            "repo": r, "pr": p, "kind": kind, "pipeline": pipe,
            "status": status, "terra_findings": terra,
            "luna_added": luna, "cost_usd": c,
            "cost_unknown": bool(unk), "run_dir": rd,
            "finished_ts": ts,
            "github_url": GITHUB_URL.format(repo=r, pr=p)})
    return {"deliveries": deliveries, "reviews": reviews}


def _env_snapshot() -> dict[str, Any]:
    """Non-secret env config: values shown, except secret-sounding keys.

    Secret-sounding keys report presence only and their values are
    never read.
    """
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
    for name in names:
        present = bool(os.environ.get(name, ""))
        if opcfg.secret_like(name):
            out[name] = {"value": None,
                         "display": opcfg.mask_value(name, present)}
        else:
            out[name] = {"value": os.environ.get(name, "") if present
                         else None,
                         "display": os.environ.get(name, "") if present
                         else "unset"}
    return out


def collect_overview(state_dir: Path | str) -> dict[str, Any]:
    """Gather every overview panel. Read-only; tolerates absence."""
    state_dir = Path(state_dir)
    settings, source = opcfg.load(state_dir)
    data: dict[str, Any] = {
        "state_dir": str(state_dir),
        "operator_source": source,
        "settings": settings.to_dict(),
        "spend_label": LEDGER_LABEL,
        "store_present": (state_dir / "events.sqlite").exists(),
    }
    data["poll"] = _read_poll(state_dir)
    data["publications"] = _publications(state_dir)
    store = _open_store(state_dir)
    if store is None:
        data.update({
            "enabled": False, "enabled_at": None, "pipeline": None,
            "repos": [], "ledger": None, "period": None,
            "by_model": [], "reviews": [], "decisions": {},
            "email": {"by_outcome": {}, "note": "no state store"},
            "recent": {"deliveries": [], "reviews": []},
            "error": ("no readable events.sqlite in state dir; "
                      "worker may not be enabled yet"),
        })
        data["audit"] = opcfg.audit_trail(state_dir)
        data["env"] = _env_snapshot()
        return data
    try:
        data["enabled_at"] = store.meta_get("enabled_at")
        data["pipeline"] = store.meta_get("pipeline")
        try:
            data["repos"] = json.loads(store.meta_get("repos") or "[]")
        except Exception:
            data["repos"] = []
        data["enabled"] = bool(data["enabled_at"])
        data["ledger"] = store.spend_totals()
        data["period"] = _period_spend(store)
        reviews = store.reviews_list()
        data["by_model"] = _spend_by_model(reviews)
        data["decisions"] = store.deliveries_by_decision()
        done = [r for r in reviews
                if r.get("status") in ("complete", "inconclusive")]
        failed = [r for r in reviews if r not in done]
        data["reviews"] = {
            "completed": len(done),
            "failed": len(failed),
            "prs_reviewed": len({f"{r['repo']}#{r['pr']}" for r in done}),
            "terra_findings_total": sum(
                int(r.get("terra_findings") or 0) for r in done),
            "luna_exclusive_total": sum(
                int(r.get("luna_added") or 0) for r in done),
        }
        data["email"] = _email_outcomes(reviews)
        data["recent"] = _recent(store)
    finally:
        store.close()
    # Effective limits: min() over env + file where the env config is
    # available; otherwise show the file values plainly.
    try:
        from codatiel_i2 import event_pipeline as pipe_mod
        cfg = pipe_mod.config_from_env()
        cfg = opcfg.apply_to_event_config(cfg)
        data["effective"] = {
            "available": True,
            "monthly_effective_usd": cfg.spend_cap_usd,
            "daily_effective_usd": cfg.daily_cap_usd,
            "per_pr_effective_usd": (cfg.whole_pr_ceiling_usd
                                     if cfg.env == "dev" else None),
            "pipeline": cfg.pipeline,
            "pause_reviews": settings.pause_reviews if source == "file"
            else False,
            "repos": cfg.repos,
            "operator_source": cfg.operator_source,
        }
    except Exception as exc:
        data["effective"] = {
            "available": False,
            "note": f"live worker config unavailable: {exc}",
        }
    data["audit"] = opcfg.audit_trail(state_dir)
    data["env"] = _env_snapshot()
    return data


# -- HTML rendering ----------------------------------------------------------


def layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{esc(title)} — Codatiel operator</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:72rem;margin:2rem auto;
padding:0 1rem;color:#111}}
nav a{{margin-right:1rem}}table{{border-collapse:collapse;margin:1rem 0}}
th,td{{border:1px solid #999;padding:.3rem .6rem;text-align:left}}
.warn{{background:#fff3cd;padding:.5rem}} .err{{background:#f8d7da;padding:.5rem}}
.ok{{background:#d4edda;padding:.5rem}} pre{{background:#f4f4f4;padding:.5rem;
overflow:auto}} .note{{color:#555;font-size:.9em}} form label{{display:block;
margin:.4rem 0}} input[type=text]{{width:30rem;max-width:100%}}
textarea{{width:30rem;max-width:100%}} footer{{margin-top:2rem;color:#555;
font-size:.85em}}
</style></head><body>
<nav><a href="/">Overview</a><a href="/settings">Settings</a></nav>
<h1>{esc(title)}</h1>
{body}
<footer>Codatiel operator dashboard — loopback only. Spend figures are
{esc(LEDGER_LABEL)}</footer>
</body></html>"""


def _kv(rows: list[tuple[str, Any]]) -> str:
    cells = "".join(
        f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows)
    return f"<table>{cells}</table>"


def render_overview(d: dict[str, Any]) -> str:
    parts: list[str] = []
    if d.get("error"):
        parts.append(f'<p class="warn">{esc(d["error"])}</p>')
    poll = d.get("poll", {})
    parts.append("<h2>Service status</h2>")
    parts.append(_kv([
        ("State dir", d.get("state_dir")),
        ("Worker enabled", "yes" if d.get("enabled") else "no"),
        ("Enabled at", d.get("enabled_at") or "—"),
        ("Pipeline", d.get("pipeline") or "—"),
        ("Operator settings", d.get("operator_source")),
        ("Last successful poll", poll.get("last_successful_poll") or "—"),
        ("Last poll cycle", poll.get("last_end_line") or "—"),
    ]))
    if poll.get("log_tail"):
        parts.append("<h3>Poll log (tail)</h3><pre>"
                     + esc("\n".join(poll["log_tail"])) + "</pre>")
    eff = d.get("effective", {})
    parts.append("<h2>Spend vs Codatiel limits</h2>")
    parts.append(f'<p class="note">{esc(d.get("spend_label", ""))}</p>')
    ledger = d.get("ledger") or {}
    if eff.get("available"):
        parts.append(_kv([
            ("Monthly cap (effective)", eff.get("monthly_effective_usd")),
            ("Daily cap (effective)", eff.get("daily_effective_usd")
             if eff.get("daily_effective_usd") is not None else "unset"),
            ("Per-PR breaker (effective)",
             eff.get("per_pr_effective_usd")
             if eff.get("per_pr_effective_usd") is not None else "—"),
            ("Ledger settled USD", ledger.get("settled_usd")),
            ("Ledger reserved USD", ledger.get("reserved_usd")),
            ("Unknown-cost holds", ledger.get("unknown_holds")),
        ]))
    else:
        parts.append(_kv([
            ("Ledger settled USD", ledger.get("settled_usd")),
            ("Ledger reserved USD", ledger.get("reserved_usd")),
            ("Unknown-cost holds", ledger.get("unknown_holds")),
            ("Effective caps", eff.get("note", "unavailable")),
        ]))
    settings = d.get("settings", {})
    parts.append(
        "<p>Upstream OpenAI project limit (operator-entered, "
        "DISPLAY ONLY — never fetched, never enforced, never part of "
        "the Codatiel ledger): "
        f"<strong>{esc(settings.get('openai_project_limit_usd'))}</strong> "
        f'USD — <a href="{esc(settings.get("openai_billing_url"))}">'
        "billing/limits</a></p>")
    period = d.get("period") or {}
    if period.get("today"):
        parts.append("<h3>Today / this month (Codatiel ledger)</h3>")
        parts.append(_kv([
            ("Today — ledger USD", period["today"]["codatiel_ledger_usd"]),
            ("Today — reviews", period["today"]["reviews"]),
            ("This month — ledger USD",
             (period.get("this_month") or {}).get("codatiel_ledger_usd")),
            ("This month — reviews",
             (period.get("this_month") or {}).get("reviews")),
        ]))
    if d.get("by_model"):
        rows = "".join(
            f"<tr><td>{esc(m['model'])}</td><td>{m['reviews']}</td>"
            f"<td>{m['codatiel_ledger_usd']}</td>"
            f"<td>{m['unknown_cost_reviews']}</td></tr>"
            for m in d["by_model"])
        parts.append("<h3>Spend by model (Codatiel ledger)</h3>"
                     "<table><tr><th>Model</th><th>Reviews</th>"
                     "<th>Ledger USD</th><th>Unknown-cost</th></tr>"
                     + rows + "</table>")
    if (period.get("by_repo")):
        rows = "".join(
            f"<tr><td>{esc(r['repo'])}</td><td>{r['reviews']}</td>"
            f"<td>{r['codatiel_ledger_usd']}</td>"
            f"<td>{r['unknown_cost_reviews']}</td></tr>"
            for r in period["by_repo"])
        parts.append("<h3>Spend by repo (Codatiel ledger)</h3>"
                     "<table><tr><th>Repo</th><th>Reviews</th>"
                     "<th>Ledger USD</th><th>Unknown-cost</th></tr>"
                     + rows + "</table>")
    rev = d.get("reviews", {})
    pubs = d.get("publications", {})
    parts.append("<h2>Reviews &amp; publications</h2>")
    parts.append(_kv([
        ("PRs reviewed", rev.get("prs_reviewed")),
        ("Reviews completed", rev.get("completed")),
        ("Reviews failed", rev.get("failed")),
        ("Terra findings total", rev.get("terra_findings_total")),
        ("Luna-exclusive total", rev.get("luna_exclusive_total")),
        ("GitHub comments published",
         pubs.get("published_total") if pubs.get("configured")
         else "0 (shadow-only — pipeline never posts PR comments)"),
        ("Publications store",
         pubs.get("source") or pubs.get("note", "absent (tolerated)")),
    ]))
    email = d.get("email", {})
    parts.append("<h2>Digest email outcomes</h2>")
    parts.append(f'<p class="note">{esc(email.get("note", ""))}</p>')
    parts.append(_kv(sorted((email.get("by_outcome") or {}).items())))
    parts.append("<h2>Deferred / skipped / failed by reason</h2>")
    decisions = d.get("decisions") or {}
    if decisions:
        parts.append(_kv(sorted(decisions.items())))
    else:
        parts.append("<p>No delivery decisions recorded.</p>")
    recent = d.get("recent", {})
    if recent.get("reviews"):
        rows = "".join(
            f"<tr><td><a href=\"{esc(r['github_url'])}\">"
            f"{esc(r['repo'])}#{esc(r['pr'])}</a></td>"
            f"<td>{esc(r['status'])}</td><td>{esc(r['kind'])}</td>"
            f"<td>{esc(r['cost_usd'])}</td></tr>"
            for r in recent["reviews"])
        parts.append("<h2>Recent activity</h2>"
                     "<table><tr><th>PR</th><th>Status</th><th>Kind</th>"
                     "<th>Cost USD</th></tr>" + rows + "</table>")
    if recent.get("deliveries"):
        rows = "".join(
            f"<tr><td>{esc(str(x['delivery_id'])[:24])}</td>"
            + f"<td>{esc(x['repo'])}#{esc(x['pr'])}</td>"
            f"<td>{esc(x['decision'])}</td>"
            f"<td>{esc(str(x['reason'])[:120])}</td></tr>"
            for x in recent["deliveries"])
        parts.append("<h3>Recent deliveries</h3>"
                     "<table><tr><th>Delivery</th><th>PR</th>"
                     "<th>Decision</th><th>Reason</th></tr>"
                     + rows + "</table>")
    return layout("Overview", "\n".join(parts))


def _settings_form_values(settings: dict[str, Any],
                          env: dict[str, Any]) -> dict[str, str]:
    def num(key: str) -> str:
        value = settings.get(key)
        return "" if value is None else str(value)

    repos = settings.get("repos") or {}
    if not repos:
        env_repos = (env.get("CODATIEL_DEV_REPOS", {}).get("value") or ""
                     + "," + (env.get("CODATIEL_TEST_REPOS", {})
                              .get("value") or ""))
        lines = [r.strip() for r in env_repos.split(",") if r.strip()]
    else:
        lines = [f"{slug}=off" if not enabled else slug
                 for slug, enabled in sorted(repos.items())]
    thresh = settings.get("alert_thresholds") or {}
    return {
        "monthly_budget_usd": num("monthly_budget_usd"),
        "daily_budget_usd": num("daily_budget_usd"),
        "per_pr_breaker_usd": num("per_pr_breaker_usd"),
        "warn_frac": str(thresh.get("warn_frac", 0.8)),
        "critical_frac": str(thresh.get("critical_frac", 0.95)),
        "notify_to": ", ".join(settings.get("notify_to") or []),
        "notify_sender": settings.get("notify_sender") or "",
        "model_profile": settings.get("model_profile") or "",
        "repos": "\n".join(lines),
        "openai_project_limit_usd": num("openai_project_limit_usd"),
        "openai_billing_url": (
            settings.get("openai_billing_url") or opcfg.DEFAULT_BILLING_URL),
    }


def render_settings(d: dict[str, Any],
                    errors: list[str] | None = None,
                    saved: bool = False) -> str:
    settings = d.get("settings", {})
    env = d.get("env", {})
    vals = _settings_form_values(settings, env)
    parts: list[str] = []
    if saved:
        parts.append('<p class="ok">Settings saved and audit-logged.</p>')
    for err in errors or []:
        parts.append(f'<p class="err">{esc(err)}</p>')
    parts.append(
        f"<p>Source: <strong>{esc(d.get('operator_source'))}</strong> "
        "(absent = worker uses env configuration only).</p>")

    def check(name: str, on: bool) -> str:
        return (f'<label><input type="checkbox" name="{name}"'
                + (" checked" if on else "") + f"> {esc(name)}</label>")

    profile_opts = ['<option value="">env default</option>']
    for choice in opcfg.MODEL_PROFILES:
        selected = " selected" if vals["model_profile"] == choice else ""
        profile_opts.append(
            f'<option value="{choice}"{selected}>{choice}</option>')
    parts.append(f"""<form method="post" action="/settings">
<label>monthly_budget_usd (USD; empty = env default)
<input type="text" name="monthly_budget_usd" value="{esc(vals['monthly_budget_usd'])}"></label>
<label>daily_budget_usd (USD; empty = no daily budget)
<input type="text" name="daily_budget_usd" value="{esc(vals['daily_budget_usd'])}"></label>
<label>per_pr_breaker_usd (USD; empty = env default)
<input type="text" name="per_pr_breaker_usd" value="{esc(vals['per_pr_breaker_usd'])}"></label>
<label>alert warn_frac (0, 1]
<input type="text" name="warn_frac" value="{esc(vals['warn_frac'])}"></label>
<label>alert critical_frac (0, 1], above warn
<input type="text" name="critical_frac" value="{esc(vals['critical_frac'])}"></label>
{check("pause_reviews", bool(settings.get("pause_reviews")))}
{check("pause_github_comments", bool(settings.get("pause_github_comments", True)))}
{check("email_enabled", bool(settings.get("email_enabled", True)))}
{check("email_on_complete", bool(settings.get("email_on_complete", True)))}
<label>notify_to (comma-separated; empty = env recipients)
<input type="text" name="notify_to" value="{esc(vals['notify_to'])}"></label>
<label>notify_sender (empty = env sender)
<input type="text" name="notify_sender" value="{esc(vals['notify_sender'])}"></label>
<label>model_profile (fixed choices only — cannot bypass spend/review contracts)
<select name="model_profile">{"".join(profile_opts)}</select></label>
<label>repos — one <code>owner/repo</code> per line; append <code>=off</code>
to disable a repo without removing it (empty = no file constraint, env allowlist stands)
<textarea name="repos" rows="6">{esc(vals['repos'])}</textarea></label>
<label>openai_project_limit_usd (DISPLAY ONLY — never fetched, never enforced)
<input type="text" name="openai_project_limit_usd" value="{esc(vals['openai_project_limit_usd'])}"></label>
<label>openai_billing_url
<input type="text" name="openai_billing_url" value="{esc(vals['openai_billing_url'])}"></label>
<label>actor (audit identity)
<input type="text" name="actor" value="dashboard"></label>
<input type="submit" value="Save settings">
</form>""")
    parts.append("<h2>Effective resolution (env vs file → effective)</h2>")
    eff = d.get("effective", {})
    if eff.get("available"):
        parts.append(_kv([
            ("Monthly cap effective USD",
             eff.get("monthly_effective_usd")),
            ("Daily cap effective USD",
             eff.get("daily_effective_usd")),
            ("Per-PR breaker effective USD",
             eff.get("per_pr_effective_usd")),
            ("Pipeline", eff.get("pipeline")),
            ("Pause reviews", eff.get("pause_reviews")),
            ("Effective repos", ", ".join(eff.get("repos") or [])),
        ]))
    else:
        parts.append(f'<p class="note">{esc(eff.get("note", ""))}</p>')
    parts.append("<h2>Env config (values shown; secrets never read)</h2>")
    parts.append(_kv([(k, v.get("display")) for k, v in sorted(env.items())]))
    parts.append("<h2>Audit trail (most recent first)</h2>")
    audit = d.get("audit") or []
    if audit:
        rows = "".join(
            f"<tr><td>{esc(a.get('ts'))}</td>"
            f"<td>{esc(a.get('actor'))}</td>"
            f"<td>{esc(a.get('action'))}</td>"
            f"<td><pre>{esc(json.dumps(a.get('changes', a.get('errors', '')), sort_keys=True))}</pre></td></tr>"
            for a in audit[:20])
        parts.append("<table><tr><th>Time</th><th>Actor</th><th>Action</th>"
                     "<th>Detail</th></tr>" + rows + "</table>")
    else:
        parts.append("<p>No audit entries yet.</p>")
    return layout("Settings", "\n".join(parts))


# -- form parsing ------------------------------------------------------------


def _opt_float(raw: str, key: str, errors: list[str]) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{key} must be a number, got {raw!r}")
        return None


def parse_settings_form(form: dict[str, list[str]]
                        ) -> tuple[dict, list[str], str]:
    """Map POST fields to an operator.json patch. Never raises."""
    errors: list[str] = []
    first = {k: (v[0] if v else "") for k, v in form.items()}
    patch: dict[str, Any] = {}
    for key in ("monthly_budget_usd", "daily_budget_usd",
                "per_pr_breaker_usd", "openai_project_limit_usd"):
        if key in first:
            patch[key] = _opt_float(first[key], key, errors)
    for key in ("warn_frac", "critical_frac"):
        if key in first:
            patch.setdefault("alert_thresholds", {})[key] = _opt_float(
                first[key], key, errors)
    for key in ("pause_reviews", "pause_github_comments",
                "email_enabled", "email_on_complete"):
        patch[key] = key in form
    if "notify_to" in first:
        patch["notify_to"] = [a.strip() for a in first["notify_to"].split(",")
                              if a.strip()]
    if "notify_sender" in first:
        patch["notify_sender"] = first["notify_sender"].strip()
    if "model_profile" in first:
        patch["model_profile"] = first["model_profile"].strip() or None
    if "openai_billing_url" in first:
        patch["openai_billing_url"] = first["openai_billing_url"].strip()
    if "repos" in first:
        repos: dict[str, bool] = {}
        for lineno, line in enumerate(first["repos"].splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                slug, _, flag = line.partition("=")
                slug, flag = slug.strip(), flag.strip().lower()
                if flag in ("off", "false", "0", "no", "disabled"):
                    repos[slug] = False
                elif flag in ("on", "true", "1", "yes", "enabled"):
                    repos[slug] = True
                else:
                    errors.append(
                        f"repos line {lineno}: {line!r} — flag must be "
                        "empty/=on or =off")
                    continue
            else:
                slug = line
                repos[slug] = True
            if not opcfg.REPO_RE.match(slug):
                errors.append(
                    f"repos line {lineno}: {slug!r} is not an "
                    "'owner/repo' slug")
        patch["repos"] = repos
    actor = (first.get("actor") or "dashboard").strip() or "dashboard"
    return patch, errors, actor


# -- HTTP service --------------------------------------------------------------


def ensure_loopback(host: str) -> str:
    """Refuse non-loopback bind addresses (SSH tunnel access only)."""
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise opcfg.OperatorConfigError(
            f"dashboard host {host!r} does not resolve: {exc}") from exc
    addrs = [info[4][0] for info in infos]
    if not addrs or not all(
            _is_loopback_addr(a) for a in addrs):
        raise opcfg.OperatorConfigError(
            f"dashboard refuses non-loopback host {host!r} "
            f"(resolves to {addrs}); bind 127.0.0.1 only")
    return host


def _is_loopback_addr(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


class OperatorHandler(BaseHTTPRequestHandler):
    server_version = "CodatielOperator/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, code: int, body: str,
              headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        state_dir = Path(self.server.state_dir)  # type: ignore[attr-defined]
        if parsed.path == "/":
            try:
                data = collect_overview(state_dir)
            except opcfg.OperatorConfigError as exc:
                self._send(500, layout(
                    "Overview unavailable",
                    f'<p class="err">{esc(exc)}</p>'))
                return
            self._send(200, render_overview(data))
        elif parsed.path == "/settings":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                data = collect_overview(state_dir)
            except opcfg.OperatorConfigError as exc:
                self._send(500, layout(
                    "Settings unavailable",
                    f'<p class="err">{esc(exc)}</p>'))
                return
            saved = query.get("saved") == ["1"]
            self._send(200, render_settings(data, saved=saved))
        else:
            self._send(404, layout("Not found",
                                   "<p>Unknown path. "
                                   '<a href="/">Overview</a></p>'))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/settings":
            self._send(404, layout("Not found", "<p>Unknown path.</p>"))
            return
        state_dir = Path(self.server.state_dir)  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(max(0, length)).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)
        patch, form_errors, actor = parse_settings_form(form)
        if form_errors:
            try:
                data = collect_overview(state_dir)
            except opcfg.OperatorConfigError as exc:
                self._send(500, layout(
                    "Settings unavailable",
                    f'<p class="err">{esc(exc)}</p>'))
                return
            self._send(400, render_settings(data, errors=form_errors))
            return
        try:
            known = None
            try:
                from codatiel_i2 import event_pipeline as pipe_mod
                known = pipe_mod.config_from_env().repos
            except Exception:
                known = None
            opcfg.save(state_dir, patch, actor=actor,
                       known_repos=known)
        except opcfg.OperatorConfigError as exc:
            try:
                data = collect_overview(state_dir)
            except opcfg.OperatorConfigError as exc2:
                self._send(500, layout(
                    "Settings unavailable",
                    f'<p class="err">{esc(exc2)}</p>'))
                return
            self._send(400, render_settings(data, errors=[str(exc)]))
            return
        self._send(303, "", headers={"Location": "/settings?saved=1"})


def build_server(state_dir: Path | str, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    ensure_loopback(host)
    server = ThreadingHTTPServer((host, port), OperatorHandler)
    server.state_dir = str(state_dir)  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def serve(state_dir: Path | str, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> None:
    server = build_server(state_dir, host, port)
    addr = server.server_address
    print(f"Codatiel operator dashboard on http://{addr[0]}:{addr[1]}/ "
          f"(state: {state_dir}; loopback only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
