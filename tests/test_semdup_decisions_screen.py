"""Tests for the Decisions screening gate (DECISIONS-03).

No network: all provider calls are injected via post_fn. Verifies the
frozen routing table, fail-open behaviour, cache exclusion, flag parsing,
spend accounting and priority ordering.
"""
import os

import pytest

from codatiel_i2.semdup_dev import decisions_screen as DS
from codatiel_i2.semdup_dev import cache as CACHE


def _pair(pid="spair-1", rank=0):
    return {"pair_id": pid, "pair_rank": rank,
            "target": {"path": "a.py", "symbol": "f",
                       "start_line": 1, "end_line": 10},
            "counterpart": {"path": "b.py", "symbol": "g",
                            "start_line": 1, "end_line": 10},
            "head_rev": "abc123"}


def _ev(pair):
    return "def f(): return 1", "def g(): return 1"


def _ok(p1, p2, usage=None):
    usage = usage if usage is not None else {
        "input_tokens": 500,
        "input_tokens_details": {"cached_tokens": 0,
                                 "cache_write_tokens": 0},
        "output_tokens": 0, "total_tokens": 500}
    return {"ok": True,
            "body": {"model": "gpt-6-luna",
                     "answers": [
                         {"type": "predicate", "name": "plausible_overlap",
                          "probability": p1},
                         {"type": "predicate", "name": "unsafe_to_dismiss",
                          "probability": p2}],
                     "usage": usage},
            "wall_s": 0.2}


def _fail():
    return {"ok": False, "error": "timeout", "wall_s": 1.0}


def test_off_is_baseline(monkeypatch):
    monkeypatch.delenv("SEMDUP_DECISIONS_SCREEN", raising=False)
    assert DS.screen_mode() == "off"
    pairs = [_pair()]
    out = DS.screen_pairs(pairs, mode="off", head_rev="abc",
                          evidence_fn=_ev,
                          post_fn=lambda k, t: (_ for _ in ()).throw(
                              AssertionError("must not call provider")))
    assert out["advanced"] == pairs
    assert out["screened"] == []
    assert out["screen_calls"] == 0


def test_invalid_flag_means_off(monkeypatch):
    monkeypatch.setenv("SEMDUP_DECISIONS_SCREEN", "turbo")
    assert DS.screen_mode() == "off"
    for bad in ("ECO", " Eco ", "1", "yes"):
        monkeypatch.setenv("SEMDUP_DECISIONS_SCREEN", bad)
        assert DS.screen_mode() == ("eco" if bad.strip().lower() == "eco"
                                    else "off")


def test_eco_routing_table():
    assert DS.should_skip(0.29, 0.9, False, "eco") is True
    assert DS.should_skip(0.30, 0.0, False, "eco") is False
    assert DS.should_skip(0.0, 0.0, False, "eco") is True
    assert DS.should_skip(0.1, 0.1, True, "eco") is False  # fail-open
    assert DS.should_skip(None, 0.1, False, "eco") is False
    assert DS.should_skip(0.29, 0.9, False, "eco-protected") is False
    assert DS.should_skip(0.29, 0.59, False, "eco-protected") is True
    assert DS.should_skip(0.29, 0.6, False, "eco-protected") is False
    assert DS.should_skip(0.1, 0.1, False, "priority") is True
    assert DS.should_skip(0.5, 0.5, False, "priority") is False


