"""Spend-accounting tests (M7.1 §9.1 cost cases and §8 future executions).

No paid inference: scripted models, synthetic usage objects, and controlled
fake SDK-model failures only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import runtime_sdk
from codatiel_i2 import service as service_mod  # noqa: E402
from codatiel_i2.core.spend import CONFIRMED_ZERO_USAGE, UNKNOWN_USAGE, SpendLedger  # noqa: E402


def _usage(requests=1, in_tokens=100, out_tokens=10):
    from agents.usage import Usage

    return Usage(requests=requests, input_tokens=in_tokens, output_tokens=out_tokens,
                 total_tokens=in_tokens + out_tokens)


def test_repeated_reconciliation_never_double_debits():
    ledger = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    assert ledger.record_response("r1", _usage()) is True
    assert ledger.record_response("r1", _usage()) is False
    first = ledger.to_record()
    assert ledger.record_response("r2", _usage(in_tokens=200, out_tokens=20)) is True
    second = ledger.to_record()
    assert second["usage"] == {"requests": 2, "input_tokens": 300, "output_tokens": 30}
    assert second["usd_estimate"] == pytest.approx(0.00003 + 0.000015)
    assert ledger.to_record() == second
    assert first != second


def test_restart_during_reconciliation_is_idempotent():
    ledger = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    ledger.record_response("r1", _usage())
    snapshot = ledger.snapshot()
    restarted = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    restarted.restore(snapshot)
    assert restarted.record_response("r1", _usage()) is False
    assert restarted.record_response("r2", _usage()) is True
    assert restarted.to_record()["usage"] == {"requests": 2, "input_tokens": 200, "output_tokens": 20}


def test_unknown_usage_is_preserved_not_estimated():
    ledger = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    ledger.record_response("r1", _usage())
    ledger.record_response("timeout-1", None)
    record = ledger.to_record()
    assert record["unknown_requests"] == 1
    assert record["unknown_response_ids"] == ["timeout-1"]
    assert record["usd_is_confirmed_lower_bound"] is True
    assert record["usd_estimate"] == pytest.approx(0.00001 + 0.000005)


class _Flaky(runtime_sdk.ScriptedModel):
    def __init__(self, script, usages, failure):
        super().__init__(script)
        self._usages = list(usages)
        self._failure = failure

    async def get_response(self, *args, **kwargs):
        if self.calls >= len(self._usages):
            raise self._failure
        response = await super().get_response(*args, **kwargs)
        response.usage = self._usages[self.calls - 1]
        return response


def _repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "cost@test"], ["config", "user.name", "cost"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, timeout=60)
    (root / "app.py").write_text("LIMIT = 100\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, timeout=60)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True, timeout=60)
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True, timeout=60).stdout.strip()
    (root / "app.py").write_text("LIMIT = 10\n")
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "head"], check=True, timeout=60)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True, timeout=60).stdout.strip()
    return {"root": root, "base": base, "head": head}


def _scenario(tmp_path: Path, name: str, turns: list, final: dict) -> str:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"turns": turns, "final": final}))
    return str(path)


def test_multiple_billed_turns_then_failure_retains_partial_usage(tmp_path: Path):
    repo = _repo(tmp_path)
    script = [
        runtime_sdk.scripted_tool_call(call_id="a", name="read_file",
                                       arguments={"path": "app.py", "revision": "head"},
                                       response_id="r1"),
        runtime_sdk.scripted_tool_call(call_id="b", name="read_file",
                                       arguments={"path": "app.py", "revision": "head"},
                                       response_id="r2"),
    ]
    model = _Flaky(script, [_usage(in_tokens=1000, out_tokens=100),
                            _usage(in_tokens=2000, out_tokens=200)],
                   RuntimeError("provider failed after two billed turns"))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model, model_name="luna:test-cost",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        rate={"input": 0.10, "output": 0.50}, rate_status="priced", max_usd_per_review=5.0)
    assert out["status"] == "failed"
    spend = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())["spend"]
    assert spend["usage"] == {"requests": 2, "input_tokens": 3000, "output_tokens": 300}
    assert spend["usd_estimate"] == pytest.approx(0.0003 + 0.00015)
    assert spend["accounting_status"] == UNKNOWN_USAGE
    assert len(spend["request_ledger"]) == 2


def test_provider_failure_before_confirmed_usage_is_unknown(tmp_path: Path):
    repo = _repo(tmp_path)
    model = _Flaky([], [], RuntimeError("provider failed immediately"))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model, model_name="luna:test-cost",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        rate={"input": 0.10, "output": 0.50}, rate_status="priced", max_usd_per_review=5.0)
    spend = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())["spend"]
    assert spend["usage"] == {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    assert spend["usd_estimate"] is None
    assert spend["accounting_status"] == UNKNOWN_USAGE


def test_response_before_validation_failure_is_retained(tmp_path: Path):
    repo = _repo(tmp_path)
    scenario = _scenario(tmp_path, "validation-failure",
                         [{"tool": "read_file", "args": {"path": "app.py", "revision": "head"}}],
                         {"status": "finding", "summary": "x", "findings": [{
                             "detector": "contracts", "title": "t", "path": "app.py",
                             "revision": "head", "line_start": 1, "line_end": 1,
                             "claim": "c", "evidence_ids": ["ev_" + "f" * 16]}]})
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"],
        model=service_mod.scripted_model_from_scenario(scenario), model_name="scripted:cost",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work")
    rows = [json.loads(line) for line in
            (Path(out["run_dir"]) / "findings.jsonl").read_text().splitlines() if line.strip()]
    agent_rows = [row for row in rows if row["finding_id"].startswith("agent-")]
    assert agent_rows[0]["disposition"] == "rejected_invalid"
    spend = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())["spend"]
    assert spend["usage"]["requests"] >= 1
    assert spend["accounting_status"] == CONFIRMED_ZERO_USAGE


def test_budget_exhaustion_blocks_further_paid_runs(tmp_path: Path):
    repo = _repo(tmp_path)
    scenario = _scenario(tmp_path, "blocked", [], {"status": "silence", "summary": "", "findings": []})
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"],
        model=service_mod.scripted_model_from_scenario(scenario), model_name="scripted:cost",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        rate={"input": 1000.0, "output": 1000.0}, rate_status="test-priced",
        max_usd_per_review=0.000001)
    assert out["status"] == "budget_refused"
    assert "run_dir" not in out


def test_unconfirmed_rates_refuse_paid_inference(tmp_path: Path):
    repo = _repo(tmp_path)
    scenario = _scenario(tmp_path, "unpriced", [], {"status": "silence", "summary": "", "findings": []})
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"],
        model=service_mod.scripted_model_from_scenario(scenario), model_name="luna:unpriced",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        rate={}, rate_status="unconfirmed", max_usd_per_review=5.0)
    assert out["status"] == "budget_refused"
    assert out["stage"] == "preflight"


def test_paid_completion_is_confirmed_usage(tmp_path: Path):
    repo = _repo(tmp_path)
    script = [runtime_sdk.scripted_tool_call(call_id="a", name="read_file",
                                             arguments={"path": "app.py", "revision": "head"},
                                             response_id="r1"),
              runtime_sdk.scripted_final(json.dumps({"status": "silence", "summary": "ok",
                                                     "findings": []}),
                                         response_id="r2")]
    model = _Flaky(script, [_usage(in_tokens=1000, out_tokens=100),
                            _usage(in_tokens=500, out_tokens=50)], RuntimeError("unused"))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model, model_name="luna:test-cost",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        rate={"input": 0.10, "output": 0.50}, rate_status="priced", max_usd_per_review=5.0)
    assert out["status"] == "complete"
    spend = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())["spend"]
    assert spend["accounting_status"] == "CONFIRMED_USAGE"
    assert spend["usage"] == {"requests": 2, "input_tokens": 1500, "output_tokens": 150}
    assert spend["usd_estimate"] == pytest.approx(0.00015 + 0.000075)
    assert (Path(out["run_dir"]) / "spend_ledger.jsonl").exists()


def test_turn_exhaustion_after_billed_responses_retains_partial_spend(tmp_path: Path):
    repo = _repo(tmp_path)

    class _TurnsExceeded(Exception):
        pass
    _TurnsExceeded.__name__ = "MaxTurnsExceeded"

    script = [runtime_sdk.scripted_tool_call(call_id="a", name="read_file",
                                             arguments={"path": "app.py", "revision": "head"},
                                             response_id="r1"),
              runtime_sdk.scripted_tool_call(call_id="b", name="read_file",
                                             arguments={"path": "app.py", "revision": "head"},
                                             response_id="r2")]
    model = _Flaky(script, [_usage(in_tokens=1000, out_tokens=100),
                            _usage(in_tokens=2000, out_tokens=200)],
                   _TurnsExceeded("too many turns"))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model, model_name="luna:test-cost",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        rate={"input": 0.10, "output": 0.50}, rate_status="priced", max_usd_per_review=5.0)
    assert out["status"] == "incomplete_budget_exhausted"
    spend = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())["spend"]
    assert spend["usage"] == {"requests": 2, "input_tokens": 3000, "output_tokens": 300}
    assert spend["accounting_status"] == UNKNOWN_USAGE


def test_confirmed_spend_past_ceiling_aborts_with_partial_ledger(tmp_path: Path):
    repo = _repo(tmp_path)
    script = [runtime_sdk.scripted_tool_call(call_id="a", name="read_file",
                                             arguments={"path": "app.py", "revision": "head"},
                                             response_id="r1")]
    model = _Flaky(script, [_usage(in_tokens=1000, out_tokens=20000)], RuntimeError("unused"))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model, model_name="luna:test-cost",
        runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        rate={"input": 0.10, "output": 0.50}, rate_status="priced",
        max_usd_per_review=0.0025, max_turns=1)
    assert out["status"] == "failed"
    spend = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())["spend"]
    assert spend["exceeds_ceiling"] is True
    assert len(spend["request_ledger"]) == 1
    assert spend["accounting_status"] == UNKNOWN_USAGE


def test_string_model_id_is_resolved_before_wrapping(tmp_path, monkeypatch):
    """Regression: native SDK string model ids (Luna backend) must resolve to
    a Model object before spend wrapping; otherwise the run dies with
    AttributeError before any inference."""
    import agents.models.openai_provider as provider_mod

    script = [runtime_sdk.scripted_final('{"status": "silence", "summary": "ok", "findings": []}',
                                         response_id="r1")]

    class _FakeProvider:
        def get_model(self, name):
            assert name == "fake:string"
            return runtime_sdk.ScriptedModel(list(script))

    monkeypatch.setattr(provider_mod, "OpenAIProvider", lambda: _FakeProvider())
    ledger = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    result = runtime_sdk.run_review(
        instructions="test", user_input="test", tools=[], model="fake:string",
        max_turns=1, usage_ledger=ledger)
    assert result is not None
    assert ledger.to_record()["usage"]["requests"] == 1


def test_sink_is_durable_and_restores_without_double_debit(tmp_path: Path):
    from codatiel_i2.core.spend import SpendLedger

    ledger = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    sink = tmp_path / "sink.jsonl"
    ledger.attach_sink(sink)
    assert ledger.sink_error == ""
    ledger.record_response("r1", _usage(in_tokens=100, out_tokens=10))
    ledger.record_response("r2", _usage(in_tokens=200, out_tokens=20))
    # The sink holds every response durably, independent of process memory.
    rows = [json.loads(line) for line in sink.read_text().splitlines() if line.strip()]
    assert [row["response_id"] for row in rows] == ["r1", "r2"]
    restarted = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    restarted.restore_sink(sink)
    assert restarted.record_response("r1", _usage(in_tokens=100, out_tokens=10)) is False
    assert restarted.to_record()["usage"] == {"requests": 2, "input_tokens": 300, "output_tokens": 30}


def test_sink_restore_applies_usage_unknown_corrections(tmp_path: Path):
    from codatiel_i2.core.spend import SpendLedger

    ledger = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    sink = tmp_path / "sink.jsonl"
    ledger.attach_sink(sink)
    ledger.record_response("r1", _usage(in_tokens=100, out_tokens=10))
    ledger.mark_unknown("r1")
    # Crash recovery must keep the unknown marking, not resurrect confirmed zeros.
    restarted = SpendLedger(rate={"input": 0.10, "output": 0.50}, rate_status="priced", ceiling_usd=5.0)
    restarted.restore_sink(sink)
    record = restarted.to_record()
    assert record["unknown_requests"] == 1
    assert record["unknown_response_ids"] == ["r1"]
    assert record["usd_estimate"] is None


def test_tool_budget_reservation_is_atomic_under_concurrency(tmp_path):
    """Concurrent pre-dispatch checks must not over-admit: at most
    `tool_budget` calls may proceed to execution.

    Exercises the exact check-then-record sequence the SDK tool wrappers
    use, from many threads against one shared context: without atomic
    reservation, interleaved checks would admit more than the budget.
    """
    import threading

    from codatiel_i2 import runtime_sdk

    ctx = runtime_sdk.ReviewContext(
        repo="r", base_sha="b", head_sha="h", base_dir=tmp_path,
        head_dir=tmp_path, diff_repo=tmp_path, tool_budget=3)
    admitted: list = []
    lock = threading.Lock()

    def attempt():
        blocked = runtime_sdk._over_budget(ctx)
        if blocked is None:
            with lock:
                admitted.append(1)
            runtime_sdk._record(ctx, "read_file", {"path": "a.py"},
                                {"evidence": {"evidence_id": "ev_" + "0" * 16}})

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly `tool_budget` racers admitted; the rest were refused.
    assert len(admitted) == 3
    assert len(ctx.tool_log) == 3


def test_split_cache_pricing_uses_hit_rate_for_reported_cached_tokens():
    from codatiel_i2.core.spend import SpendLedger

    ledger = SpendLedger(
        rate={"input": 0.30, "input_cache_hit": 0.006, "output": 1.20},
        rate_status="priced", ceiling_usd=5.0)
    ledger.record_response("r1", _usage(in_tokens=1000, out_tokens=100),
                           cached_input_tokens=800, reasoning_output_tokens=60)
    record = ledger.to_record()
    # 200 miss @0.30 + 800 hit @0.006 + 100 out @1.20, per million.
    assert record["usd_estimate"] == pytest.approx(
        200 / 1e6 * 0.30 + 800 / 1e6 * 0.006 + 100 / 1e6 * 1.20)
    assert record["cached_input_tokens"] == 800
    assert record["reasoning_output_tokens"] == 60
    assert record["usage_details_reported_responses"] == 1


def test_absent_details_stay_unknown_not_zero_and_use_flat_rate():
    from codatiel_i2.core.spend import SpendLedger

    ledger = SpendLedger(
        rate={"input": 0.30, "input_cache_hit": 0.006, "output": 1.20},
        rate_status="priced", ceiling_usd=5.0)
    ledger.record_response("r1", _usage(in_tokens=1000, out_tokens=100))
    entry = ledger.entries["r1"]
    assert entry["cached_input_tokens"] is None
    assert entry["reasoning_output_tokens"] is None
    record = ledger.to_record()
    assert record["usd_estimate"] == pytest.approx(
        1000 / 1e6 * 0.30 + 100 / 1e6 * 1.20)
    assert record["usage_details_reported_responses"] == 0


def test_mark_unknown_clears_reported_details():
    from codatiel_i2.core.spend import SpendLedger

    ledger = SpendLedger(rate={"input": 0.30, "output": 1.20}, rate_status="priced",
                         ceiling_usd=5.0)
    ledger.record_response("r1", _usage(), cached_input_tokens=50,
                           reasoning_output_tokens=5)
    ledger.mark_unknown("r1")
    entry = ledger.entries["r1"]
    assert entry["cached_input_tokens"] is None
    assert entry["reasoning_output_tokens"] is None


def test_deepseek_backend_resolves_and_marks_usage_details(monkeypatch):
    from codatiel_i2 import providers as P

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    resolved = P.resolve_backend("deepseek")
    assert resolved["model"] == "deepseek-flash"
    assert resolved["rate_usd_per_mtok"]["input_cache_hit"] == 0.006
    model, settings = P.build_model("deepseek")
    assert getattr(model, "provider_usage_details", False) is True
    assert settings.extra_body == {"thinking": {"type": "enabled"}}
    assert settings.max_tokens == 8192


def test_deepseek_is_paid_backend():
    from codatiel_i2 import service as service_mod

    assert service_mod._model_is_paid_backend("deepseek:deepseek-flash") is True


def test_glm_backend_resolves_with_quota_billing(monkeypatch):
    from codatiel_i2 import providers as P

    monkeypatch.setenv("ZAI_API_KEY", "test-id.test-secret")
    resolved = P.resolve_backend("glm")
    assert resolved["model"] == "glm-5.3-flash"
    assert resolved["rate_status"].startswith("quota_")
    assert resolved["rate_usd_per_mtok"] == {}
    model, settings = P.build_model("glm")
    assert getattr(model, "provider_usage_details", False) is True
    assert settings.extra_body == {"thinking": {"type": "enabled",
                                                "clear_thinking": False}}
    assert settings.max_tokens == 8192
    # Reasoning replay is enabled for glm models (provider requires
    # reasoning_content back with tool results under preserved thinking).
    hook = getattr(model, "should_replay_reasoning_content", None)
    assert callable(hook)
    from agents.models.reasoning_content_replay import (
        ReasoningContentReplayContext, ReasoningContentSource)
    ctx = ReasoningContentReplayContext(
        model="glm-5.3-flash", base_url=None,
        reasoning=ReasoningContentSource(item={}, origin_model="glm-5.3-flash",
                                         provider_data={}))
    assert hook(ctx) is True


def test_glm_is_paid_backend_with_quota_carve_out():
    from codatiel_i2 import service as service_mod

    assert service_mod._model_is_paid_backend("glm:glm-5.3-flash") is True
    assert service_mod._rate_is_quota(
        "quota_zai_coding_plan_tokens_recorded_usd_unconfirmed") is True
    assert service_mod._rate_is_quota("standard_verified") is False
