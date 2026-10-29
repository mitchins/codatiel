"""CACHE-01 mocked qualification: caching options, usage fields and
accounting survive the adapters. Deterministic, no network, no inference."""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from codatiel_i2.core.spend import (
    CACHE_HIT,
    CACHE_MISS,
    CACHE_UNSUPPORTED,
    CACHE_USAGE_UNAVAILABLE,
    SpendLedger,
    classify_cache,
)


def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _ledger(**rate):
    base = {"input": 0.1, "output": 0.5}
    base.update(rate)
    return SpendLedger(rate=base, rate_status="test", ceiling_usd=5.0)


# ------------------------------------------------------------------ ledger

def test_mixed_read_write_uncached_pricing():
    # input_tokens already includes the cache sub-counts (both providers).
    led = _ledger(input_cache_hit=0.01, input_cache_write=0.125)
    led.record_response("r1", _ns(requests=1, input_tokens=10000,
                                  output_tokens=100),
                        cached_input_tokens=8000,
                        cache_write_input_tokens=1000)
    # uncached 1000 @0.1 + read 8000 @0.01 + write 1000 @0.125 + out 100 @0.5
    expect = 1000 / 1e6 * 0.1 + 8000 / 1e6 * 0.01 + 1000 / 1e6 * 0.125 \
        + 100 / 1e6 * 0.5
    assert led.confirmed_usd() == pytest.approx(expect)


def test_anthropic_semantics_input_excludes_cache():
    # Anthropic input_tokens counts only uncached tokens: no subtraction.
    led = _ledger(input_cache_hit=0.01, input_cache_write=0.125)
    led.record_response("r1", _ns(requests=1, input_tokens=100,
                                  output_tokens=10),
                        cached_input_tokens=800,
                        cache_write_input_tokens=100,
                        cache_in_input=False)
    expect = (100 * 0.1 + 800 * 0.01 + 100 * 0.125 + 10 * 0.5) / 1e6
    assert led.confirmed_usd() == pytest.approx(expect)


def test_openai_semantics_input_includes_cache():
    # OpenAI-family input_tokens is the whole prompt including cache
    # sub-counts: the uncached slice excludes both (default True).
    led = _ledger(input_cache_hit=0.01, input_cache_write=0.125)
    led.record_response("r1", _ns(requests=1, input_tokens=1000,
                                  output_tokens=10),
                        cached_input_tokens=800,
                        cache_write_input_tokens=100)
    expect = (100 * 0.1 + 800 * 0.01 + 100 * 0.125 + 10 * 0.5) / 1e6
    assert led.confirmed_usd() == pytest.approx(expect)
    assert led.entries["r1"]["cache_in_input"] is True


def test_write_falls_back_to_input_rate_when_unpriced():
    led = _ledger(input_cache_hit=0.01)  # no write rate
    led.record_response("r1", _ns(requests=1, input_tokens=2000,
                                  output_tokens=0),
                        cached_input_tokens=0,
                        cache_write_input_tokens=1000)
    expect = 1000 / 1e6 * 0.1 + 1000 / 1e6 * 0.1
    assert led.confirmed_usd() == pytest.approx(expect)


def test_missing_cache_metadata_is_not_zero():
    led = _ledger(input_cache_hit=0.01, input_cache_write=0.125)
    led.record_response("r1", _ns(requests=1, input_tokens=1000,
                                  output_tokens=10))
    entry = led.entries["r1"]
    assert entry["cached_input_tokens"] is None
    assert entry["cache_write_input_tokens"] is None
    assert "usage_details_reported_responses" in led.to_record()
    assert led.to_record()["usage_details_reported_responses"] == 0


def test_classify_cache_states():
    assert classify_cache(None, True) == CACHE_USAGE_UNAVAILABLE
    assert classify_cache({"usage_known": False}, True) == CACHE_USAGE_UNAVAILABLE
    assert classify_cache({"usage_known": True, "cached_input_tokens": 0,
                           "cache_write_input_tokens": 0}, False) == CACHE_UNSUPPORTED
    assert classify_cache({"usage_known": True, "cached_input_tokens": 500,
                           "cache_write_input_tokens": 0}, True) == CACHE_HIT
    assert classify_cache({"usage_known": True, "cached_input_tokens": 0,
                           "cache_write_input_tokens": 500}, True) == CACHE_MISS
    assert classify_cache({"usage_known": True, "cached_input_tokens": None,
                           "cache_write_input_tokens": None}, None) == CACHE_USAGE_UNAVAILABLE