def test_parse_answer_fail_open():
    assert parse_failed(DS.parse_answer(_fail())) is True
    refusal = {"ok": True, "body": {"answers": [
        {"type": "refusal", "name": "plausible_overlap"},
        {"type": "predicate", "name": "unsafe_to_dismiss",
         "probability": 0.8}]}, "wall_s": 0.1}
    # Single-question refusal: parsed without failure flag, but the None
    # probability fail-opens at the routing layer (never skipped).
    parsed = DS.parse_answer(refusal)
    assert parsed["p_overlap"] is None
    assert DS.should_skip(parsed["p_overlap"], parsed["p_unsafe"], False,
                           "eco") is False
    incomplete = {"ok": True, "body": {"answers": [
        {"type": "predicate", "name": "plausible_overlap",
         "probability": 0.1}]}, "wall_s": 0.1}
    assert parse_failed(DS.parse_answer(incomplete)) is True
    wrong = {"ok": True, "body": {"answers": [
        {"type": "predicate", "name": "something_else",
         "probability": 0.1},
        {"type": "predicate", "name": "unsafe_to_dismiss",
         "probability": 0.1}]}, "wall_s": 0.1}
    assert parse_failed(DS.parse_answer(wrong)) is True
    good = DS.parse_answer(_ok(0.2, 0.7))
    assert good == {"p_overlap": 0.2, "p_unsafe": 0.7, "failed": False,
                    "error": None}


def parse_failed(parsed):
    return parsed["failed"]


def test_screen_end_to_end_eco_advances_failures():
    pairs = [_pair("a", 0), _pair("b", 1), _pair("c", 2)]
    calls = {"n": 0}

    def post(key, text):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok(0.1, 0.1)  # skip
        if calls["n"] == 2:
            return _ok(0.9, 0.9)  # advance
        return _fail()  # fail-open advance

    out = DS.screen_pairs(pairs, mode="eco", head_rev="abc",
                          evidence_fn=_ev, post_fn=post)
    assert [p["pair_id"] for p in out["advanced"]] == ["b", "c"]
    assert [p["pair_id"] for p in out["screened"]] == ["a"]
    assert out["screen_calls"] == 3
    assert out["screen_failures"] == 1
    row = out["screened"][0]
    assert row["reason"] == DS.SCREEN_REASON
    assert row["completed"] is False
    assert row["assess_ran"] is False
    assert row["pair_id"] == "a" and row["pair_rank"] == 0
    # The failed third call carries no provider usage: batch spend is
    # unconfirmable (unknown, never confirmed $0), even though two calls
    # priced fine.
    assert out["screen_spend_usd"] is None
    assert out["screen_spend_unknown"] is True


def test_screened_rows_never_cacheable():
    row = {"reason": DS.SCREEN_REASON, "completed": False}
    assert CACHE.is_completed_row(row) is False
    # Even if a future writer forgets the flag, the reason alone blocks.
    assert CACHE.is_completed_row({"reason": DS.SCREEN_REASON}) is False


def test_protected_keeps_high_p2():
    pairs = [_pair("a", 0)]

    def post(key, text):
        return _ok(0.2, 0.7)

    out = DS.screen_pairs(pairs, mode="eco-protected", head_rev="abc",
                          evidence_fn=_ev, post_fn=post)
    assert [p["pair_id"] for p in out["advanced"]] == ["a"]
    assert out["screened"] == []
    out2 = DS.screen_pairs(pairs, mode="eco", head_rev="abc",
                           evidence_fn=_ev, post_fn=post)
    assert out2["screened"] and [p["pair_id"] for p in out2["screened"]] == ["a"]


def test_priority_orders_risk_first_then_score():
    pairs = [_pair("low", 0), _pair("mid", 1), _pair("risky", 2)]
    scripted = iter([(0.1, 0.1), (0.5, 0.5), (0.4, 0.9)])

    def post2(key, text):
        a, b = next(scripted)
        return _ok(a, b)

    out = DS.screen_pairs(pairs, mode="priority", head_rev="abc",
                          evidence_fn=_ev, post_fn=post2)
    # protected rule skips low (p1<0.3, p2<0.6); risky band first.
    assert [p["pair_id"] for p in out["screened"]] == ["low"]
    assert [p["pair_id"] for p in out["advanced"]][0] == "risky"


