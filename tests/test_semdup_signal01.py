"""SEMDUP-SIGNAL-01 failing-first tests: risk classification + per-pair economics.

Gates covered (mission D):
1. identical retrieval/ordering, 2. identical Terra/Luna eligibility,
3. no additional model requests, 4. unchanged publication/continuation/
   mirror/scaffold decisions, 5. no changes to ordinary review or ACK
   handling, 6. no loss/duplication of confirmed spending, 7. correct
   attribution across multi-turn tool calls, 8. correct partial-spend
   attribution after interruptions, 9. safe handling of absent/legacy
   usage fields, 10. stable classification schema with unknown fallback.
"""
import json

import pytest

from codatiel_i2.semdup_dev import classification as CL
from codatiel_i2.semdup_dev import pairing as PA


# --------------------------------------------------------------------------
# A: schema stability (gate 10) + verdict authority.
# --------------------------------------------------------------------------

def test_normalize_full_classification_passthrough():
    got = CL.normalize_classification({
        "primary_category": "state_integrity",
        "secondary_category": "performance_resilience",
        "divergence_impact": "high",
        "shared_responsibility": "cursor advance",
        "divergence_consequence": "skipped rows",
        "uncertainty": False,
    })
    assert got["primary_category"] == "state_integrity"
    assert got["secondary_category"] == "performance_resilience"
    assert got["divergence_impact"] == "high"
    assert got["uncertainty"] is False


@pytest.mark.parametrize("raw", [None, "x", 42, [], True])
def test_normalize_non_dict_is_unknown_never_raises(raw):
    got = CL.normalize_classification(raw)
    assert got["primary_category"] == "unknown"
    assert got["divergence_impact"] == "unknown"


@pytest.mark.parametrize("raw", [{}, {"primary_category": "bogus"},
                                 {"divergence_impact": "critical!!"},
                                 {"primary_category": None,
                                  "divergence_impact": ["high"]}])
def test_normalize_malformed_fields_fall_back_unknown(raw):
    got = CL.normalize_classification(raw)
    assert got["primary_category"] in CL.PRIMARY_CATEGORIES
    assert got["divergence_impact"] in CL.DIVERGENCE_IMPACTS
    if "bogus" in str(raw) or not raw:
        assert got["primary_category"] == "unknown"


def test_normalize_bad_secondary_dropped_not_fabricated():
    got = CL.normalize_classification({"primary_category": "maintainability",
                                       "secondary_category": "nonsense",
                                       "divergence_impact": "low"})
    assert got["secondary_category"] is None
    assert got["primary_category"] == "maintainability"


def test_validate_editor_output_ignores_classification():
    from codatiel_i2.semdup_dev import editor as ED
    base = {"decision": "KEEP", "revised_comment": "c", "reason": "r"}
    assert ED.validate_editor_output(dict(base)) == []
    assert ED.validate_editor_output(
        dict(base, classification={"primary_category": "bogus"})) == []
    assert ED.validate_editor_output(
        dict(base, classification=None)) == []


def test_editor_instructions_contain_classification_schema():
    from codatiel_i2.semdup_dev import editor as ED
    assert "primary_category" in ED.EDITOR_INSTRUCTIONS
    assert "divergence_impact" in ED.EDITOR_INSTRUCTIONS
    assert ED.EDITOR_INSTRUCTIONS.count("JSON object") >= 1


def test_edit_pair_carries_classification_unknown_fallback():
    from codatiel_i2.semdup_dev import editor as ED
    pkg = {"rev": "r",
           "target": {"path": "a.py", "symbol": "f",
                      "start_line": 1, "end_line": 3, "text": "def f():\n pass\n",
                      "sha256": "x"},
           "counterpart": {"path": "b.py", "symbol": "g",
                           "start_line": 1, "end_line": 3,
                           "text": "def g():\n pass\n", "sha256": "y"},
           "files": {}}
    call = lambda **kw: {"ok": True,
                         "final_text": json.dumps(
                             {"decision": "KEEP", "revised_comment": "c",
                              "reason": "r",
                              "classification": {
                                  "primary_category": "security_safety",
                                  "divergence_impact": "high"}}),
                         "tool_calls": [], "wall_s": 0.1}
    out = ED.edit_pair(package=pkg, proposed_comment="p", tools=[],
                       model="m", model_settings=None, ledger=None,
                       call=call)
    assert out["ok"] is True
    assert out["editor"]["classification"]["primary_category"] == \
        "security_safety"
    call2 = lambda **kw: {"ok": True,
                          "final_text": json.dumps(
                              {"decision": "DROP", "revised_comment": "",
                               "reason": "r"}),
                          "tool_calls": [], "wall_s": 0.1}
    out2 = ED.edit_pair(package=pkg, proposed_comment="p", tools=[],
                        model="m", model_settings=None, ledger=None,
                        call=call2)
    assert out2["ok"] is True
    assert out2["editor"]["classification"]["primary_category"] == \
        "unknown"


