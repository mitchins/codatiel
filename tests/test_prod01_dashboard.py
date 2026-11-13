"""PROD-01 Track B: operator settings layer + minimal dashboard.

No paid inference, no network, no live state: every test builds its own
tmp state dir (fresh SQLite via event_store) and its own run dirs.
Covers: validation, precedence, audit, persistence across a simulated
restart, spend-query accuracy against a fixture DB, loopback binding,
and the CLI wiring. Never touches ops secrets, cron, or live state.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from codatiel_i2 import event_store as S
from codatiel_i2 import operator_config as OC
from codatiel_i2 import operator_dashboard as OD

REPO_A = "o/r"
REPO_B = "o/s"


# -- fixtures ---------------------------------------------------------------


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
         cap: str = "20") -> Path:
    state = tmp_path / "estate"
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(state))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", f"{REPO_A},{REPO_B}")
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", cap)
    monkeypatch.setenv("CODATIEL_TEST_TO", "test-ops@example.com")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "codatiel-test@example.com")
    monkeypatch.setenv("REVIEW_PIPELINE", "terra_luna_additive")
    monkeypatch.setenv("CODATIEL_EVENTS_TRANSPORT", "fixture")
    monkeypatch.delenv("CODATIEL_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("CODATIEL_DEV_STATE", raising=False)
    return state


def _store(state: Path) -> S.Store:
    state.mkdir(parents=True, exist_ok=True)
    return S.Store(state / "events.sqlite")


def _fixture_db(state: Path) -> dict:
    """Known spend/decision mix. Ledger-known total: 1.5 + 0.5 + 2.0."""
    store = _store(state)
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    store.meta_set("pipeline", "terra_luna_additive")
    store.meta_set("repos", json.dumps([REPO_A, REPO_B]))
    now = time.time()
    rd1 = state / "runs" / "rd1"
    rd1.mkdir(parents=True)
    (rd1 / "metrics.json").write_text(json.dumps(
        {"model": "terra:gpt-6.1-terra"}))
    (rd1 / "delivery.json").write_text(json.dumps(
        {"transport_receipt": {"parsed": True, "succeeded": 1,
                               "failed": 0}}))
    rd2 = state / "runs" / "rd2"
    rd2.mkdir(parents=True)
    store.review_record(repo=REPO_A, pr=1, kind="full", base_sha="a" * 40,
                        head_sha="b" * 40, diff_sha="d1", files=3,
                        pipeline="terra_luna_additive", status="complete",
                        terra_findings=2, luna_added=1, cost_usd=1.5,
                        cost_unknown=False, run_dir=str(rd1), now=now)
    store.review_record(repo=REPO_A, pr=1, kind="incremental",
                        base_sha="b" * 40, head_sha="c" * 40, diff_sha="d2",
                        files=1, pipeline="terra_luna_additive",
                        status="complete", terra_findings=0, luna_added=0,
                        cost_usd=0.5, cost_unknown=False, run_dir=str(rd1),
                        now=now)
    store.review_record(repo=REPO_A, pr=2, kind="full", base_sha="a" * 40,
                        head_sha="d" * 40, diff_sha="d3", files=9,
                        pipeline="terra_luna_additive", status="failed",
                        terra_findings=0, luna_added=0, cost_usd=None,
                        cost_unknown=True, run_dir=str(rd2), now=now)
    store.review_record(repo=REPO_B, pr=3, kind="full", base_sha="a" * 40,
                        head_sha="e" * 40, diff_sha="d4", files=2,
                        pipeline="terra_only", status="complete",
                        terra_findings=1, luna_added=0, cost_usd=2.0,
                        cost_unknown=False, run_dir=str(rd2), now=now)
    store.delivery_record(delivery_id="g1", event_type="pull_request",
                          action="opened", repo=REPO_A, pr=1,
                          head_sha="b" * 40, decision="completed",
                          reason="full review complete", cost_usd=1.5,
                          detail={})
    store.delivery_record(delivery_id="g2", event_type="pull_request",
                          action="labeled", repo=REPO_A, pr=2,
                          head_sha="d" * 40, decision="skipped:draft",
                          reason="draft deferred", cost_usd=0.0,
                          detail={})
    store.delivery_record(delivery_id="g3", event_type="push",
                          action="", repo=REPO_B, pr=0, head_sha="",
                          decision="needs_attention:unknown_endpoint",
                          reason="missing sha", cost_usd=None, detail={})
    (state / "poll.last").write_text("2026-11-13T04:45:05Z\n")
    (state / "poll.log").write_text(
        "2026-11-13T04:45:04Z poll[cron] repo=o/r open=0\n"
        "2026-11-13T04:45:05Z poll[cron] END ok ingested=2 "
        "last_ok=2026-11-13T04:45:05Z\n")
    store.close()
    return {"rd1": rd1, "rd2": rd2}


# -- validation ---------------------------------------------------------------


def test_validation_rejects_bad_docs():
    bad = [
        ({"monthly_budget_usd": -1}, "positive"),
        ({"monthly_budget_usd": 0}, "positive"),
        ({"daily_budget_usd": "lots"}, "number"),
        ({"repos": {"not-a-slug!!": True}}, "owner/repo"),
        ({"model_profile": "gpt-99"}, "fixed choices"),
        ({"alert_thresholds": {"warn_frac": 0.9, "critical_frac": 0.9}},
         "below"),
        ({"alert_thresholds": {"warn_frac": 2.0, "critical_frac": 0.9}},
         "(0, 1]"),
        ({"notify_to": ["not-an-email"]}, "email"),
        ({"pause_reviews": "yes"}, "true/false"),
        ({"mystery_key": 1}, "unknown key"),
        ({"openai_project_limit_usd": -5}, "display-only"),
    ]
    for doc, needle in bad:
        errors = OC.validate_doc(doc)
        assert errors, doc
        assert any(needle in e for e in errors), (doc, errors)


def test_validation_rejects_unknown_repo_against_allowlist():
    errors = OC.validate_doc({"repos": {"evil/fork": True}},
                             known_repos=[REPO_A])
    assert errors and any("unknown" in e for e in errors)


def test_validation_accepts_valid_doc():
    doc = {"monthly_budget_usd": 5.0, "daily_budget_usd": 1.0,
           "per_pr_breaker_usd": 2.0,
           "alert_thresholds": {"warn_frac": 0.7, "critical_frac": 0.9},
           "repos": {REPO_A: True, REPO_B: False},
           "pause_reviews": False, "pause_github_comments": True,
           "email_enabled": True, "email_on_complete": True,
           "notify_to": ["ops@example.com"], "notify_sender": "c@example.com",
           "model_profile": "terra_only",
           "openai_project_limit_usd": 100.0,
           "openai_billing_url": "https://example.com/limits"}
    assert OC.validate_doc(doc, known_repos=[REPO_A, REPO_B]) == []


# -- precedence ---------------------------------------------------------------


def test_spend_precedence_is_min(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    OC.save(state, {"monthly_budget_usd": 3.0,
                    "per_pr_breaker_usd": 9.0}, actor="t")
    settings, source = OC.load(state)
    assert source == "file"
    caps = OC.effective_spend_caps(settings, env_monthly_usd=20.0,
                                   env_per_pr_usd=2.0)
    assert caps["monthly_effective_usd"] == 3.0  # file lowers env
    assert caps["per_pr_effective_usd"] == 2.0  # env lowers stale file
    caps2 = OC.effective_spend_caps(settings, env_monthly_usd=1.0,
                                    env_per_pr_usd=50.0)
    assert caps2["monthly_effective_usd"] == 1.0
    assert caps2["per_pr_effective_usd"] == 9.0


def test_absent_file_falls_back_to_env(tmp_path):
    settings, source = OC.load(tmp_path / "missing")
    assert source == "absent" and not settings.present
    caps = OC.effective_spend_caps(settings, env_monthly_usd=20.0)
    assert caps["monthly_effective_usd"] == 20.0
    assert OC.effective_repos(settings, [REPO_A]) == [REPO_A]
    assert OC.effective_model_profile(settings, "terra_only") == "terra_only"
    assert OC.email_sending_allowed(settings) is True


def test_repos_only_narrow_and_pause_is_fail_closed(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    OC.save(state, {"repos": {REPO_A: True}, "pause_reviews": True},
            actor="t")
    settings, _ = OC.load(state)
    assert OC.effective_repos(settings, [REPO_A, REPO_B]) == [REPO_A]
    assert settings.pause_reviews is True


def test_notify_and_profile_precedence(monkeypatch, tmp_path):
    state = _env(monkeypatch, tmp_path)
    OC.save(state, {"notify_to": ["file@example.com"],
                    "notify_sender": "file-sender@example.com",
                    "model_profile": "terra_only",
                    "email_enabled": False}, actor="t")
    from codatiel_i2 import event_pipeline as P
    cfg = P.config_from_env()
    assert cfg.notify_to == ["test-ops@example.com"]
    cfg = OC.apply_to_event_config(cfg)
    assert cfg.operator_source == "file"
    assert cfg.spend_cap_usd == 20.0  # file set nothing: env stands
    assert cfg.notify_to == ["file@example.com"]  # explicit file wins
    assert cfg.notify_sender == "file-sender@example.com"
    assert cfg.pipeline == "terra_only"
    assert cfg.email_enabled is False


def test_invalid_file_refuses_loudly(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    (state / "operator.json").write_text('{"monthly_budget_usd": -2}')
    with pytest.raises(OC.OperatorConfigError, match="positive"):
        OC.load(state)


# -- audit + persistence --------------------------------------------------------


def test_audit_and_restart_persistence(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    OC.save(state, {"monthly_budget_usd": 4.0,
                    "repos": {REPO_A: True}}, actor="op1")
    OC.save(state, {"pause_reviews": True}, actor="op2")
    # Simulated restart: fresh load from disk.
    settings, source = OC.load(state)
    assert source == "file"
    assert settings.monthly_budget_usd == 4.0
    assert settings.pause_reviews is True
    assert settings.repos == {REPO_A: True}
    trail = OC.audit_trail(state)
    assert len(trail) == 2
    assert trail[0]["actor"] == "op2" and trail[0]["action"] == "saved"
    assert "monthly_budget_usd" in trail[1]["changes"]
    # Rejected attempts are audit-logged too.
    with pytest.raises(OC.OperatorConfigError):
        OC.save(state, {"monthly_budget_usd": -1}, actor="op3")
    trail = OC.audit_trail(state)
    assert trail[0]["action"] == "rejected"
    # Values unchanged by the rejected write.
    settings, _ = OC.load(state)
    assert settings.monthly_budget_usd == 4.0


# -- gates ----------------------------------------------------------------------


def test_ingest_gate_pause_allowlist_daily(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    from codatiel_i2 import event_pipeline as P
    cfg = P.EventConfig(env="test", state_dir=state, repos=[REPO_A, REPO_B],
                        spend_cap_usd=20.0, daily_cap_usd=0.01)
    store = S.Store(state / "events.sqlite")
    try:
        OC.save(state, {"pause_reviews": True}, actor="t")
        settings, _ = OC.load(state)
        gate = OC.ingest_gate(cfg, store, {"repo": REPO_A}, settings)
        assert gate and gate["decision"] == "skipped:paused"
        OC.save(state, {"pause_reviews": False,
                        "repos": {REPO_A: True}}, actor="t")
        settings, _ = OC.load(state)
        gate = OC.ingest_gate(cfg, store, {"repo": REPO_B}, settings)
        assert gate and gate["decision"] == "skipped:not-allowlisted"
        gate = OC.ingest_gate(cfg, store, {"repo": REPO_A}, settings)
        assert gate and gate["decision"] == "skipped:ceiling"
        cfg = P.EventConfig(env="test", state_dir=state,
                            repos=[REPO_A, REPO_B], spend_cap_usd=20.0)
        gate = OC.ingest_gate(cfg, store, {"repo": REPO_A})
        assert gate is None
    finally:
        store.close()


# -- spend queries ---------------------------------------------------------------


def test_spend_query_accuracy(tmp_path):
    state = tmp_path / "estate"
    _fixture_db(state)
    data = OD.collect_overview(state)
    assert data["store_present"] and data["enabled"]
    assert data["poll"]["last_successful_poll"] == "2026-11-13T04:45:05Z"
    assert data["poll"]["last_end_ok"] is True
    today = data["period"]["today"]
    assert today["codatiel_ledger_usd"] == pytest.approx(4.0)
    assert today["reviews"] == 4 and today["unknown_cost_reviews"] == 1
    by_repo = {r["repo"]: r for r in data["period"]["by_repo"]}
    assert by_repo[REPO_A]["codatiel_ledger_usd"] == pytest.approx(2.0)
    assert by_repo[REPO_B]["codatiel_ledger_usd"] == pytest.approx(2.0)
    by_model = {m["model"]: m for m in data["by_model"]}
    assert by_model["terra:gpt-6.1-terra"]["codatiel_ledger_usd"] == \
        pytest.approx(2.0)
    assert by_model["unknown"]["codatiel_ledger_usd"] == pytest.approx(2.0)
    assert by_model["unknown"]["unknown_cost_reviews"] == 1
    assert data["reviews"]["prs_reviewed"] == 2
    assert data["reviews"]["completed"] == 3
    assert data["reviews"]["failed"] == 1
    decisions = data["decisions"]
    assert decisions["completed"] == 1
    assert decisions["skipped:draft"] == 1
    assert decisions["needs_attention:unknown_endpoint"] == 1
    email = data["email"]["by_outcome"]
    assert email["delivered"] == 2  # two review rows share rd1
    assert email["no-delivery-record"] == 2, email  # both rd2 rows
    pubs = data["publications"]
    assert pubs["configured"] is False and pubs["published_total"] == 0
    urls = [r["github_url"] for r in data["recent"]["reviews"]]
    assert "https://github.com/o/r/pull/1" in urls
    assert data["ledger"]["settled_usd"] == 0.0  # untouched fixture ledger


def test_overview_tolerates_absence(tmp_path):
    data = OD.collect_overview(tmp_path / "empty")
    assert data["store_present"] is False
    assert data["publications"]["configured"] is False
    assert data["poll"]["last_successful_poll"] is None
    assert "error" in data


def test_secrets_never_rendered(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("SMTP2GO_API_KEY", "smtp2go-secret-value")
    state = tmp_path / "estate"
    _fixture_db(state)
    body = OD.render_overview(OD.collect_overview(state))
    settings_body = OD.render_settings(OD.collect_overview(state))
    assert "sk-super-secret-value" not in body
    assert "smtp2go-secret-value" not in body
    assert "sk-super-secret-value" not in settings_body
    assert "set" in settings_body  # presence marker shown instead


# -- HTTP + binding ---------------------------------------------------------------


def _serve_in_thread(state: Path):
    server = OD.build_server(state, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_localhost_binding_and_views(tmp_path):
    state = tmp_path / "estate"
    _fixture_db(state)
    server = _serve_in_thread(state)
    try:
        host, port = server.server_address[0], server.server_address[1]
        assert host == "127.0.0.1"
        with urllib.request.urlopen(
                f"http://{host}:{port}/", timeout=10) as resp:
            body = resp.read().decode()
        assert resp.status == 200
        assert "Codatiel ledger only" in body
        assert "https://github.com/o/r/pull/1" in body
        with urllib.request.urlopen(
                f"http://{host}:{port}/settings", timeout=10) as resp:
            settings_body = resp.read().decode()
        assert "monthly_budget_usd" in settings_body
        assert "model_profile" in settings_body
        # Valid POST saves + redirects.
        conn = http.client.HTTPConnection(host, port, timeout=10)
        payload = urllib.parse.urlencode(
            {"monthly_budget_usd": "6.0", "warn_frac": "0.7",
             "critical_frac": "0.9", "repos": "o/r\no/s=off",
             "openai_billing_url": "https://example.com/limits",
             "actor": "test-http"}).encode()
        conn.request("POST", "/settings", body=payload,
                     headers={"Content-Type":
                              "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        assert resp.status == 303, resp.status
        assert resp.getheader("Location") == "/settings?saved=1"
        settings, _ = OC.load(state)
        assert settings.monthly_budget_usd == 6.0
        assert settings.repos == {"o/r": True, "o/s": False}
        # Invalid POST is a 400 with the explained error, no write.
        conn.request("POST", "/settings",
                     body=urllib.parse.urlencode(
                         {"monthly_budget_usd": "-3",
                          "actor": "test-http"}).encode(),
                     headers={"Content-Type":
                              "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        assert resp.status == 400
        assert "positive" in resp.read().decode()
        settings, _ = OC.load(state)
        assert settings.monthly_budget_usd == 6.0
    finally:
        server.shutdown()
        server.server_close()


def test_non_loopback_bind_refused(tmp_path):
    with pytest.raises(OC.OperatorConfigError, match="loopback"):
        OD.build_server(tmp_path, "0.0.0.0", 8487)
    with pytest.raises(OC.OperatorConfigError, match="loopback"):
        OD.ensure_loopback("8.8.8.8")


# -- CLI ---------------------------------------------------------------------------


def test_cli_dashboard_parser_and_worker_hook(monkeypatch, tmp_path):
    from codatiel_i2.cli import build_parser, cmd_events
    args = build_parser().parse_args(["dashboard", "--port", "8488"])
    assert args.cmd == "dashboard" and args.port == 8488
    assert args.host == "127.0.0.1"
    args = build_parser().parse_args(["dashboard"])
    assert args.port == 8487
    # Worker hook: a stricter file monthly cap narrows `events status`.
    state = _env(monkeypatch, tmp_path)
    store = _store(state)
    from codatiel_i2 import event_pipeline as P
    P.enable(cfg=P.config_from_env(), store=store,
             discover=lambda r: ([], ""))
    store.close()
    args = build_parser().parse_args(["events", "status"])
    assert cmd_events(args)["spend_cap_usd"] == 20.0
    OC.save(state, {"monthly_budget_usd": 7.0}, actor="t")
    args = build_parser().parse_args(["events", "status"])
    out = cmd_events(args)
    assert out["spend_cap_usd"] == 7.0, out
    # An invalid file refuses loudly before any work.
    (state / "operator.json").write_text('{"repos": {"bogus": true}}')
    args = build_parser().parse_args(["events", "status"])
    out = cmd_events(args)
    assert out["status"] == "failed"
    assert out["stage"] == "operator-config"