def test_cache_key_isolation_by_response_id():
    led = _ledger()
    led.record_response("resp-A", _ns(requests=1, input_tokens=100,
                                      output_tokens=10),
                        cached_input_tokens=90)
    # Same content, different provider response id: separate entry, no merge.
    assert led.record_response("resp-B", _ns(requests=1, input_tokens=100,
                                            output_tokens=10),
                               cached_input_tokens=90) is True
    assert led.record_response("resp-A", _ns(requests=1, input_tokens=100,
                                            output_tokens=10),
                               cached_input_tokens=90) is False
    assert len(led.entries) == 2


def test_accurate_cost_reservations_with_cache():
    led = _ledger(input_cache_hit=0.01)
    led.ceiling_usd = 0.0002
    led.record_response("r1", _ns(requests=1, input_tokens=10000,
                                  output_tokens=0),
                        cached_input_tokens=9000)
    # 1000 @0.1 + 9000 @0.01 = 0.00019 < ceiling: no false trip.
    assert led.confirmed_usd() == pytest.approx(0.00019)
    assert led.confirmed_usd() < led.ceiling_usd


# ------------------------------------------------------------------ Luna wrapper

def _luna_wrapped(cached=None):
    from codatiel_i2 import runtime_sdk as R
    details = None if cached == "absent" else _ns(cached_tokens=cached)
    usage = _ns(requests=1, input_tokens=1000, output_tokens=10,
                input_tokens_details=details,
                output_tokens_details=_ns(reasoning_tokens=None))
    resp = _ns(response_id="resp-luna-1", usage=usage)
    ledger = SpendLedger(rate={"input": 0.1, "output": 0.5,
                               "input_cache_hit": 0.01,
                               "input_cache_write": 0.125},
                         rate_status="t", ceiling_usd=5.0)

    class NativeOpenAIModel:
        async def get_response(self, *a, **k):
            return resp

    async def go():
        return await R.UsageRecordingModel(NativeOpenAIModel(),
                                           ledger).get_response()
    asyncio.run(go())
    return ledger


def test_luna_native_details_recorded_not_dropped():
    # Root-cause regression: the native OpenAI model has no
    # provider_usage_details flag, yet its usage structurally carries
    # provider-mapped input_tokens_details. Cached tokens must be read.
    ledger = _luna_wrapped(cached=800)
    assert ledger.entries["resp-luna-1"]["cached_input_tokens"] == 800


def test_luna_absent_details_stay_none():
    ledger = _luna_wrapped(cached="absent")
    assert ledger.entries["resp-luna-1"]["cached_input_tokens"] is None


# ------------------------------------------------------------------ Haiku adapter

def _haiku_model(payload_usage):
    from codatiel_i2 import runtime_sdk as R
    model = R.HaikuModel.__new__(R.HaikuModel)
    model._model = "claude-haiku-5-5"
    model._max_tokens = 10
    model._thinking = None
    model._output_config = None
    model._thinking_groups = []
    model._usage_details = {}
    model._unknown_usage_response_ids = set()
    model._termination_by_response = {}

    async def fake_create(**kwargs):
        fake_create.kwargs = kwargs
        return _ns(id="resp-h-1", stop_reason="end_turn",
                   content=[_ns(type="text", text="ok")],
                   usage=payload_usage)
    model._client = _ns(messages=_ns(create=fake_create))
    return model, fake_create


def _run(coro):
    return asyncio.run(coro)


def test_haiku_breakpoints_do_not_alter_content():
    from codatiel_i2 import runtime_sdk as R
    import copy
    model, fake = _haiku_model(_ns(input_tokens=100, output_tokens=5,
                  cache_creation_input_tokens=0,
                  cache_read_input_tokens=0))
    tools = [_ns(name="read_file", description="r",
                 params_json_schema={"type": "object"})]
    R.require_sdk()
    out = _run(model.get_response(
        "sys", [{"role": "user", "content": "hi"}], None, tools, None,
        None, None, previous_response_id=None, conversation_id=None,
        prompt=None))
    kw = fake.kwargs
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"][-1]["content"][-1]["cache_control"] == \
        {"type": "ephemeral"}
    # Strip markers: content identical to the unmarked mapping.
    assert kw["messages"][-1]["content"][-1]["text"] == "hi"
    assert out.usage.input_tokens == 100  # no double count
    assert model.usage_details_for("resp-h-1") == {
        "cached_input_tokens": 0, "cache_write_input_tokens": 0,
        "cache_in_input": False}


