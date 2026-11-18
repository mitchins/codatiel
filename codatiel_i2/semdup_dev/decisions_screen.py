"""SEMDUP Decisions screening (DECISIONS-03 promotion artifact).

Cheap pre-screen: one POST /v1/decisions call per candidate pair
(model gpt-6-luna, two frozen predicates). Low-scoring pairs are
recorded as screened and never reach Terra; everything else keeps the
existing Terra -> Luna path unchanged.

Safety properties (all tested in tests/test_semdup_decisions_screen.py):
- Default OFF: with the flag unset/invalid the pair list is untouched.
- Fail-open: API errors, refusals, malformed/incomplete answers and
  timeouts advance every pair (baseline behaviour, never a silent drop).
- Screened rows are NOT verdicts: they carry completed=False and the
  reason is listed in cache.is_completed_row's never-cache set, so they
  can never seed continuation or cross-revision reuse.
- Screen spend is priced with the provider-confirmed Luna rate card and
  recorded in spend_semdup_decisions.jsonl, included in spend_usd.
- Screen batch preflight respects the remaining ceiling; if the screen
  itself does not fit, everything advances (baseline spend profile).

Modes (SEMDUP_DECISIONS_SCREEN): off (default), eco, eco-protected,
priority. Thresholds frozen per DECISIONS-03 FROZEN03.
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

# Frozen operating points (FROZEN03).
MODES = ("off", "eco", "eco-protected", "priority")
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
    r"|e2e)(/|_|\.|$)|_test\.|_spec\.|conftest|Test\.swift|Tests?/",
    re.IGNORECASE)
_TEST_SYM = re.compile(
    r"^(test_|test|Test|spec_|should_|mock_|fake_|stub_)", re.IGNORECASE)


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


def price_usage(usage: dict) -> float:
    n = (usage or {}).get("input_tokens") or 0
    det = ((usage or {}).get("input_tokens_details") or {})
    cached = det.get("cached_tokens") or 0
    written = det.get("cache_write_tokens") or 0
    return max(0, n - cached) * RATE_IN + cached * RATE_CACHED + written * RATE_WRITE


def should_skip(p_overlap: float | None, p_unsafe: float | None,
                failed: bool, mode: str) -> bool:
    if failed or p_overlap is None:
        return False
    if mode == "eco":
        return p_overlap < ECO_P1
    if mode in ("eco-protected", "priority"):
        return (p_overlap < PROTECTED_P1 and (p_unsafe or 0) < PROTECTED_P2)
    return False


def priority_score(p_overlap: float | None, p_unsafe: float | None) -> float:
    return PRIORITY_W1 * (p_overlap or 0) + (1 - PRIORITY_W1) * (p_unsafe or 0)


def screen_pairs(pairs: list, *, mode: str, head_rev: str,
                 evidence_fn=None, api_key: str | None = None,
                 ledger=None, ceiling_remaining: float | None = None,
                 post_fn=None) -> dict:
    """Route one pair list through the Decisions screen.

    Returns {"advanced": [...], "screened": [...], "screen_spend_usd",
    "screen_calls", "screen_failures"}. ``evidence_fn(pair)`` returns
    (target_text, counterpart_text); ``post_fn`` performs the HTTP call
    (injectable for tests). Screened rows carry completed=False and are
    never cacheable (see cache.is_completed_row).
    """
    if mode in (None, "", "off") or not pairs:
        return {"advanced": list(pairs), "screened": [],
                "screen_spend_usd": 0.0, "screen_calls": 0,
                "screen_failures": 0}
    if mode not in MODES:
        mode = "off"
        return {"advanced": list(pairs), "screened": [],
                "screen_spend_usd": 0.0, "screen_calls": 0,
                "screen_failures": 0}
    if api_key is None:
        api_key = os.environ.get("SEMDUP_API_KEY") or os.environ.get(
            "OPENAI_API_KEY") or ""
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
                "screen_spend_usd": 0.0, "screen_calls": 0,
                "screen_failures": 0, "screen_deferred": True}
    advanced, screened = [], []
    spend, calls, failures = 0.0, 0, 0
    for pair, text, has_evidence in inputs:
        if not has_evidence:
            advanced.append(pair)  # no evidence to screen on: advance
            continue
        res = post(api_key, text)
        parsed = parse_answer(res)
        usage = (res.get("body") or {}).get("usage", {}) if res.get(
            "ok") else {}
        cost = round(price_usage(usage), 9)
        spend = round(spend + cost, 9)
        calls += 1
        if parsed["failed"]:
            failures += 1
        if ledger is not None and res.get("ok") and usage:
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
                           f"not examined, not rejected"),
                "completed": False,
                "screened": True,
                "screen_p_overlap": parsed["p_overlap"],
                "screen_p_unsafe": parsed["p_unsafe"],
                "screen_spend_usd": cost,
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
             if (p.get("screen_p_unsafe") or 0) >= RISK_BAND_P2],
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
        ex_ids = set(map(id, explore))
        advanced = ([p for p in main if id(p) not in ex_ids] + explore)
    return {"advanced": advanced, "screened": screened,
            "screen_spend_usd": spend, "screen_calls": calls,
            "screen_failures": failures}
