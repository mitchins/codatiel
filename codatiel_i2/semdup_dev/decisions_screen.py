"""SEMDUP Decisions screening (DECISIONS-03 promotion artifact).

Cheap pre-screen: one POST /v1/decisions call per candidate pair
(model gpt-6-luna, two frozen predicates). Low-scoring pairs are
recorded as screened and never reach Terra; everything else keeps the
existing Terra -> Luna path unchanged.

Safety properties (all tested in tests/test_semdup_decisions_screen.py):
- Default OFF: with the flag unset/invalid the pair list is untouched.
- Fail-open: API errors, refusals, malformed/incomplete answers and
  timeouts advance every pair (baseline behaviour, never a silent drop).
  A refused/missing UNSAFE answer never suppresses in eco-protected/
  priority (unknown safety is treated as unsafe); unknown Q1 never
  suppresses anywhere. ("eco" consults Q1 alone by frozen design and
  has no Q2 fail-open question.)
- Screen spend comes out of the same review ceiling: batch preflight
  plus a per-call hard stop keep screen + review within ceiling_usd.
- No API key configured also fails open locally WITHOUT issuing calls:
  screening degrades to the ordering-only path (pairs still demote and
  the presentation order still applies when the mode is enabled).
- Adversarial note: candidate source text travels inside the Decisions
  request, so a malicious PR could in principle shape its own scores to
  dodge Terra. Screening is therefore a cost control, never a verdict —
  screened rows are "not examined, not rejected", and any suppression
  mode stays HELD until the E-sample adjudicates yield.
- Screened rows are NOT verdicts: they carry completed=False and the
  reason is listed in cache.is_completed_row's never-cache set, so they
  can never seed continuation or cross-revision reuse.
- Screen spend is priced with the provider-confirmed Luna rate card and
  recorded in spend_semdup_decisions.jsonl, included in spend_usd.
- Screen batch preflight respects the remaining ceiling; if the screen
  itself does not fit, everything advances (baseline spend profile).

Modes (SEMDUP_DECISIONS_SCREEN): off (default), eco, eco-protected,
priority, order (ordering-only: no calls, no skips). Thresholds frozen per DECISIONS-03 FROZEN03.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error

SCREEN_VERSION = "decisions-screen-0.1"
DECISIONS_URL = "https://api.openai.com/v1/decisions"
DECISIONS_MODEL = "gpt-6-luna"

Q_OVERLAP = (
    "Do these two implementations exhibit a plausible, non-trivial overlap "
    "in behaviour, policy or responsibility for which reuse, consolidation "
    "or consistency investigation could be valuable? Include short but "
    "semantically important guards; exclude superficial similarity alone."
)
Q_UNSAFE = (
    "Could differences in state handling, security, verification, "
    "lifecycle, ownership, recovery, or other context make dismissing this "
    "candidate without further investigation unsafe?"
)

# Frozen operating points (FROZEN03) plus DECISIONS-04 ordering-only mode.
# "order" performs NO Decisions calls and NEVER skips: it applies the
# production-first demotion to the incoming credibility order and tags rows
# for audit/presentation. Zero suppression risk, zero marginal cost, works
# with no API key. This is the only mode enablable without the E-sample.
MODES = ("off", "eco", "eco-protected", "priority", "order")
ECO_P1 = 0.3
PROTECTED_P1 = 0.3
PROTECTED_P2 = 0.6
RISK_BAND_P2 = 0.6
PRIORITY_W1 = 0.75
EXPLORATION_FRAC = 0.10

# Provider-confirmed Standard short-context Luna rates (per 1M tokens).
RATE_IN = 0.10 / 1e6
RATE_CACHED = 0.01 / 1e6
RATE_WRITE = 0.125 / 1e6

SCREEN_REASON = "decisions_screened"

# DECISIONS-04: local test-code detector (path/symbol only, $0, no text
# needed). Test-involved pairs are DEMOTED in priority ordering, never
# suppressed: all 23 independently-audited useful records are non-test,
# so demotion cannot displace a confirmed-useful pair by construction.
# Domain flags do NOT advance rows in-band (replay showed flagged-boosts
# displacing confirmed-useful ordinary rows).
_TEST_PATH = re.compile(
    r"(^|/)(test|tests|spec|specs|mock|mocks|fixture|fixtures|stub|stubs"
    r"|e2e)(/|_|\.|$)|_test\.|_spec\.|conftest|Test\.swift|(^|/)Tests?/",
    re.IGNORECASE)
# Word-boundary on "test": production symbols like testing/testament/
# testify must NOT demote (review finding: bare ^test prefix overmatched).
_TEST_SYM = re.compile(
    r"^(test_|test(?![A-Za-z])|spec_|should_|mock_|fake_|stub_)",
    re.IGNORECASE)


def pair_test_involved(pair: dict) -> bool:
    """True when either side looks like test/mock/fixture code."""
    try:
        t = pair.get("target") or {}
        c = pair.get("counterpart") or {}
        return bool(
            _TEST_PATH.search(str(t.get("path") or ""))
            or _TEST_PATH.search(str(c.get("path") or ""))
            or _TEST_SYM.search(str(t.get("symbol") or ""))
            or _TEST_SYM.search(str(c.get("symbol") or "")))
    except Exception:
        return False


def screen_mode() -> str:
    """Independently controllable flag; anything unrecognized means off."""
    mode = (os.environ.get("SEMDUP_DECISIONS_SCREEN") or "off").strip().lower()
    return mode if mode in MODES else "off"


def render_screen_input(*, head_rev: str, target: dict, counterpart: dict,
                        target_text: str, counterpart_text: str) -> str:
    return "\n".join([
        f"Reviewed revision: {head_rev} (both sources below are pinned to "
        "this revision).",
        "Change role: the NEW/CHANGED implementation is the first one.",
        "Note: static evidence only; no follow-up source fetching is "
        "available to this screen.",
        "",
        f"## New/changed implementation: {target.get('path')}::"
        f"{target.get('symbol')} (lines {target.get('start_line')}-"
        f"{target.get('end_line')})",
        "```", target_text, "```",
        "",
        f"## Existing implementation: {counterpart.get('path')}::"
        f"{counterpart.get('symbol')} (lines {counterpart.get('start_line')}-"
        f"{counterpart.get('end_line')})",
        "```", counterpart_text, "```",
    ])


def _post_decision(api_key: str, input_text: str,
                   timeout_s: float = 60.0) -> dict:
    payload = json.dumps({
        "model": DECISIONS_MODEL,
        "input": input_text,
        "questions": [
            {"type": "predicate", "name": "plausible_overlap",
             "instructions": Q_OVERLAP},
            {"type": "predicate", "name": "unsafe_to_dismiss",
             "instructions": Q_UNSAFE},
        ],
    }).encode()
    req = urllib.request.Request(
        DECISIONS_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.load(resp)
        return {"ok": True, "body": body,
                "wall_s": round(time.monotonic() - t0, 3)}
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            detail = ""
        return {"ok": False, "error": f"http_{exc.code}", "detail": detail,
                "wall_s": round(time.monotonic() - t0, 3)}
    except Exception as exc:  # timeouts, DNS, TLS: fail open downstream
        return {"ok": False, "error": type(exc).__name__,
                "detail": str(exc)[:200],
                "wall_s": round(time.monotonic() - t0, 3)}


def parse_answer(res: dict) -> dict:
    """Extract (p_overlap, p_unsafe, failed). Fail-open: anything
    unexpected (transport failure, refusal, malformed or incomplete
    answer set) reports failed=True and the caller advances the pair."""
    if not res.get("ok"):
        return {"p_overlap": None, "p_unsafe": None, "failed": True,
                "error": res.get("error")}
    probs: dict = {}
    try:
        answers = res["body"].get("answers")
        if not isinstance(answers, list):
            raise ValueError("answers-not-list")
        for ans in answers:
            if not isinstance(ans, dict):
                raise ValueError("answer-not-dict")
            if ans.get("type") == "refusal":
                probs[ans.get("name")] = None
            elif (ans.get("type") == "predicate"
                    and ans.get("name") in ("plausible_overlap",
                                            "unsafe_to_dismiss")):
                probs[ans.get("name")] = float(ans.get("probability"))
            else:
                raise ValueError("unexpected-answer")
        if set(probs) != {"plausible_overlap", "unsafe_to_dismiss"}:
            raise ValueError("incomplete-answer-set")
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"p_overlap": None, "p_unsafe": None, "failed": True,
                "error": "malformed-answer"}
    return {"p_overlap": probs["plausible_overlap"],
            "p_unsafe": probs["unsafe_to_dismiss"], "failed": False,
            "error": None}


def _normalize_rate(card: dict | None) -> dict:
    """Map a provider rate card to {rate_in, rate_cached, rate_write}.

    Provider cards (model_profiles.json) use input/input_cache_hit/
    input_cache_write, possibly under tiers.short (Decisions inputs are
    always short-context). Falls back to the module Luna card. Tier
    note: provider semantics include cache reads AND writes inside
    input_tokens (verified live), so the uncached slice subtracts both.
    """
    base = {"rate_in": RATE_IN, "rate_cached": RATE_CACHED,
            "rate_write": RATE_WRITE}
    if not isinstance(card, dict):
        return base
    tier = ((card.get("tiers") or {}).get("short") or {})
    src = tier or card
    try:
        # Card values are USD per 1M tokens; normalize to USD per token.
        return {"rate_in": float(src.get("input", base["rate_in"] * 1e6))
                / 1e6,
                "rate_cached": float(src.get(
                    "input_cache_hit", base["rate_cached"] * 1e6)) / 1e6,
                "rate_write": float(src.get(
                    "input_cache_write", base["rate_write"] * 1e6)) / 1e6}
    except (TypeError, ValueError):
        return base


def price_usage(usage: dict, rate: dict | None = None) -> float:
    """Price one Decisions response in USD.

    ``rate`` is a resolved provider card (model_profiles shape) or an
    already-normalized {rate_in, rate_cached, rate_write} mapping;
    without it the module Luna card is used, so the reported total and
    the spend ledger agree. OpenAI-family semantics (cache_in_input=True):
    cache-write tokens are already inside input_tokens, so the uncached
    slice subtracts reads AND writes — matching core/spend.py. Unknown /
    missing usage prices to None upstream (callers must propagate unknown,
    never confirm $0).
    """
    if isinstance(rate, dict) and "rate_in" in rate:
        r = rate
    else:
        r = _normalize_rate(rate)
    n = (usage or {}).get("input_tokens") or 0
    det = ((usage or {}).get("input_tokens_details") or {})
    cached = det.get("cached_tokens") or 0
    written = det.get("cache_write_tokens") or 0
    # Short tier assumed: screen inputs are a few KB (target+counterpart
    # code), far below the 272K long-tier threshold, so the short card
    # always applies. Revisit if screen inputs ever grow ~100x.
    return (max(0, n - cached - written) * r["rate_in"]
            + cached * r["rate_cached"] + written * r["rate_write"])


def should_skip(p_overlap: float | None, p_unsafe: float | None,
                failed: bool, mode: str) -> bool:
    # Fail-open: a missing/refused UNSAFE answer must NEVER suppress in
    # the protected modes. Unknown safety is treated as unsafe until
    # proven otherwise — skip only on an explicitly answered safe pair.
    # NOTE "eco" consults Q1 alone by frozen design (FROZEN03) and has no
    # Q2 fail-open question; use eco-protected/priority where Q2 matters.
    if failed or p_overlap is None:
        return False
    if mode == "eco":
        return p_overlap < ECO_P1
    if mode in ("eco-protected", "priority"):
        return (p_overlap < PROTECTED_P1
                and p_unsafe is not None and p_unsafe < PROTECTED_P2)
    return False


def _interleave_order(main: list, explore: list) -> list:
    """Splice exploration rows back at evenly spaced positions.

    Exploration appended at the tail is cut first under a binding ceiling
    — exactly when novelty matters most. Even spacing guarantees every
    budget level executes a proportional share of gamble slots while the
    risk band (head of main) is never displaced. Deterministic given the
    sampled set (positions derive from lengths only). Shared with the
    offline replay (sim04) so shipped and simulated orders coincide.
    """
    if not explore or not main:
        return list(main) + list(explore)
    ex_ids = set(map(id, explore))
    rest = [p for p in main if id(p) not in ex_ids]
    n = len(explore)
    # Slots start at 1: the head of main (risk band) is never displaced.
    slots = sorted({min(len(rest), max(
        1, (i + 1) * len(rest) // (n + 1))) for i in range(n)})
    out = list(rest)
    for offset, (slot, row) in enumerate(zip(slots, explore)):
        out.insert(min(len(out), slot + offset), row)
    return out


def priority_score(p_overlap: float | None, p_unsafe: float | None) -> float:
    # Unknown safety ranks as MAXIMUM risk (examine first), never minimum.
    q1 = p_overlap or 0
    q2 = p_unsafe if p_unsafe is not None else 1.0
    return PRIORITY_W1 * q1 + (1 - PRIORITY_W1) * q2


def screen_pairs(pairs: list, *, mode: str, head_rev: str,
                 evidence_fn=None, api_key: str | None = None,
                 ledger=None, ceiling_remaining: float | None = None,
                 post_fn=None, rate: dict | None = None) -> dict:
    """Route one pair list through the Decisions screen.

    Returns {"advanced": [...], "screened": [...], "screen_spend_usd",
    "screen_calls", "screen_failures", "screen_spend_unknown"}. ``rate``
    is the resolved provider card (keys rate_in/rate_cached/rate_write per
    1M tokens); without it the module Luna card is used. ``evidence_fn``
    returns (target_text, counterpart_text); ``post_fn`` performs the HTTP
    call (injectable for tests). Screened rows carry completed=False, keep
    their pre-filter pair_rank for telemetry only, and are never cacheable
    (see cache.is_completed_row).

    Unknown provider usage on any billed call makes the batch spend
    unconfirmable: screen_spend_usd is then None with
    screen_spend_unknown=True (unknown is never confirmed $0).
    """
    if mode in (None, "", "off") or not pairs:
        return {"advanced": list(pairs), "screened": [],
                "screen_spend_usd": 0.0, "screen_spend_unknown": False,
                "screen_calls": 0, "screen_failures": 0}
    if mode not in MODES:
        mode = "off"
        return {"advanced": list(pairs), "screened": [],
                "screen_spend_usd": 0.0, "screen_spend_unknown": False,
                "screen_calls": 0, "screen_failures": 0}
    if mode == "order":
        # Genuine ordering-only (DECISIONS-04 correction): no evidence
        # fetch, no provider calls, no skips. Stable production-first
        # demotion over the incoming credibility order; every pair
        # advances. Tagged for audit and presentation ordering.
        advanced = []
        for pair in pairs:
            pair = dict(pair)
            pair["screen_p_overlap"] = None
            pair["screen_p_unsafe"] = None
            pair["screen_test_involved"] = pair_test_involved(pair)
            pair["screen_mode"] = "order"
            advanced.append(pair)
        advanced.sort(key=lambda p: (1 if p.get("screen_test_involved")
                                     else 0))
        return {"advanced": advanced, "screened": [],
                "screen_spend_usd": 0.0, "screen_spend_unknown": False,
                "screen_calls": 0, "screen_failures": 0, "mode": "order"}
    if api_key is None:
        api_key = os.environ.get("SEMDUP_API_KEY") or os.environ.get(
            "OPENAI_API_KEY") or ""
    if not api_key and post_fn is None:
        # No credentials: fail open LOCALLY without issuing calls.
        # Ordering/demotion/presentation still apply when the mode is
        # enabled (degrades to the ordering-only path for routing).
        advanced = []
        for pair in pairs:
            pair = dict(pair)
            pair["screen_p_overlap"] = None
            pair["screen_p_unsafe"] = None
            pair["screen_test_involved"] = pair_test_involved(pair)
            pair["screen_mode"] = mode
            advanced.append(pair)
        if mode == "priority":
            advanced.sort(key=lambda p: (1 if p.get(
                "screen_test_involved") else 0))
        return {"advanced": advanced, "screened": [],
                "screen_spend_usd": 0.0, "screen_spend_unknown": False,
                "screen_calls": 0, "screen_failures": 0,
                "screen_no_key": True, "mode": mode}
    post = post_fn or (lambda key, text: _post_decision(key, text))
    inputs: list = []
    for pair in pairs:
        target = pair.get("target") or {}
        counterpart = pair.get("counterpart") or {}
        try:
            t_text, c_text = evidence_fn(pair) if evidence_fn else ("", "")
        except Exception:
            t_text, c_text = "", ""
        inputs.append((pair, render_screen_input(
            head_rev=head_rev, target=target, counterpart=counterpart,
            target_text=t_text, counterpart_text=c_text),
            bool(t_text and c_text)))
    # Preflight: rough upper bound on the whole screen batch; if the
    # screen itself does not fit the remaining ceiling, advance
    # everything (baseline spend profile, never overspend to save).
    est = sum(len(text) for _, text, _ in inputs) / 3.0 * (
        RATE_IN + RATE_WRITE)
    if ceiling_remaining is not None and est > ceiling_remaining:
        return {"advanced": list(pairs), "screened": [],
                "screen_spend_usd": 0.0, "screen_spend_unknown": False,
                "screen_calls": 0, "screen_failures": 0,
                "screen_deferred": True}
    advanced, screened = [], []
    spend, calls, failures, unknown_spend = 0.0, 0, 0, False
    stopped_by_budget = False
    for pair, text, has_evidence in inputs:
        if not has_evidence:
            pair = dict(pair)
            pair["screen_test_involved"] = pair_test_involved(pair)
            advanced.append(pair)  # no evidence to screen on: advance
            continue
        if stopped_by_budget:
            pair = dict(pair)
            pair["screen_p_overlap"] = None
            pair["screen_p_unsafe"] = None
            pair["screen_test_involved"] = pair_test_involved(pair)
            advanced.append(pair)
            continue
        try:
            res = post(api_key, text)
            parsed = parse_answer(res)
        except Exception:
            # The screen itself must never raise: a failed screen call
            # advances the pair (fail-open), never loses it.
            res, parsed = {"ok": False, "error": "screen-call-raised"}, {
                "p_overlap": None, "p_unsafe": None, "failed": True,
                "error": "screen-call-raised"}
        usage = (res.get("body") or {}).get("usage", {}) if res.get(
            "ok") else {}
        usage_known = bool(res.get("ok")) and isinstance(
            (usage or {}).get("input_tokens"), (int, float))
        cost = None
        if usage_known:
            cost = round(price_usage(usage, rate), 9)
            spend = round(spend + cost, 9)
        else:
            # Billed call without provider-confirmed usage: the batch
            # total is unconfirmable (never confirmed $0 downstream).
            unknown_spend = True
        calls += 1
        if parsed["failed"]:
            failures += 1
        # Per-call hard stop: the screen must never consume the budget
        # it is meant to protect. When the next estimated call would
        # exceed the remaining ceiling, screening ends here — every
        # remaining pair advances unscreened.
        if (ceiling_remaining is not None and not unknown_spend
                and spend + len(text) / 3.0 * (
                    RATE_IN + RATE_WRITE) > ceiling_remaining):
            stopped_by_budget = True
        if ledger is not None and usage_known:
            try:
                from types import SimpleNamespace
                ledger.record_response(
                    f"decisions-{pair.get('pair_id', calls)}",
                    SimpleNamespace(
                        requests=1,
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens")),
                    cached_input_tokens=(usage.get(
                        "input_tokens_details") or {}).get("cached_tokens"),
                    cache_write_input_tokens=(usage.get(
                        "input_tokens_details") or {}).get(
                            "cache_write_tokens"),
                    cache_in_input=True)
            except Exception:
                pass
        if should_skip(parsed["p_overlap"], parsed["p_unsafe"],
                       parsed["failed"], mode):
            row = dict(pair)
            row.update({
                "reason": SCREEN_REASON,
                "head_rev": head_rev,
                "detail": (f"decisions screen {SCREEN_VERSION} "
                           f"p_overlap={parsed['p_overlap']} "
                           f"p_unsafe={parsed['p_unsafe']} mode={mode}; "
                           f"not examined, not rejected; pre-filter "
                           f"pair_rank kept for telemetry only"),
                "completed": False,
                "screened": True,
                "screen_p_overlap": parsed["p_overlap"],
                "screen_p_unsafe": parsed["p_unsafe"],
                "screen_test_involved": pair_test_involved(pair),
                "screen_spend_usd": (cost if usage_known else None),
                "assess_ran": False,
            })
            screened.append(row)
        else:
            pair = dict(pair)
            pair["screen_p_overlap"] = parsed["p_overlap"]
            pair["screen_p_unsafe"] = parsed["p_unsafe"]
            pair["screen_test_involved"] = pair_test_involved(pair)
            advanced.append(pair)
    if mode == "priority":
        # DECISIONS-04 P4: production-first within each band (non-test
        # before test-involved; score decides within tiers). Exploration
        # is uniform from the non-risk bottom quartile (Amendment A3:
        # stratified sampling systematically delayed flagged rows into
        # cut-off slots; seed ranges showed draw composition does not
        # move headline metrics). Risk-band rows never enter the pool.
        def _pkey(p):
            return (1 if p.get("screen_test_involved") else 0,
                    -priority_score(p.get("screen_p_overlap"),
                                    p.get("screen_p_unsafe")),
                    str((p.get("target") or {}).get("path") or ""),
                    str((p.get("target") or {}).get("symbol") or ""))
        risky = sorted(
            [p for p in advanced
             if (p.get("screen_p_unsafe") is not None
                 and p.get("screen_p_unsafe") >= RISK_BAND_P2)],
            key=_pkey)
        risky_ids = set(map(id, risky))
        rest = sorted(
            [p for p in advanced if id(p) not in risky_ids], key=_pkey)
        main = risky + rest
        n_exp = max(1, int(len(main) * EXPLORATION_FRAC)) if main else 0
        pool = rest[-(max(1, len(rest) // 4)):] if rest else []
        import random as _random
        explore = _random.Random(SCREEN_VERSION).sample(
            pool, min(n_exp, len(pool))) if pool else []
        advanced = _interleave_order(main, explore)
    return {"advanced": advanced, "screened": screened,
            "screen_spend_usd": (None if unknown_spend else spend),
            "screen_spend_unknown": unknown_spend,
            "screen_stopped_by_budget": stopped_by_budget,
            "screen_calls": calls, "screen_failures": failures}