def test_haiku_usage_split_recorded_without_double_count():
    from codatiel_i2 import runtime_sdk as R
    model, _ = _haiku_model(_ns(input_tokens=1000, output_tokens=50,
                  cache_creation_input_tokens=100,
                  cache_read_input_tokens=800))
    ledger = SpendLedger(rate={"input": 0.1, "output": 0.5,
                               "input_cache_hit": 0.01,
                               "input_cache_write": 0.125},
                         rate_status="t", ceiling_usd=5.0)

    # Record through the real wrapper over the real adapter, exactly
    # as production wires it: the adapter's per-response registry is the
    # authoritative cache split.
    _run(R.UsageRecordingModel(model, ledger).get_response(
        "sys", [{"role": "user", "content": "hi"}], None, [], None, None,
        None, previous_response_id=None, conversation_id=None, prompt=None))
    entry = ledger.entries["resp-h-1"]
    assert entry["cached_input_tokens"] == 800
    assert entry["cache_write_input_tokens"] == 100
    assert entry["cache_in_input"] is False  # Anthropic input is uncached-only
    # 1000*0.1 + 800*0.01 + 100*0.125 + 50*0.5, all per 1e6: no subtraction.
    expect = (1000 * 0.1 + 800 * 0.01 + 100 * 0.125 + 50 * 0.5) / 1e6
    assert ledger.confirmed_usd() == pytest.approx(expect)


def test_haiku_unknown_usage_never_confirmed_zero():
    from codatiel_i2 import runtime_sdk as R
    model, _ = _haiku_model(None)
    ledger = SpendLedger(rate={"input": 0.1, "output": 0.5},
                         rate_status="t", ceiling_usd=5.0)
    resp = _run(model.get_response(
        "sys", [{"role": "user", "content": "hi"}], None, [], None, None,
        None, previous_response_id=None, conversation_id=None, prompt=None))
    assert "resp-h-1" in model._unknown_usage_response_ids

    class Stub:
        # Mirrors the real adapter contract: provider-unusable responses
        # are published via _unknown_usage_response_ids.
        _unknown_usage_response_ids = {"resp-h-1"}

        async def get_response(self, *a, **k):
            return resp
    _run(R.UsageRecordingModel(Stub(), ledger).get_response())
    entry = ledger.entries["resp-h-1"]
    assert entry["usage_known"] is False
    assert entry["cached_input_tokens"] is None


# ------------------------------------------------------------------ no accidental inference

def test_prompt_cache_key_stable_per_review_unique_across():
    from codatiel_i2 import runtime_sdk as R
    k1 = R.prompt_cache_key_for(repo="o/r", base_sha="a" * 40,
                                head_sha="b" * 40,
                                instructions_id="x", model_name="luna:t")
    k2 = R.prompt_cache_key_for(repo="o/r", base_sha="a" * 40,
                                head_sha="b" * 40,
                                instructions_id="x", model_name="luna:t")
    k3 = R.prompt_cache_key_for(repo="o/r", base_sha="a" * 40,
                                head_sha="c" * 40,
                                instructions_id="x", model_name="luna:t")
    assert k1 == k2 and k1 != k3
    assert k1.startswith("codatiel-i2-") and len(k1) < 64


def test_with_prompt_cache_key_preserves_settings():
    from codatiel_i2 import runtime_sdk as R
    from agents.model_settings import ModelSettings
    base = ModelSettings(store=False)
    out = R.with_prompt_cache_key(base, "codatiel-i2-abc")
    assert out.extra_args["prompt_cache_key"] == "codatiel-i2-abc"
    assert out.store is False
    assert base.extra_args is None  # input untouched
    # Pre-existing key wins; never overwritten.
    out2 = R.with_prompt_cache_key(out, "codatiel-i2-other")
    assert out2.extra_args["prompt_cache_key"] == "codatiel-i2-abc"
    assert R.with_prompt_cache_key(base, None) is base


def test_status_and_pause_paths_make_no_model_calls(tmp_path, monkeypatch):
    from codatiel_i2 import cli as cli_mod
    from codatiel_i2 import service as svc_mod
    calls = []

    async def boom(*a, **k):
        calls.append(1)
        raise AssertionError("model must not be invoked")
    monkeypatch.setattr(svc_mod, "review_pr", boom)
    monkeypatch.setattr(svc_mod, "review_local", boom)
    parser = cli_mod.build_parser()
    out = cli_mod.cmd_watcher(parser.parse_args(
        ["watcher", "--state", str(tmp_path / "w.json"), "status"]))
    assert out["status"] in ("ok", "failed")
    out = cli_mod.cmd_watcher(parser.parse_args(
        ["watcher", "--state", str(tmp_path / "w.json"),
         "--repos", "o/r", "pause-repo"]))
    assert out["status"] == "paused"
    out = cli_mod.cmd_watcher(parser.parse_args(
        ["watcher", "--state", str(tmp_path / "w.json"),
         "--repos", "o/r", "resume-repo"]))
    assert out["status"] == "resumed"
    assert calls == []