def test_ceiling_preflight_defers_screen():
    pairs = [_pair()]

    def post(key, text):
        raise AssertionError("preflight should have deferred")

    out = DS.screen_pairs(pairs, mode="eco", head_rev="abc",
                          evidence_fn=_ev, post_fn=post,
                          ceiling_remaining=0.0000001)
    assert out["advanced"] == pairs
    assert out.get("screen_deferred") is True
    assert out["screen_calls"] == 0


def test_missing_evidence_advances():
    pairs = [_pair()]

    def post(key, text):
        raise AssertionError("no evidence: must not call")

    out = DS.screen_pairs(pairs, mode="eco", head_rev="abc",
                          evidence_fn=lambda p: ("", ""),
                          post_fn=post)
    # No-evidence pairs advance untouched in membership, tagged for audit.
    assert [p["pair_id"] for p in out["advanced"]] == ["spair-1"]
    assert out["advanced"][0]["screen_test_involved"] is False
    assert out["screened"] == []


def test_price_usage_cached_and_write():
    assert DS.price_usage({}) == 0.0
    assert DS.price_usage({"input_tokens": 1000,
                           "input_tokens_details": {"cached_tokens": 1000,
                                                    "cache_write_tokens": 0}}
                          ) == pytest.approx(1000 * DS.RATE_CACHED)
    # OpenAI-family semantics: writes are inside input_tokens, so the
    # uncached slice subtracts reads AND writes (core/spend.py).
    assert DS.price_usage({"input_tokens": 1000,
                           "input_tokens_details": {"cached_tokens": 200,
                                                    "cache_write_tokens": 100}}
                          ) == pytest.approx(700 * DS.RATE_IN + 200 *
                                             DS.RATE_CACHED + 100 *
                                             DS.RATE_WRITE)


def test_price_usage_provider_card():
    card = {"input": 0.1, "input_cache_hit": 0.01,
            "input_cache_write": 0.125}
    assert DS.price_usage({"input_tokens": 1000,
                           "input_tokens_details": {"cached_tokens": 0,
                                                    "cache_write_tokens": 0}},
                          card) == pytest.approx(1000 * 0.10 / 1e6)
    assert DS.price_usage({"input_tokens": 1000,
                           "input_tokens_details": {"cached_tokens": 200,
                                                    "cache_write_tokens": 100}},
                          {"tiers": {"short": card}}) == pytest.approx(
        700 * 0.10 / 1e6 + 200 * 0.01 / 1e6 + 100 * 0.125 / 1e6)
    assert DS.price_usage({"input_tokens": 100}, {}) == pytest.approx(
        100 * DS.RATE_IN)


def test_q2_refusal_or_missing_never_skips():
    # B1: unknown safety is treated as unsafe — never suppress.
    assert DS.should_skip(0.1, None, False, "eco-protected") is False
    assert DS.should_skip(0.1, None, False, "priority") is False
    assert DS.should_skip(0.29, None, False, "eco-protected") is False
    assert DS.should_skip(0.29, 0.59, False, "eco-protected") is True
    assert DS.should_skip(0.1, 0.2, False, "eco") is True  # eco: Q1 only
    assert DS.priority_score(0.5, None) == pytest.approx(
        0.75 * 0.5 + 0.25 * 1.0)


def test_missing_usage_marks_spend_unknown():
    pairs = [_pair("a", 0)]

    def post(key, text):
        return {"ok": True, "body": {"model": "x", "answers": [
            {"type": "predicate", "name": "plausible_overlap",
             "probability": 0.1},
            {"type": "predicate", "name": "unsafe_to_dismiss",
             "probability": 0.1}]}, "wall_s": 0.2}  # no usage block

    out = DS.screen_pairs(pairs, mode="eco", head_rev="abc",
                          evidence_fn=_ev, post_fn=post)
    assert out["screen_calls"] == 1
    assert out["screen_spend_usd"] is None
    assert out["screen_spend_unknown"] is True
    assert [p["pair_id"] for p in out["screened"]] == ["a"]


