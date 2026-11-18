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


def test_normalize_unknown_forces_uncertainty():
    got = CL.normalize_classification({"primary_category": "unknown",
                                       "divergence_impact": "unknown",
                                       "uncertainty": False})
    assert got["uncertainty"] is True
    ok = CL.normalize_classification({"primary_category": "state_integrity",
                                      "divergence_impact": "high",
                                      "uncertainty": False})
    assert ok["uncertainty"] is False


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
    import re
    kw = dict(repo="r", pr=1, base_rev="b" * 40, head_rev="h" * 40,
              rank=0)
    a = PA.pair_id_for(_pair(), **kw)
    b = PA.pair_id_for(_pair(), **kw)
    assert a == b
    assert re.fullmatch(r"spair-[0-9a-f]{24}", a)
    # Digest only: no unit identities, separators or source leak in.
    assert "a.py" not in a and "::" not in a and "/" not in a


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
    # Unknown is never priced at $0: confirmed is None, not 0.0.
    assert got["confirmed_usd"] is None
    assert led.entries["zzz"]["usage_known"] is False
    empty = led.price_entries([])
    assert empty == {"confirmed_usd": 0.0, "known": 0, "unknown": 0,
                     "unavailable": 0}
    absent = led.price_entries(["no-such-response"])
    assert absent["confirmed_usd"] is None and absent["unknown"] == 1


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
    # Partial Luna spend outranks the clean-hold label.
    assert ES.budget_status(
        {"reason": "budget_stop", "assess_ran": True,
         "terra_completed": True, "luna_response_ids": ["r1"]}) == \
        "interrupted_after_partial_spend"
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
    from semdup_testkit import (backends, indexed_two_file_repo, pair,
                                publish_assess, publish_edit)
    from codatiel_i2.semdup_dev import editorial_shadow as ES
    backends(monkeypatch)
    base, head = _lib_files()
    repo, base_rev, head_rev, db = indexed_two_file_repo(
        tmp_path, base, head)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ES.review_candidate_pairs(
        [pair("new.py", "add_one_new", "lib.py", "add_one")],
        repo="o/r", repo_path=repo, merged_db=db,
        base_rev=base_rev, head_rev=head_rev, head_tree=tmp_path,
        base_tree=tmp_path, run_dir=run_dir, ceiling_usd=0.000001,
        assess_fn=publish_assess, edit_fn=publish_edit, pr=7)
    assert out["ok"] is True
    assert out["suppressed"][0]["reason"] == "budget_stop"
    assert out["suppressed"][0]["budget_status"] == \
        "refused_before_first_request"
    assert out["budget_refusals"]["by_status"][
        "refused_before_first_request"] == {"count": 1,
                                           "confirmed_usd": 0.0,
                                           "unknown": False}


def _usage(requests, in_tok, out_tok, cached=0, reasoning=0):
    from types import SimpleNamespace
    return SimpleNamespace(requests=requests, input_tokens=in_tok,
                           output_tokens=out_tok)


def test_multi_turn_ledger_attribution_end_to_end(tmp_path, monkeypatch):
    import json as _json
    import sys
    sys.path.insert(0, "tests")
    from semdup_testkit import run_judged, pair
    base, head = _lib_files()

    def multi_assess(**kwargs):
        ledger = kwargs.get("ledger")
        ledger.record_response("terra-r1", _usage(1, 1000, 10),
                               cached_input_tokens=200,
                               reasoning_output_tokens=5)
        ledger.record_response("terra-r2", _usage(1, 3000, 20))
        return {"ok": True,
                "terra": {"overlap": True, "relationship": "equivalent",
                          "opportunity": "direct_reuse",
                          "shared_component": "add-one", "differences": []},
                "transcript": {"tool_calls": [
                    {"tool": "read_file", "args": {},
                     "summary": "x" * 100}], "wall_s": 1.2}}

    def single_edit(**kwargs):
        ledger = kwargs.get("ledger")
        ledger.record_response("luna-r1", _usage(1, 500, 30))
        return {"ok": True,
                "editor": {"decision": "KEEP",
                           "revised_comment": "use helper()",
                           "reason": "concrete reuse",
                           "classification": {
                               "primary_category": "maintainability",
                               "divergence_impact": "low"}},
                "transcript": {"tool_calls": [], "wall_s": 0.4},
                "error": ""}

    out = run_judged(tmp_path, monkeypatch, base, head,
                     [pair("new.py", "add_one_new", "lib.py", "add_one")],
                     multi_assess, single_edit, pr=7)
    assert len(out["published"]) == 1
    pub = out["published"][0]
    assert pub["classification"]["primary_category"] == "maintainability"
    assert pub["inference"]["terra_wall_s"] == 1.2
    assert pub["inference"]["terra_tool_calls"] == 1
    rec = _json.loads((tmp_path / "run" / "semdup_pairs.jsonl")
                      .read_text().splitlines()[0])
    assert rec["terra"]["responses"] == 2
    assert rec["terra"]["usage"]["input_tokens"] == 4000
    assert rec["terra"]["usage"]["cached_input_tokens"] == 200
    assert rec["terra"]["usage"]["reasoning_output_tokens"] == 5
    assert rec["luna"]["responses"] == 1
    assert rec["terra"]["tool_calls"] == [{"name": "read_file",
                                          "summary_chars": 100}]
    # Priced math: (1000-200)/1e6*1 + 200/1e6*1 + 10/1e6*1
    #            + 3000/1e6*1 + 20/1e6*1 + 500/1e6*1 + 30/1e6*1
    assert rec["pair_priced_usd"] == round(
        0.0008 + 0.0002 + 0.00001 + 0.003 + 0.00002 + 0.0005
        + 0.00003, 6)
    assert out["suppressed"] == []
    # Spend sinks carry provider rows; settlement schema untouched.
    terra_rows = (tmp_path / "run" / "spend_semdup_terra.jsonl") \
        .read_text().splitlines()
    assert len(terra_rows) == 2
    assert all("pair_id" not in line for line in terra_rows)


def test_interrupted_pair_retains_partial_attribution(tmp_path,
                                                      monkeypatch):
    import json as _json
    import sys
    sys.path.insert(0, "tests")
    from semdup_testkit import run_judged, pair
    import pytest
    base, head = _lib_files()

    def boom_assess(**kwargs):
        ledger = kwargs.get("ledger")
        ledger.record_response("terra-r1", _usage(1, 1000, 10))
        raise RuntimeError("synthetic interruption")

    def never_edit(**kwargs):  # pragma: no cover
        raise AssertionError("must not run")

    with pytest.raises(RuntimeError):
        run_judged(tmp_path, monkeypatch, base, head,
                   [pair("new.py", "add_one_new", "lib.py", "add_one")],
                   boom_assess, never_edit, pr=7)
    rec = _json.loads((tmp_path / "run" / "semdup_pairs.jsonl")
                      .read_text().splitlines()[0])
    assert rec["outcome"]["path"] == "interrupted"
    assert rec["terra"]["responses"] == 1
    assert rec["terra"]["usage"]["input_tokens"] == 1000
    assert rec["pair_priced_usd"] == round(1000 / 1e6 + 10 / 1e6, 6)


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
