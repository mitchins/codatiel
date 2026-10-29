"""STABILIZE-02 R1 Stage A: tiered accounting correctness.

Tiered provider pricing (Luna short/long, Haiku standard/high), missing
telemetry stays unknown with bounded reservations, crash recovery
preserves provider semantics, explicit-zero vs absent classification,
unpriced categories can never defeat the ceiling.

Deterministic, no network, no inference.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from codatiel_i2.core.spend import (
    CACHE_HIT,
    CACHE_MISS,
    CACHE_UNSUPPORTED,
    CACHE_USAGE_UNAVAILABLE,
    PRICED,
    PRICING_PARTIAL,
    PRICING_UNAVAILABLE,
    SpendLedger,
    classify_cache,
)

PROFILES_PATH = Path(__file__).resolve().parent.parent / "codatiel_i2" / "config" / "model_profiles.json"


def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _card(backend):
    """Tiered rate card mirroring the shipped profiles (values asserted
    against the profile file itself in test_profile_* below)."""
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


def _profiles():
    return {p["profile"]: p for p in json.loads(PROFILES_PATH.read_text())["profiles"]}


# ------------------------------------------------------- profile grounding

def test_luna_profile_declares_standard_processing_tier():
    luna = _profiles()["luna_review"]
    assert luna["processing_tier"] == "standard"
    assert luna["rate_usd_per_mtok"]["processing_tier"] == "standard"


def test_active_write_rates_are_default_retention():
    # CACHE-01 uses default retention: Luna 30m (1.25x, not the 2.5x 12h
    # write) and Haiku 5m TTL (1.25x, not the 2x 1h write).
    ps = _profiles()
    luna = ps["luna_review"]["rate_usd_per_mtok"]
    assert luna["input_cache_write"] == pytest.approx(0.125)
    assert luna["input_cache_write_12h"] == pytest.approx(0.25)
    assert luna["tiers"]["short"]["input_cache_write"] == pytest.approx(0.125)
    assert luna["tiers"]["long"]["input_cache_write"] == pytest.approx(0.25)
    haiku = ps["haiku_review"]["rate_usd_per_mtok"]
    assert haiku["input_cache_write"] == pytest.approx(0.125)
    assert haiku["input_cache_write_1h"] == pytest.approx(0.2)
    assert haiku["tiers"]["high"]["input_cache_write"] == pytest.approx(0.625)


def test_rate_cards_cite_source_and_retrieval_date():
    ps = _profiles()
    for profile in ("luna_review", "haiku_review"):
        source = ps[profile]["rate_usd_per_mtok"]["rate_source"]
        assert source["docs_url"].startswith("https://")
        assert source["retrieved"] == "2026-10-26"
        assert "verified" in ps[profile]["rate_status"]


# ------------------------------------------------------- tier boundaries

def test_luna_tier_boundary_short_long():
    led = _ledger("luna")
    led.record_response("a", _ns(requests=1, input_tokens=271999, output_tokens=10),
                        cached_input_tokens=0, cache_write_input_tokens=0)
    led.record_response("b", _ns(requests=1, input_tokens=272000, output_tokens=10),
                        cached_input_tokens=0, cache_write_input_tokens=0)
    led.record_response("c", _ns(requests=1, input_tokens=272001, output_tokens=10),
                        cached_input_tokens=0, cache_write_input_tokens=0)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["a"]["tier"] == "short" and rows["a"]["pricing_status"] == PRICED
    assert rows["b"]["tier"] == "short" and rows["b"]["pricing_status"] == PRICED
    assert rows["c"]["tier"] == "long" and rows["c"]["pricing_status"] == PRICED
    expect = (271999 + 272000) / 1e6 * 0.1 + 272001 / 1e6 * 0.2 + 30 / 1e6 * 0.5
    # Long-context output is 1.5x; requests a/b outputs price at short.
    short_out = 20 / 1e6 * 0.5
    long_out = 10 / 1e6 * 0.75
    expect = (271999 + 272000) / 1e6 * 0.1 + 272001 / 1e6 * 0.2 + short_out + long_out
    assert led.confirmed_usd() == pytest.approx(expect)


def test_haiku_tier_boundary_standard_high():
    led = _ledger("haiku")
    # Mostly cache hits still count toward the prompt total: a 100001-token
    # prompt prices high even when 90000 of it is a read.
    led.record_response("a", _ns(requests=1, input_tokens=99999, output_tokens=10),
                        cached_input_tokens=0, cache_write_input_tokens=0,
                        cache_in_input=False)
    led.record_response("b", _ns(requests=1, input_tokens=90000, output_tokens=10),
                        cached_input_tokens=9000, cache_write_input_tokens=1000,
                        cache_in_input=False)
    led.record_response("c", _ns(requests=1, input_tokens=10001, output_tokens=10),
                        cached_input_tokens=90000, cache_write_input_tokens=0,
                        cache_in_input=False)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["a"]["tier"] == "standard"
    assert rows["b"]["tier"] == "standard"  # 90000+9000+1000 = 100000
    assert rows["c"]["tier"] == "high"  # 10001+90000 = 100001
    for row in rows.values():
        assert row["pricing_status"] == PRICED
    std = (99999 + 90000) / 1e6 * 0.1 + 9000 / 1e6 * 0.01 + 1000 / 1e6 * 0.125
    high = 10001 / 1e6 * 0.5 + 90000 / 1e6 * 0.05 + 20 / 1e6 * 0.5 + 10 / 1e6 * 2.5
    assert led.confirmed_usd() == pytest.approx(std + high)


def test_each_request_priced_in_its_own_tier():
    led = _ledger("haiku")
    led.record_response("small", _ns(requests=1, input_tokens=1000, output_tokens=10),
                        cached_input_tokens=0, cache_write_input_tokens=0,
                        cache_in_input=False)
    led.record_response("big", _ns(requests=1, input_tokens=150000, output_tokens=10),
                        cached_input_tokens=0, cache_write_input_tokens=0,
                        cache_in_input=False)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["small"]["tier"] == "standard"
    assert rows["big"]["tier"] == "high"
    expect = (1000 / 1e6 * 0.1 + 10 / 1e6 * 0.5
              + 150000 / 1e6 * 0.5 + 10 / 1e6 * 2.5)
    assert led.confirmed_usd() == pytest.approx(expect)


# ------------------------------------------------- missing stays unknown

def test_missing_luna_write_is_unavailable_with_bound():
    led = _ledger("luna")
    led.record_response("r1", _ns(requests=1, input_tokens=10000, output_tokens=100),
                        cached_input_tokens=8000,
                        cache_write_input_tokens=None)
    rec = led.to_record()
    rows = {r["response_id"]: r for r in rec["request_ledger"]}
    assert rows["r1"]["pricing_status"] == PRICING_UNAVAILABLE
    assert rows["r1"]["priced_cost_usd"] is None
    # Bound: whole prompt at the highest input-side rate (write 0.125).
    assert rows["r1"]["bounded_reserve_usd"] == pytest.approx(
        10000 / 1e6 * 0.125 + 100 / 1e6 * 0.5)
    assert rec["pricing_unavailable_ids"] == ["r1"]
    assert led.confirmed_usd() == pytest.approx(0.0)
    assert rec["bounded_reserve_usd"] == pytest.approx(rows["r1"]["bounded_reserve_usd"])
    assert rec["usd_is_confirmed_lower_bound"] is True


def test_missing_anthropic_split_is_partial_lower_bound():
    # SUPERSEDED by R1.1: the R1 bound priced only the known input, which
    # omits separately billed Anthropic cache lines and was therefore not
    # a maximum. A missing Anthropic split is PRICING_PARTIAL: the known
    # input is a lower bound, never a maximum.
    led = _ledger("haiku")
    led.record_response("r1", _ns(requests=1, input_tokens=5000, output_tokens=50),
                        cache_in_input=False)
    rec = led.to_record()
    rows = {r["response_id"]: r for r in rec["request_ledger"]}
    assert rows["r1"]["tier"] == "high"  # prompt size unknown: never cheap
    assert rows["r1"]["pricing_status"] == PRICING_PARTIAL
    assert rows["r1"]["bounded_reserve_usd"] is None
    lower = (5000 * 0.5 + 50 * 2.5) / 1e6
    assert led.confirmed_usd() == pytest.approx(lower)
    assert rec["pricing_partial_ids"] == ["r1"]
    assert rec["partial_lower_bound_usd"] == pytest.approx(lower)


def test_reported_zero_vs_absent_split():
    # Explicit zeros are measured (PRICED); absent Anthropic fields are
    # PRICING_PARTIAL lower bounds (R1.1: never a maximum).
    led = _ledger("haiku")
    led.record_response("zero", _ns(requests=1, input_tokens=1000, output_tokens=10),
                        cached_input_tokens=0, cache_write_input_tokens=0,
                        cache_in_input=False)
    led.record_response("absent", _ns(requests=1, input_tokens=1000, output_tokens=10),
                        cache_in_input=False)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["zero"]["pricing_status"] == PRICED
    assert rows["zero"]["priced_cost_usd"] == pytest.approx(
        1000 / 1e6 * 0.1 + 10 / 1e6 * 0.5)
    assert rows["absent"]["pricing_status"] == PRICING_PARTIAL
    assert rows["absent"]["bounded_reserve_usd"] is None
    assert led.confirmed_usd() == pytest.approx(
        rows["zero"]["priced_cost_usd"] + rows["absent"]["priced_cost_usd"])


def test_malformed_cache_fields_become_absent_not_zero():
    led = _ledger("luna")
    led.record_response("r1", _ns(requests=1, input_tokens=1000, output_tokens=10),
                        cached_input_tokens="lots",
                        cache_write_input_tokens={"n": 3})
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert led.entries["r1"]["cached_input_tokens"] is None
    assert rows["r1"]["pricing_status"] == PRICING_UNAVAILABLE
    assert led.confirmed_usd() == pytest.approx(0.0)


def test_partial_record_read_without_write():
    led = _ledger("luna")
    led.record_response("r1", _ns(requests=1, input_tokens=5000, output_tokens=10),
                        cached_input_tokens=4000, cache_write_input_tokens=None)
    rec = led.to_record()
    assert rec["pricing_unavailable_ids"] == ["r1"]
    assert led.confirmed_usd() == pytest.approx(0.0)


# ------------------------------------------------------- classification

def test_classify_explicit_zero_is_miss_absent_is_unavailable():
    assert classify_cache({"usage_known": True, "cached_input_tokens": 0,
                           "cache_write_input_tokens": 0}, True) == CACHE_MISS
    assert classify_cache({"usage_known": True, "cached_input_tokens": 0,
                           "cache_write_input_tokens": 500}, True) == CACHE_MISS
    assert classify_cache({"usage_known": True, "cached_input_tokens": 500,
                           "cache_write_input_tokens": 0}, True) == CACHE_HIT
    assert classify_cache({"usage_known": True, "cached_input_tokens": None,
                           "cache_write_input_tokens": 500}, True) == CACHE_USAGE_UNAVAILABLE
    assert classify_cache({"usage_known": True, "cached_input_tokens": None,
                           "cache_write_input_tokens": None}, True) == CACHE_USAGE_UNAVAILABLE
    assert classify_cache({"usage_known": True, "cached_input_tokens": 0,
                           "cache_write_input_tokens": 0}, False) == CACHE_UNSUPPORTED
    assert classify_cache(None, True) == CACHE_USAGE_UNAVAILABLE


# --------------------------------------------------- crash recovery (A3)

def test_sink_round_trip_preserves_provider_semantics(tmp_path):
    for backend, in_input in (("luna", True), ("haiku", False)):
        led = _ledger(backend)
        sink = tmp_path / f"sink-{backend}.jsonl"
        led.attach_sink(sink)
        led.record_response("r1", _ns(requests=1, input_tokens=2000, output_tokens=20),
                            cached_input_tokens=500, cache_write_input_tokens=100,
                            cache_in_input=in_input)
        before_cost = led.confirmed_usd()
        before_record = led.to_record()
        restored = _ledger(backend)
        restored.restore_sink(sink)
        assert restored.entries["r1"]["cache_in_input"] is in_input
        assert restored.confirmed_usd() == pytest.approx(before_cost)
        assert restored.to_record()["usage"] == before_record["usage"]
        assert (restored.to_record()["cached_input_tokens"]
                == before_record["cached_input_tokens"])
        # Idempotent: restoring twice changes nothing.
        restored.restore_sink(sink)
        assert restored.confirmed_usd() == pytest.approx(before_cost)
        assert len(restored.entries) == 1


def test_sink_round_trip_keeps_unknown_marking(tmp_path):
    led = _ledger("haiku")
    sink = tmp_path / "sink-unk.jsonl"
    led.attach_sink(sink)
    led.record_response("r1", _ns(requests=1, input_tokens=100, output_tokens=10),
                        cached_input_tokens=10, cache_write_input_tokens=5,
                        cache_in_input=False)
    led.mark_unknown("r1")
    restored = _ledger("haiku")
    restored.restore_sink(sink)
    assert restored.entries["r1"]["usage_known"] is False
    assert restored.confirmed_usd() == pytest.approx(0.0)


# ------------------------------------------------- unpriced categories

def test_missing_write_rate_marks_unavailable_not_input_rate():
    card = _card("luna")
    del card["tiers"]["short"]["input_cache_write"]
    led = SpendLedger(rate=card, rate_status="tiered-test", ceiling_usd=5.0)
    led.record_response("r1", _ns(requests=1, input_tokens=2000, output_tokens=10),
                        cached_input_tokens=500, cache_write_input_tokens=1000)
    rows = {r["response_id"]: r for r in led.to_record()["request_ledger"]}
    assert rows["r1"]["pricing_status"] == PRICING_UNAVAILABLE
    assert rows["r1"]["priced_cost_usd"] is None
    assert led.confirmed_usd() == pytest.approx(0.0)
    # The write slice must not inherit the 0.1 input rate: the bound prices
    # every slice at the tier's highest documented rate (>= write premium).
    bound = rows["r1"]["bounded_reserve_usd"]
    flat_fallback = 500 / 1e6 * 0.1 + 1000 / 1e6 * 0.1 + 500 / 1e6 * 0.1
    assert bound > flat_fallback


def test_missing_pricing_cannot_defeat_the_ceiling():
    card = _card("haiku")
    del card["tiers"]["standard"]["input_cache_write"]
    led = SpendLedger(rate=card, rate_status="tiered-test", ceiling_usd=0.0001)
    led.record_response("r1", _ns(requests=1, input_tokens=5000, output_tokens=10),
                        cached_input_tokens=1000, cache_write_input_tokens=4000,
                        cache_in_input=False)
    rec = led.to_record()
    assert rec["pricing_unavailable_ids"] == ["r1"]
    assert led.confirmed_usd() == pytest.approx(0.0)
    assert led.over_ceiling() is True
    assert rec["exceeds_ceiling"] is True


def test_confirmed_only_ceiling_parity_for_legacy_ledgers():
    # Legacy flat ledgers (no tier card) keep the historical confirmed-only
    # check: over_ceiling() matches confirmed_usd() > ceiling exactly.
    led = SpendLedger(rate={"input": 0.10, "output": 0.50},
                      rate_status="priced", ceiling_usd=5.0)
    assert led.over_ceiling() is False
    led.record_response("r1", _ns(requests=1, input_tokens=100, output_tokens=10))
    assert led.over_ceiling() is False
    assert led.to_record()["exceeds_ceiling"] is False