def test_no_key_fails_open_locally(monkeypatch):
    monkeypatch.delenv("SEMDUP_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pairs = [_pair("a", 0), _tpair("t1", path="tests/x.py",
                                   symbol="test_y")]

    def post(key, text):
        raise AssertionError("must not issue network calls without a key")

    out = DS.screen_pairs(pairs, mode="eco", head_rev="abc",
                          evidence_fn=_ev, post_fn=None)
    # post_fn=None forces the real path, which must refuse without a key.
    assert [p["pair_id"] for p in out["advanced"]] == ["a", "t1"]
    assert out["screened"] == []
    assert out["screen_calls"] == 0
    assert out["screen_no_key"] is True


def test_production_symbols_not_demoted():
    for sym in ("testing", "testament", "testify", "contest"):
        assert DS.pair_test_involved(
            _tpair("x", path="src/lib.py", symbol=sym)) is False
    for sym in ("test", "test_x", "Test"):
        assert DS.pair_test_involved(
            _tpair("x", path="src/lib.py", symbol=sym)) is True


def _tpair(pid, path="src/pay.py", symbol="charge", rank=0):
    return {"pair_id": pid, "pair_rank": rank,
            "target": {"path": path, "symbol": symbol,
                       "start_line": 1, "end_line": 10},
            "counterpart": {"path": "src/other.py", "symbol": "helper",
                            "start_line": 1, "end_line": 10},
            "head_rev": "abc123"}


def test_pair_test_involved():
    assert DS.pair_test_involved(
        _tpair("x", path="tests/test_pay.py")) is True
    assert DS.pair_test_involved(
        _tpair("x", symbol="test_charge")) is True
    assert DS.pair_test_involved(
        _tpair("x", path="src/pay.py", symbol="charge")) is False
    assert DS.pair_test_involved({}) is False


def test_priority_demotes_test_pairs():
    # Same scores: production pair must precede the test-involved one in
    # both the risk band and the main band; nothing is suppressed.
    prod = _tpair("prod", path="src/pay.py", symbol="charge")
    tst = _tpair("tst", path="tests/test_pay.py", symbol="test_charge")

    def post(key, text):
        return _ok(0.9, 0.9)

    out = DS.screen_pairs([tst, prod], mode="priority", head_rev="abc",
                          evidence_fn=_ev, post_fn=post)
    assert [p["pair_id"] for p in out["advanced"]][:2] == ["prod", "tst"]
    assert out["screened"] == []
    assert out["advanced"][0]["screen_test_involved"] is False
    assert out["advanced"][1]["screen_test_involved"] is True


def test_priority_exploration_uniform_bottom_quartile():
    # 10 production + 10 test pairs, equal scores: main order is
    # production-first; the 2 exploration slots (10%) come from the
    # bottom quartile; the seed is deterministic across runs.
    pairs = ([_tpair(f"p{i}", path="src/m.py", symbol=f"f{i}")
              for i in range(10)]
             + [_tpair(f"t{i}", path="tests/test_m.py",
                       symbol=f"test_f{i}") for i in range(10)])

    def post(key, text):
        return _ok(0.5, 0.5)

    def run():
        return DS.screen_pairs(list(pairs), mode="priority", head_rev="abc",
                               evidence_fn=_ev, post_fn=post)["advanced"]
    a, b = run(), run()
    assert [p["pair_id"] for p in a] == [p["pair_id"] for p in b]
    assert len(a) == 20
    assert all(not p.get("screen_test_involved") for p in a[:10])
    assert all(p.get("screen_test_involved") for p in a[10:])