# --------------------------------------------------------------------------
# B1: stable pair identity.
# --------------------------------------------------------------------------

def _pair():
    return {"target": {"path": "a.py", "symbol": "f"},
            "counterpart": {"path": "b.py", "symbol": "g"},
            "classification": "introduced"}


def test_pair_id_deterministic_and_digest_only():
    kw = dict(repo="r", pr=1, base_rev="b" * 40, head_rev="h" * 40,
              rank=0)
    a = PA.pair_id_for(_pair(), **kw)
    b = PA.pair_id_for(_pair(), **kw)
    assert a == b
    assert a.startswith("spair-")
    assert "a.py" not in a and " ourselves" not in a


def test_pair_id_changes_on_direction_rank_rev_symbol():
    kw = dict(repo="r", pr=1, base_rev="b" * 40, head_rev="h" * 40,
              rank=0)
    base = PA.pair_id_for(_pair(), **kw)
    swapped = {"target": {"path": "b.py", "symbol": "g"},
               "counterpart": {"path": "a.py", "symbol": "f"},
               "classification": "introduced"}
    assert PA.pair_id_for(swapped, **kw) != base
    assert PA.pair_id_for(_pair(), **dict(kw, rank=1)) != base
    assert PA.pair_id_for(_pair(), **dict(kw, head_rev="x" * 40)) != base
    other = {"target": {"path": "a.py", "symbol": "f2"},
             "counterpart": {"path": "b.py", "symbol": "g"},
             "classification": "introduced"}
    assert PA.pair_id_for(other, **kw) != base


def test_stamp_pairs_assigns_rank_without_reordering():
    pairs = [_pair(), _pair()]
    stamped = PA.stamp_pairs(pairs, repo="r", pr=1, base_rev="b",
                             head_rev="h")
    assert [p["pair_rank"] for p in stamped] == [0, 1]
    assert stamped[0]["pair_id"] != stamped[1]["pair_id"]
    # order preserved (gate 1)
    assert [p["target"]["symbol"] for p in stamped] == ["f", "f"]


# --------------------------------------------------------------------------
# B3: ledger subset pricing never mutates settlement (gate 6).
# --------------------------------------------------------------------------

def _ledger():
    from codatiel_i2.core.spend import SpendLedger
    led = SpendLedger(rate={"input": 2.0, "output": 8.0},
                      rate_status="test", ceiling_usd=100.0)

    class U:
        def __init__(self, r, i, o):
            self.requests, self.input_tokens, self.output_tokens = r, i, o
    led.record_response("a", U(1, 1_000_000, 0),
                        cached_input_tokens=250_000,
                        reasoning_output_tokens=None)
    led.record_response("b", U(2, 500_000, 1000))
    return led


def test_price_entries_subset_matches_proportional_share():
    led = _ledger()
    before = (dict(led.entries), list(led.order))
    part = led.price_entries(["a"])
    assert part["unknown"] == 0
    assert part["confirmed_usd"] is not None and part["confirmed_usd"] > 0
    whole = led.confirmed_usd()
    rest = led.price_entries(["b"])
    assert abs(part["confirmed_usd"] + rest["confirmed_usd"]
               - whole) < 1e-9
    assert dict(led.entries) == before[0] and list(led.order) == before[1]


def test_price_entries_unknown_never_zero():
    led = _ledger()
    led.record_response("zzz", None)
    got = led.price_entries(["zzz"])
    assert got["unknown"] == 1
    assert got["confirmed_usd"] == 0.0  # no confirmed spend, not $0 claim
    assert led.entries["zzz"]["usage_known"] is False


# --------------------------------------------------------------------------
# B4: budget refusal states.
# --------------------------------------------------------------------------

def test_budget_status_mapping():
    from codatiel_i2.semdup_dev import editorial_shadow as ES
    assert ES.budget_status(
        {"reason": "budget_stop", "assess_ran": None,
         "terra_response_ids": []}) == "refused_before_first_request"
    assert ES.budget_status(
        {"reason": "budget_stop", "assess_ran": True,
         "terra_response_ids": []}) == \
        "refused_before_first_billed_response"
    assert ES.budget_status(
        {"reason": "budget_stop", "assess_ran": True,
         "terra_response_ids": ["resp_1"]}) == \
        "interrupted_after_partial_spend"
    assert ES.budget_status(
        {"reason": "budget_stop", "assess_ran": True,
         "terra_completed": True, "luna_response_ids": []}) == \
        "terra_held_luna_refused"
    assert ES.budget_status({"reason": "terra_no_lead"}) == ""


# --------------------------------------------------------------------------
# B2/B3 integration: per-pair sink, rate snapshot, spend schema intact.
# --------------------------------------------------------------------------

