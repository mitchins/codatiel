"""STABILIZE-02 R1.1: Anthropic partials are lower bounds, never maxima.

A Haiku request with missing cache figures is PRICING_PARTIAL (its known
input is a lower bound; cache lines bill separately). A run holding one
sends no further model request. Every request is pre-checked against the
remaining budget before sending. OpenAI missing-split bounds are
unchanged (true maxima). Deterministic, no network, no inference.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import types
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import runtime_sdk
from codatiel_i2 import service as service_mod  # noqa: E402
from codatiel_i2.core.spend import (  # noqa: E402
    PRICED,
    PRICING_PARTIAL,
    PRICING_UNAVAILABLE,
    BudgetCeilingExceeded,
    PricingUnbounded,
    SpendLedger,
)


def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _card(backend):
    if backend == "luna":
        return {
            "input": 0.1, "output": 0.5,
            "input_cache_hit": 0.01, "input_cache_write": 0.125,
            "pricing_backend": "luna", "processing_tier": "standard",
            "tier_threshold_tokens": 272000,
            "tier_threshold_basis": "total_input_tokens_including_cache",
            "tiers": {
                "short": {"input": 0.1, "output": 0.5,
                          "input_cache_hit": 0.01, "input_cache_write": 0.125},
                "long": {"input": 0.2, "output": 0.75,
                         "input_cache_hit": 0.02, "input_cache_write": 0.25},
            },
        }
    return {
        "input": 0.1, "output": 0.5,
        "input_cache_hit": 0.01, "input_cache_write": 0.125,
        "pricing_backend": "haiku", "processing_tier": "standard",
        "tier_threshold_tokens": 100000,
        "tier_threshold_basis": "prompt_total_uncached_plus_cache_read_plus_cache_write",
        "tiers": {
            "standard": {"input": 0.1, "output": 0.5,
                         "input_cache_hit": 0.01, "input_cache_write": 0.125},
            "high": {"input": 0.5, "output": 2.5,
                     "input_cache_hit": 0.05, "input_cache_write": 0.625},
        },
    }


def _ledger(backend, **kw):
    args = {"rate": _card(backend), "rate_status": "tiered-test",
            "ceiling_usd": 5.0}
    args.update(kw)
    return SpendLedger(**args)


def _usage(requests=1, in_tokens=100, out_tokens=10):
    from agents.usage import Usage

    return Usage(requests=requests, input_tokens=in_tokens,
                 output_tokens=out_tokens,
                 total_tokens=in_tokens + out_tokens)


# --------------------------------------------- complete usage unmoved (R1)

def test_complete_usage_priced_exactly_as_r1():
    # Byte-for-byte R1 values for complete splits on recorded fixtures.
    led = _ledger("haiku")
    led.record_response("r1", _ns(requests=1, input_tokens=1000, output_tokens=10),
                        cached_input_tokens=100, cache_write_input_tokens=50,
                        cache_in_input=False)
    expect = (1000 * 0.1 + 100 * 0.01 + 50 * 0.125 + 10 * 0.5) / 1e6
    assert led.confirmed_usd() == pytest.approx(expect)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["r1"]["pricing_status"] == PRICED
    assert rows["r1"]["tier"] == "standard"
    assert rows["r1"]["priced_cost_usd"] == pytest.approx(expect)

    led = _ledger("luna")
    led.record_response("r1", _ns(requests=1, input_tokens=10000, output_tokens=100),
                        cached_input_tokens=8000, cache_write_input_tokens=1000)
    expect = (1000 * 0.1 + 8000 * 0.01 + 1000 * 0.125 + 100 * 0.5) / 1e6
    assert led.confirmed_usd() == pytest.approx(expect)
    assert led.to_record()["pricing_partial_ids"] == []


# --------------------------------------- haiku missing figures -> PARTIAL

def test_haiku_missing_read_is_partial_lower_bound():
    led = _ledger("haiku")
    led.record_response("r1", _ns(requests=1, input_tokens=90000, output_tokens=100),
                        cached_input_tokens=None, cache_write_input_tokens=5000,
                        cache_in_input=False)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    # Prompt size unknown (write present, read missing): high tier, never
    # cheap. Lower bound prices only the known lines.
    assert rows["r1"]["tier"] == "high"
    assert rows["r1"]["pricing_status"] == PRICING_PARTIAL
    lower = (90000 * 0.5 + 100 * 2.5) / 1e6
    assert rows["r1"]["priced_cost_usd"] == pytest.approx(lower)
    assert rows["r1"]["bounded_reserve_usd"] is None
    assert led.confirmed_usd() == pytest.approx(lower)


def test_haiku_missing_write_is_partial_lower_bound():
    led = _ledger("haiku")
    led.record_response("r1", _ns(requests=1, input_tokens=5000, output_tokens=50),
                        cached_input_tokens=1000, cache_write_input_tokens=None,
                        cache_in_input=False)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["r1"]["pricing_status"] == PRICING_PARTIAL
    lower = (5000 * 0.5 + 50 * 2.5) / 1e6
    assert rows["r1"]["priced_cost_usd"] == pytest.approx(lower)


def test_haiku_both_missing_is_partial_lower_bound():
    led = _ledger("haiku")
    led.record_response("r1", _ns(requests=1, input_tokens=5000, output_tokens=50),
                        cache_in_input=False)
    rec = led.to_record()
    rows = {r["response_id"]: r for r in rec["request_ledger"]}
    assert rows["r1"]["tier"] == "high"
    assert rows["r1"]["pricing_status"] == PRICING_PARTIAL
    lower = (5000 * 0.5 + 50 * 2.5) / 1e6
    assert led.confirmed_usd() == pytest.approx(lower)
    assert rec["pricing_partial_ids"] == ["r1"]
    assert rec["pricing_partial_requests"] == 1
    assert rec["partial_lower_bound_usd"] == pytest.approx(lower)
    assert rec["pricing_unavailable_ids"] == []
    assert rec["bounded_reserve_usd"] == pytest.approx(0.0)
    assert rec["usd_is_confirmed_lower_bound"] is True


def test_haiku_malformed_fields_become_partial_not_zero():
    led = _ledger("haiku")
    led.record_response("r1", _ns(requests=1, input_tokens=1000, output_tokens=10),
                        cached_input_tokens="lots",
                        cache_write_input_tokens={"n": 3},
                        cache_in_input=False)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert led.entries["r1"]["cached_input_tokens"] is None
    assert rows["r1"]["pricing_status"] == PRICING_PARTIAL


def test_haiku_explicit_zero_is_priced_absent_is_partial():
    led = _ledger("haiku")
    led.record_response("zero", _ns(requests=1, input_tokens=1000, output_tokens=10),
                        cached_input_tokens=0, cache_write_input_tokens=0,
                        cache_in_input=False)
    led.record_response("absent", _ns(requests=1, input_tokens=1000, output_tokens=10),
                        cache_in_input=False)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["zero"]["pricing_status"] == PRICED
    assert rows["zero"]["priced_cost_usd"] == pytest.approx(
        (1000 * 0.1 + 10 * 0.5) / 1e6)
    assert rows["absent"]["pricing_status"] == PRICING_PARTIAL


def test_haiku_partial_records_stay_in_tier_for_small_prompts():
    # A complete small split prices standard; partiality is about the
    # missing split, not the tier: both-missing forces high (unknown
    # prompt), while a fully known small prompt stays standard.
    led = _ledger("haiku")
    led.record_response("known", _ns(requests=1, input_tokens=800, output_tokens=10),
                        cached_input_tokens=100, cache_write_input_tokens=100,
                        cache_in_input=False)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["known"]["tier"] == "standard"
    assert rows["known"]["pricing_status"] == PRICED


# -------------------------------------- luna missing split still a bound

def test_luna_missing_write_remains_true_bound():
    led = _ledger("luna")
    led.record_response("r1", _ns(requests=1, input_tokens=10000, output_tokens=100),
                        cached_input_tokens=8000,
                        cache_write_input_tokens=None)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["r1"]["pricing_status"] == PRICING_UNAVAILABLE
    assert rows["r1"]["bounded_reserve_usd"] == pytest.approx(
        10000 / 1e6 * 0.125 + 100 / 1e6 * 0.5)
    assert led.to_record()["pricing_partial_ids"] == []
    assert led.has_unbounded() is False


# ------------------------------------------------------- unbounded halts

def test_run_holding_partial_sends_no_further_request():
    led = _ledger("haiku")
    led.record_response("r1", _ns(requests=1, input_tokens=5000, output_tokens=50),
                        cache_in_input=False)
    assert led.has_unbounded() is True
    script = [runtime_sdk.scripted_final('{"status": "silence"}',
                                         response_id="r2")]
    model = runtime_sdk.UsageRecordingModel(runtime_sdk.ScriptedModel(script),
                                            led)
    with pytest.raises(PricingUnbounded):
        asyncio.run(model.get_response(
            None, "hello", None, [], None, [], None,
            previous_response_id=None, conversation_id=None, prompt=None))
    assert model._model.calls == 0
    assert led.to_record()["usage"]["requests"] == 1


def test_clean_run_has_no_halt():
    led = _ledger("haiku")
    assert led.has_unbounded() is False
    script = [runtime_sdk.scripted_final('{"status": "silence"}',
                                         response_id="r1")]
    model = runtime_sdk.UsageRecordingModel(runtime_sdk.ScriptedModel(script),
                                            led)
    asyncio.run(model.get_response(
        None, "hello", None, [], None, [], None,
        previous_response_id=None, conversation_id=None, prompt=None))
    assert model._model.calls == 1


def test_partial_lower_bound_inside_budget_still_halts():
    # The halt is independent of ceiling arithmetic: even when the lower
    # bound fits, the unbounded remainder forbids the next request.
    led = _ledger("haiku", ceiling_usd=5.0)
    led.record_response("r1", _ns(requests=1, input_tokens=100, output_tokens=10),
                        cache_in_input=False)
    assert led.over_ceiling() is False
    assert led.has_unbounded() is True


# ------------------------------------------------- pre-request check

def test_pre_request_refusal_sends_nothing():
    led = _ledger("haiku", ceiling_usd=0.01)
    script = [runtime_sdk.scripted_final('{"status": "silence"}',
                                         response_id="r1")]
    model = runtime_sdk.UsageRecordingModel(runtime_sdk.ScriptedModel(script),
                                            led)
    with pytest.raises(BudgetCeilingExceeded):
        asyncio.run(model.get_response(
            None, "hello", None, [], None, [], None,
            previous_response_id=None, conversation_id=None, prompt=None))
    assert model._model.calls == 0
    assert led.to_record()["usage"]["requests"] == 0


def test_pre_request_passes_with_headroom():
    led = _ledger("haiku", ceiling_usd=5.0)
    script = [runtime_sdk.scripted_final('{"status": "silence"}',
                                         response_id="r1")]
    model = runtime_sdk.UsageRecordingModel(runtime_sdk.ScriptedModel(script),
                                            led)
    asyncio.run(model.get_response(
        None, "hello", None, [], None, [], None,
        previous_response_id=None, conversation_id=None, prompt=None))
    assert model._model.calls == 1


def test_pre_request_uses_highest_tier_and_rates():
    led = _ledger("haiku", ceiling_usd=5.0)
    # 100 tokens estimated at the highest input-side rate (high write
    # 0.625) plus the stated 8192-token output allowance at high output.
    ok, est, remaining = led.pre_request_ok(100)
    assert ok is True
    assert est == pytest.approx(100 / 1e6 * 0.625 + 8192 / 1e6 * 2.5)
    assert remaining == pytest.approx(5.0)


def test_legacy_ledgers_skip_pre_request_check():
    led = SpendLedger(rate={"input": 0.10, "output": 0.50},
                      rate_status="priced", ceiling_usd=0.00001)
    assert led._tier_card() is None
    assert led.has_unbounded() is False


# ------------------------------------------------------- service mapping

def _repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "cost@test"],
                 ["config", "user.name", "cost"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, timeout=60)
    (root / "app.py").write_text("LIMIT = 100\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, timeout=60)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True, timeout=60)
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    (root / "app.py").write_text("LIMIT = 10\n")
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "head"], check=True, timeout=60)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    return {"root": root, "base": base, "head": head}


class _FlakyHaiku(runtime_sdk.ScriptedModel):
    """Scripted model with a Haiku-style adapter split: provider usage
    carries input/output totals but no cache details, and the adapter
    marks input_tokens as uncached-only."""

    def __init__(self, script, usages):
        super().__init__(script)
        self._usages = list(usages)

    def usage_details_for(self, response_id):
        return {"cache_in_input": False}

    async def get_response(self, *args, **kwargs):
        response = await super().get_response(*args, **kwargs)
        if self.calls <= len(self._usages):
            response.usage = self._usages[self.calls - 1]
        return response


def test_service_maps_pricing_unbounded_to_incomplete(tmp_path: Path):
    repo = _repo(tmp_path)
    script = [
        runtime_sdk.scripted_tool_call(call_id="a", name="read_file",
                                       arguments={"path": "app.py",
                                                  "revision": "head"},
                                       response_id="r1"),
        runtime_sdk.scripted_final('{"status": "silence"}', response_id="r2"),
    ]
    model = _FlakyHaiku(script, [_usage(in_tokens=1000, out_tokens=100),
                                _usage(in_tokens=1000, out_tokens=100)])
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="haiku:test-partial", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", rate=_card("haiku"),
        rate_status="tiered-test", max_usd_per_review=5.0, max_turns=2)
    assert out["status"] == "incomplete_pricing_unbounded"
    # The first request was recorded partial; the second was never sent.
    assert model.calls == 1
    spend = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())["spend"]
    assert spend["pricing_partial_ids"] == ["r1"]
    assert spend["usd_is_confirmed_lower_bound"] is True
    assert out["status"] not in ("complete", "correct silence", "finding")


def test_service_maps_budget_ceiling_to_incomplete(tmp_path: Path):
    repo = _repo(tmp_path)
    script = [runtime_sdk.scripted_final('{"status": "silence"}',
                                         response_id="r1")]
    model = runtime_sdk.ScriptedModel(list(script))
    out = service_mod.review_local(
        repo["root"], repo["base"], repo["head"], model=model,
        model_name="haiku:test-ceiling", runs_root=tmp_path / "runs",
        work_root=tmp_path / "work", rate=_card("haiku"),
        rate_status="tiered-test", max_usd_per_review=0.01, max_turns=1)
    assert out["status"] == "incomplete_budget_ceiling"
    assert model.calls == 0
    spend = json.loads((Path(out["run_dir"]) / "metrics.json").read_text())["spend"]
    assert spend["usage"]["requests"] == 0