def test_priority_risk_band_never_delayed():
    risky = _tpair("risky", path="src/auth.py", symbol="check")
    lows = [_tpair(f"l{i}", path="src/m.py", symbol=f"f{i}")
            for i in range(12)]
    scores = iter([(0.9, 0.9)] + [(0.5, 0.5)] * 12)

    def post(key, text):
        a, b = next(scores)
        return _ok(a, b)

    out = DS.screen_pairs([risky] + lows, mode="priority", head_rev="abc",
                          evidence_fn=_ev, post_fn=post)
    assert out["advanced"][0]["pair_id"] == "risky"


def test_order_published_production_and_keep_first():
    from codatiel_i2.semdup_dev.stage import _order_published
    rows = [
        {"target": {"path": "tests/test_a.py", "symbol": "test_x"},
         "counterpart": {"path": "tests/test_b.py", "symbol": "test_y"},
         "decision": "KEEP"},
        {"target": {"path": "src/a.py", "symbol": "f"},
         "counterpart": {"path": "src/b.py", "symbol": "g"},
         "decision": "REVISE"},
        {"target": {"path": "src/c.py", "symbol": "h"},
         "counterpart": {"path": "src/d.py", "symbol": "i"},
         "decision": "KEEP"},
    ]
    ordered = _order_published(rows)
    assert [(r["target"]["path"], r["decision"]) for r in ordered] == [
        ("src/c.py", "KEEP"), ("src/a.py", "REVISE"),
        ("tests/test_a.py", "KEEP")]


def test_order_mode_never_calls_never_skips(monkeypatch):
    monkeypatch.delenv("SEMDUP_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pairs = [_tpair("t1", path="tests/test_a.py", symbol="test_x"),
             _tpair("p1", path="src/a.py", symbol="f"),
             _tpair("p2", path="src/b.py", symbol="g")]

    def post(key, text):
        raise AssertionError("order mode must not call the provider")

    out = DS.screen_pairs(list(pairs), mode="order", head_rev="abc",
                          evidence_fn=lambda p: (_ for _ in ()).throw(
                              AssertionError("no evidence fetch needed")),
                          post_fn=post)
    assert [p["pair_id"] for p in out["advanced"]] == ["p1", "p2", "t1"]
    assert out["screened"] == []
    assert out["screen_calls"] == 0
    assert out["screen_spend_usd"] == 0.0
    assert all(p["screen_mode"] == "order" for p in out["advanced"])
    assert out["advanced"][0]["screen_test_involved"] is False
    assert out["advanced"][-1]["screen_test_involved"] is True


def test_order_mode_empty_and_stable():
    assert DS.screen_pairs([], mode="order", head_rev="x")["advanced"] == []
    a = _tpair("a", path="src/a.py", symbol="f")
    b = _tpair("b", path="src/b.py", symbol="g")
    out = DS.screen_pairs([a, b], mode="order", head_rev="x")
    assert [p["pair_id"] for p in out["advanced"]] == ["a", "b"]


def test_confirmed_spend_sums_priced_calls():
    pairs = [_pair("a", 0), _pair("b", 1)]

    def post(key, text):
        return _ok(0.9, 0.9)

    out = DS.screen_pairs(pairs, mode="eco", head_rev="abc",
                          evidence_fn=_ev, post_fn=post)
    assert out["screen_spend_usd"] == pytest.approx(2 * 500 * DS.RATE_IN)
    assert out["screen_spend_unknown"] is False


def test_q2_refusal_advances_in_priority():
    pairs = [_pair("a", 0)]

    def post(key, text):
        return {"ok": True, "body": {"model": "x", "answers": [
            {"type": "predicate", "name": "plausible_overlap",
             "probability": 0.1},
            {"type": "refusal", "name": "unsafe_to_dismiss"}]},
                "wall_s": 0.2}

    out = DS.screen_pairs(pairs, mode="priority", head_rev="abc",
                          evidence_fn=_ev, post_fn=post)
    assert [p["pair_id"] for p in out["advanced"]] == ["a"]
    assert out["screened"] == []
    # Unknown safety ranks first (max risk), not last.
    assert out["advanced"][0]["screen_p_unsafe"] is None