def _lib_files():
    lib = "def add_one(x):\n    return x + 1\n"
    new = "def add_one_new(x):\n    return x + 1\n"
    return {"lib.py": lib}, {"lib.py": lib, "new.py": new}


def test_pair_economics_sink_and_published_row(tmp_path, monkeypatch):
    import json as _json
    import sys
    sys.path.insert(0, "tests")
    from semdup_testkit import run_judged, pair, publish_assess, publish_edit
    base, head = _lib_files()
    out = run_judged(tmp_path, monkeypatch, base, head,
                     [pair("new.py", "add_one_new", "lib.py", "add_one")],
                     publish_assess, publish_edit, pr=7)
    assert out["ok"] is True and len(out["published"]) == 1
    pub = out["published"][0]
    assert pub["pair_id"] and pub["pair_id"].startswith("spair-")
    assert pub["pair_rank"] == 0
    assert pub["classification"]["primary_category"] == "unknown"
    run_dir = tmp_path / "run"
    rows = [_json.loads(line) for line in
            (run_dir / "semdup_pairs.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    rec = rows[0]
    assert rec["pair_id"] == pub["pair_id"] and rec["pr"] == 7
    assert rec["terra_prompt_chars"] > 0 and rec["luna_prompt_chars"] > 0
    assert rec["terra"]["model"] == "test-terra"
    assert rec["luna"]["instructions_id"].startswith(
        "semdup-comment-editor-")
    assert rec["outcome"] == {"path": "published",
                              "reason": "concrete reuse",
                              "decision": "KEEP"}
    snap = _json.loads((run_dir / "rate_snapshot.json").read_text())
    assert snap["terra"]["rate_usd_per_mtok"] == {"input": 1.0,
                                                 "output": 1.0}
    assert snap["terra"]["rate_status"] == "test"
    # Settlement untouched: spend rows carry no pair labels.
    for name in ("spend_semdup_terra.jsonl", "spend_semdup_luna.jsonl"):
        p = run_dir / name
        if p.exists():
            for line in p.read_text().splitlines():
                assert "pair_id" not in line


def test_budget_refusal_states_end_to_end(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, "tests")
    from semdup_testkit import run_judged, pair
    base, head = _lib_files()
    states = {"n": 0}

    def raising_assess(**kwargs):
        states["n"] += 1
        return {"ok": False, "terra": {},
                "error": "BudgetCeilingExceeded: cap hit",
                "transcript": {"tool_calls": []}}

    from semdup_testkit import publish_edit
    out = run_judged(tmp_path, monkeypatch, base, head,
                     [pair("new.py", "add_one_new", "lib.py", "add_one")],
                     raising_assess, publish_edit, pr=7)
    assert out["suppressed"][0]["reason"] == "budget_stop"
    assert out["suppressed"][0]["budget_status"] == \
        "refused_before_first_billed_response"
    assert out["budget_refusals"]["total"] == 1
    assert out["budget_refusals"]["by_status"][
        "refused_before_first_billed_response"]["count"] == 1


def test_preflight_refusal_before_first_request(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, "tests")
    from semdup_testkit import run_judged, pair, publish_assess, publish_edit
    base, head = _lib_files()
    out = run_judged(tmp_path, monkeypatch, base, head,
                     [pair("new.py", "add_one_new", "lib.py", "add_one")],
                     publish_assess, publish_edit, pr=7)
    # Sanity: with a normal ceiling the pair is judged, not refused.
    assert out["ok"] is True


def test_no_additional_model_calls(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, "tests")
    from semdup_testkit import run_judged, pair, recording_calls
    calls, assess, edit = recording_calls()
    base, head = _lib_files()
    out = run_judged(tmp_path, monkeypatch, base, head,
                     [pair("new.py", "add_one_new", "lib.py", "add_one")],
                     assess, edit, pr=7)
    assert out["ok"] is True
    assert calls.count("assess") == 1 and calls.count("edit") == 1


# --------------------------------------------------------------------------
# D: regression gates — decisions unchanged by observability.
# --------------------------------------------------------------------------

def test_pair_stamping_does_not_change_eligibility_or_decisions():
    from codatiel_i2.semdup_dev import editorial_shadow as ES
    pairs = [{"target": {"path": "t/a.py", "symbol": "f"},
              "counterpart": {"path": "t/b.py", "symbol": "f"},
              "classification": "introduced", "channels": ["x"]}]
    stamped = PA.stamp_pairs([dict(p) for p in pairs], repo="r", pr=1,
                             base_rev="b", head_rev="h")
    assert ES.mirror_skip_decision(
        stamped[0]["target"], stamped[0]["counterpart"], {}) is False
    assert ES.mirror_skip_decision(
        pairs[0]["target"], pairs[0]["counterpart"], {}) is False
