"""DASH-01 operator dashboard (FastAPI migration).

Replaces the ``http.server`` prototype (``operator_dashboard.py``)
with FastAPI + Uvicorn + Jinja2. The dashboard is read-only except
for the audited operator-settings form, binds loopback only, opens
the production database read-only, and never triggers inference,
comments, or poller changes.

FastAPI is reserved as the future HTTP ingress framework; no webhook
receiver is implemented or exposed here (separate mission).
"""

from __future__ import annotations

import hmac
import json
import secrets
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.trustedhost import TrustedHostMiddleware

from codatiel_i2 import dashboard_data as DD
from codatiel_i2 import operator_config as opcfg

DEFAULT_PORT = 8487
DEFAULT_HOST = "127.0.0.1"

TEMPLATES_DIR = Path(__file__).resolve().parent / "dashboard_templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def ensure_loopback(host: str) -> str:
    """Refuse non-loopback bind addresses (SSH tunnel access only)."""
    import ipaddress
    import socket
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
    if not addrs or not all(_is_loopback_addr(a) for a in addrs):
        raise opcfg.OperatorConfigError(
            f"dashboard refuses non-loopback host {host!r} "
            f"(resolves to {addrs}); bind 127.0.0.1 only")
    return host


def _is_loopback_addr(addr: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


# -- settings form (moved from the prototype; validation unchanged) ------


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


def _mint_csrf(app: FastAPI) -> str:
    token = secrets.token_urlsafe(32)
    tokens = app.state.csrf_tokens
    tokens.add(token)
    while len(tokens) > 64:
        tokens.pop()
    return token


def _check_csrf(app: FastAPI, token: str) -> bool:
    for known in list(app.state.csrf_tokens):
        if hmac.compare_digest(known, token or ""):
            return True
    return False


_CHECKBOXES = ("pause_reviews", "pause_github_comments",
               "email_enabled", "email_on_complete")
_CHECKBOX_DEFAULTS = {"pause_reviews": False,
                      "pause_github_comments": True,
                      "email_enabled": True, "email_on_complete": True}


def _saved_checked(settings: dict[str, Any]) -> dict[str, bool]:
    return {name: bool(settings.get(name, _CHECKBOX_DEFAULTS[name]))
            for name in _CHECKBOXES}


def _submitted_vals(form: dict[str, list[str]]) -> dict[str, str]:
    """Error context: echo what the operator submitted, not saved data."""
    first = {k: (v[0] if v else "") for k, v in form.items()}
    vals = {k: first.get(k, "") for k in (
        "monthly_budget_usd", "daily_budget_usd", "per_pr_breaker_usd",
        "warn_frac", "critical_frac", "notify_to", "notify_sender",
        "model_profile", "openai_project_limit_usd", "openai_billing_url",
        "repos", "actor")}
    return vals


def _submitted_checked(form: dict[str, list[str]]) -> dict[str, bool]:
    return {name: (name in form) for name in _CHECKBOXES}


def _settings_form_values(settings: dict[str, Any],
                          env: dict[str, Any]) -> dict[str, str]:
    def num(key: str) -> str:
        value = settings.get(key)
        return "" if value is None else str(value)

    repos = settings.get("repos") or {}
    if not repos:
        dev_repos = env.get("CODATIEL_DEV_REPOS", {}).get("value") or ""
        test_repos = env.get("CODATIEL_TEST_REPOS", {}).get("value") or ""
        lines = [r.strip() for r in (dev_repos + "," + test_repos).split(",")
                 if r.strip()]
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


# -- application factory -------------------------------------------------------


def create_app(state_dir: Path | str) -> FastAPI:
    """Build the dashboard app bound to one state dir (read-only)."""
    state = Path(state_dir)
    app = FastAPI(title="Codatiel operator dashboard",
                  docs_url=None, redoc_url=None, openapi_url=None)
    # Host-header protection: the loopback bind alone does not stop a
    # rebinding hostname. Requests with any other Host are rejected
    # before any route runs. Extend allowed_hosts only for other
    # loopback names or addresses the deployment binds.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"])
    app.state.state_dir = state
    app.state.csrf_tokens: set[str] = set()

    def render(name: str, status_code: int = 200,
               **ctx: Any) -> HTMLResponse:
        ctx.setdefault("nav", [
            ("Overview", "/"), ("Repositories", "/repos"),
            ("Spend", "/spend"), ("Efficiency", "/efficiency"),
            ("Ops", "/ops"), ("Settings", "/settings")])
        ctx.setdefault("ledger_label", DD.LEDGER_LABEL)
        return HTMLResponse(_env.get_template(name).render(**ctx),
                            status_code=status_code)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        poll = DD.read_poll(state)
        return JSONResponse({
            "ok": True, "state_dir": str(state),
            "store_present": (state / "events.sqlite").exists(),
            "poller": DD.poll_status(poll).get("state")})

    @app.get("/")
    def overview() -> HTMLResponse:
        data = DD.collect_overview(state)
        return render("overview.html", title="Overview",
                      d=data, refresh=120)

    @app.get("/repos")
    def repos(request: Request) -> HTMLResponse:
        q = dict(request.query_params)
        data = DD.collect_repos(state)
        filt = (q.get("state") or "").strip()
        if filt and filt in DD.REVIEW_STATES:
            for repo in data.get("repos", []):
                repo["queue_by_state"] = {
                    k: v for k, v in
                    (repo.get("queue_by_state") or {}).items()
                    if k == filt}
        return render("repos.html", title="Repositories",
                      d=data, state_filter=filt,
                      states=list(DD.REVIEW_STATES))

    @app.get("/pr/{repo:path}/{pr}")
    def pr_detail(repo: str, pr: str) -> HTMLResponse:
        try:
            number = int(str(pr).strip())
        except ValueError:
            return render("error.html", 404, title="Not found",
                          message=f"invalid PR number {pr!r}")
        data = DD.collect_pr(state, repo, number)
        if not data.get("found"):
            return render("error.html", 404, title="PR not found",
                          message=f"no records for {repo}#{number}")
        return render("pr.html", title=f"{repo}#{number}", d=data)

    @app.get("/spend")
    def spend(request: Request) -> HTMLResponse:
        q = dict(request.query_params)
        try:
            days = max(1, min(365, int(q.get("days") or 30)))
        except ValueError:
            days = 30
        data = DD.collect_spend(state, days=days)
        return render("spend.html", title="Spend", d=data, days=days)

    @app.get("/efficiency")
    def efficiency() -> HTMLResponse:
        return render("efficiency.html", title="Efficiency",
                      d=DD.collect_efficiency(state))

    @app.get("/ops")
    def ops(request: Request) -> HTMLResponse:
        q = dict(request.query_params)
        try:
            limit = max(1, min(200, int(q.get("limit") or 50)))
        except ValueError:
            limit = 50
        filt = (q.get("state") or "").strip()
        data = DD.collect_ops(state, limit=limit)
        if filt and filt in DD.REVIEW_STATES:
            data["deliveries"] = [
                d for d in data.get("deliveries", [])
                if d.get("state") == filt]
        return render("ops.html", title="Operations", d=data,
                      limit=limit, state_filter=filt,
                      states=list(DD.REVIEW_STATES), refresh=120)

    @app.get("/settings")
    def settings_form(request: Request) -> HTMLResponse:
        q = dict(request.query_params)
        data = DD.collect_overview(state)
        token = _mint_csrf(app)
        return render("settings.html", title="Settings", d=data,
                      vals=_settings_form_values(
                          data.get("settings", {}),
                          data.get("env", {})),
                      checked=_saved_checked(data.get("settings", {})),
                      profiles=list(opcfg.MODEL_PROFILES),
                      csrf_token=token, errors=[],
                      saved=q.get("saved") == "1",
                      form_disabled=bool(data.get("config_error")))

    @app.post("/settings")
    async def settings_save(request: Request) -> HTMLResponse:
        body = (await request.body()).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(body, keep_blank_values=True)
        token = (form.get("csrf_token") or [""])[0]
        data = DD.collect_overview(state)
        disabled = bool(data.get("config_error"))

        def with_errors(errors: list[str], status: int) -> HTMLResponse:
            resp = render("settings.html", title="Settings", d=data,
                          vals=_submitted_vals(form),
                          checked=_submitted_checked(form),
                          profiles=list(opcfg.MODEL_PROFILES),
                          csrf_token=_mint_csrf(app), errors=errors,
                          saved=False, form_disabled=disabled)
            resp.status_code = status
            return resp

        if not _check_csrf(app, token):
            return with_errors(["invalid or expired form token; "
                                "reload /settings and retry"], 403)
        # Invalid operator configuration: diagnostics stay visible
        # but no save is attempted until the file is fixed.
        if disabled:
            return with_errors(
                ["operator configuration is invalid "
                 f"({data.get('config_error')}); fix operator.json "
                 "before saving"], 400)
        patch, form_errors, actor = parse_settings_form(form)
        if form_errors:
            return with_errors(form_errors, 400)
        try:
            try:
                from codatiel_i2 import event_pipeline as pipe_mod
                cfg = pipe_mod.config_from_env()
                cfg.state_dir = state
                known = cfg.repos
            except Exception:
                known = None
            opcfg.save(state, patch, actor=actor, known_repos=known)
        except opcfg.OperatorConfigError as exc:
            return with_errors([str(exc)], 400)
        return RedirectResponse("/settings?saved=1", status_code=303)

    # -- JSON API (same read-only collectors) ---------------------------------
    @app.get("/api/overview")
    def api_overview() -> JSONResponse:
        return JSONResponse(DD.collect_overview(state))

    @app.get("/api/repos")
    def api_repos() -> JSONResponse:
        return JSONResponse(DD.collect_repos(state))

    @app.get("/api/spend")
    def api_spend(request: Request) -> JSONResponse:
        q = dict(request.query_params)
        try:
            days = max(1, min(365, int(q.get("days") or 30)))
        except ValueError:
            days = 30
        return JSONResponse(DD.collect_spend(state, days=days))

    @app.get("/api/efficiency")
    def api_efficiency() -> JSONResponse:
        return JSONResponse(DD.collect_efficiency(state))

    @app.get("/api/ops")
    def api_ops() -> JSONResponse:
        return JSONResponse(DD.collect_ops(state))

    @app.get("/api/pr/{repo:path}/{pr}")
    def api_pr(repo: str, pr: str) -> JSONResponse:
        try:
            number = int(str(pr).strip())
        except ValueError:
            return JSONResponse({"error": "invalid PR number"},
                                status_code=404)
        return JSONResponse(DD.collect_pr(state, repo, number))

    return app


# -- service ---------------------------------------------------------------------


def serve(state_dir: Path | str, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> None:
    """Serve the dashboard (loopback only) until interrupted.

    Binds first, then prints the machine-readable status record with
    the allocated port — so --port 0 reports the real ephemeral port
    instead of 0.
    """
    import threading
    import time

    import uvicorn
    ensure_loopback(host)
    app = create_app(state_dir)
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    actual = port
    try:
        actual = server.servers[0].sockets[0].getsockname()[1]
    except Exception:
        pass
    print(json.dumps({"status": "ok",
                      "url": f"http://{host}:{actual}/",
                      "state_dir": str(state_dir)},
                     ensure_ascii=False, sort_keys=True), flush=True)
    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(timeout=10)
