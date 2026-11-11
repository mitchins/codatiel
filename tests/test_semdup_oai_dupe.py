"""SEMDUP-04: Terra/Luna path unit tests. No inference, no network.

All model interactions go through the injectable `call` parameter;
live-model behaviour is qualified by the bounded comparison, not here.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from codatiel_i2.semdup_dev import advisory as AV
from codatiel_i2.semdup_dev import oai_dupe as OD


TARGET_TEXT = "def aggregate(items):\n    total = 0.0\n    for it in items:\n        total += it['price'] * it['qty']\n    return round(total, 2)\n"
COUNTER_TEXT = "def compute_total(items):\n    subtotal = 0.0\n    for it in items:\n        subtotal += it['price'] * it['qty']\n    return round(subtotal, 2)\n"


def _db(tmp_path: Path, extra: list[tuple] | None = None) -> Path:
    db = tmp_path / "merged.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE units (id INTEGER PRIMARY KEY, path TEXT, "
                 "symbol TEXT, start_line INT, end_line INT, text TEXT)")
    conn.execute("INSERT INTO units (path, symbol, start_line, end_line, text)"
                 " VALUES (?,?,?,?,?)",
                 ("cases/case01/revenue.py", "aggregate_revenue_summary",
                  4, 22, TARGET_TEXT))
    conn.execute("INSERT INTO units (path, symbol, start_line, end_line, text)"
                 " VALUES (?,?,?,?,?)",
                 ("cases/case01/billing.py", "compute_invoice_total",
                  4, 21, COUNTER_TEXT))
    for row in (extra or []):
        conn.execute("INSERT INTO units (path, symbol, start_line, end_line, text)"
                     " VALUES (?,?,?,?,?)", row)
    conn.commit()
    conn.close()
    return db


def _units(target_text: str = TARGET_TEXT) -> tuple[dict, dict]:
    return ({"path": "cases/case01/revenue.py",
             "symbol": "aggregate_revenue_summary"},
            {"path": "cases/case01/billing.py",
             "symbol": "compute_invoice_total"})


def _terra(**over) -> dict:
    base = {"overlap": True, "relationship": "equivalent",
            "strength": "likely", "shared_behavior": "sum line totals",
            "shared_component": "line-total summation",
            "opportunity": "direct_reuse",
            "differences": [], "wrapper_or_delegation": False,
            "ownership_lifecycle_difference": False, "uncertainty": "none"}
    base.update(over)
    return base


def _luna(**over) -> dict:
    base = {"verdict": "survives", "shared_component": "line-total summation",
            "core_comparable": True, "replacement_unsafe_reasons": [],
            "fatal_objections": [], "unknowns": [],
            "responsibility_holds": True}
    base.update(over)
    return base


# --- evidence builder -------------------------------------------------------

def test_evidence_ok_embeds_both_texts(tmp_path):
    db = _db(tmp_path)
    t, c = _units()
    ev = OD.build_pair_evidence(db, t, c)
    assert ev["ok"] is True
    assert ev["target"]["text"] == TARGET_TEXT
    assert ev["counterpart"]["text"] == COUNTER_TEXT
    assert ev["target"]["start_line"] == 4
    assert ev["pair_sha256"]


def test_evidence_missing_target(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(
        db, {"path": "nope.py", "symbol": "missing"}, _units()[1])
    assert ev["ok"] is False and ev["code"] == "target_unresolved"


def test_evidence_empty_text(tmp_path):
    db = _db(tmp_path, extra=[("cases/x.py", "empty_fn", 1, 2, "   \n")])
    ev = OD.build_pair_evidence(
        db, {"path": "cases/x.py", "symbol": "empty_fn"}, _units()[1])
    assert ev["ok"] is False and ev["code"] == "target_unresolved"


def test_evidence_ambiguous_anchor(tmp_path):
    db = _db(tmp_path, extra=[("other/billing.py", "compute_invoice_total",
                               1, 3, COUNTER_TEXT)])
    # exact resolution must still succeed (ambiguity is across paths, and
    # validate_anchor is exact-path so this stays ok)...
    ev = OD.build_pair_evidence(db, _units()[0], _units()[1])
    assert ev["ok"] is True
    # ...while a duplicated exact anchor fails closed.
    dup_dir = tmp_path / "dup"
    dup_dir.mkdir()
    db2 = _db(dup_dir)
    conn = sqlite3.connect(str(db2))
    conn.execute("INSERT INTO units (path, symbol, start_line, end_line, text)"
                 " VALUES (?,?,?,?,?)",
                 ("cases/case01/billing.py", "compute_invoice_total",
                  30, 35, COUNTER_TEXT))
    conn.commit()
    conn.close()
    ev2 = OD.build_pair_evidence(db2, _units()[0], _units()[1])
    assert ev2["ok"] is False and ev2["code"] == "counterpart_unresolved"


def test_evidence_fragment_rejected(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(
        db, _units()[0], {"path": "cases/case01/billing.py",
                          "symbol": "compute#region1"})
    assert ev["ok"] is False and ev["code"] == "fragment_source"


def test_evidence_stale_sha(tmp_path):
    import hashlib
    db = _db(tmp_path)
    t, c = _units()
    ev = OD.build_pair_evidence(db, t, c)
    want = ev["target"]["sha256"]
    bad = OD.build_pair_evidence(db, t, c,
                                 expected_sha={"target": "0" * 64,
                                               "counterpart": ev["counterpart"]["sha256"]})
    assert bad["ok"] is False and bad["code"] == "stale_source"
    good = OD.build_pair_evidence(db, t, c,
                                  expected_sha={"target": want,
                                                "counterpart": ev["counterpart"]["sha256"]})
    assert good["ok"] is True


def test_model_inputs_contain_both_sources(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    terra_in = OD.render_terra_input(ev, head_rev="abc123")
    assert TARGET_TEXT in terra_in and COUNTER_TEXT in terra_in
    assert "abc123" in terra_in
    terra = _terra()
    luna_in = OD.render_luna_input(ev, terra)
    assert TARGET_TEXT in luna_in and COUNTER_TEXT in luna_in
    assert "sum line totals" in luna_in


# --- Terra output validation --------------------------------------------------

def test_validate_terra_good():
    assert OD.validate_terra_output(_terra()) == []
    assert OD.validate_terra_output(
        _terra(opportunity="none", shared_component="")) == []
    assert OD.validate_terra_output(
        _terra(opportunity="partial_extraction")) == []
    assert OD.validate_terra_output(_terra(opportunity="bogus")) != []
    assert OD.validate_terra_output(
        _terra(opportunity="direct_reuse", shared_component="")) != []


def test_validate_terra_missing_and_enums():
    bad = _terra()
    del bad["differences"]
    assert any("missing field" in e for e in OD.validate_terra_output(bad))
    assert OD.validate_terra_output(_terra(relationship="same")) != []
    assert OD.validate_terra_output(_terra(strength="maybe")) != []


def test_validate_terra_coherence():
    # diverged with overlap is coherent: same responsibility, different
    # behaviour. It maps to prelim investigate (honest) and the frozen
    # advisory contract suppresses it — never promoted.
    assert OD.validate_terra_output(_terra(overlap=True, relationship="diverged",
                                           strength="possible")) == []
    assert OD.validate_terra_output(_terra(overlap=True, strength="likely",
                                           relationship="diverged")) != []
    assert OD.validate_terra_output(_terra(overlap=True, relationship="related_only")) != []
    assert OD.validate_terra_output(_terra(overlap=True, strength="none")) != []
    assert OD.validate_terra_output(_terra(overlap=True, shared_behavior=" ")) != []
    assert OD.validate_terra_output(_terra(overlap=False, strength="possible")) != []
    assert OD.validate_terra_output(_terra(overlap=False, relationship="unknown",
                                           strength="none")) == []


# --- assess_pair with injected calls --------------------------------------------

def _fake_call(text: str, tool_log: list | None = None):
    def _call(**kwargs):
        assert "aggregate" in kwargs["user_input"] or "compute" in kwargs["user_input"] \
            or "def " in kwargs["user_input"]
        return {"ok": True, "final_text": text,
                "tool_calls": tool_log or [], "wall_s": 0.1}
    return _call


def _evidence_for_fake(tmp_path: Path) -> dict:
    db = _db(tmp_path)
    return OD.build_pair_evidence(db, *_units())


def test_assess_pair_ok(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    out = OD.assess_pair(evidence=ev, head_rev="abc", target_kind="introduced",
                         tools=[], model="m", model_settings=None,
                         ledger=None, call=_fake_call(json.dumps(_terra())))
    assert out["ok"] is True
    assert out["terra"]["relationship"] == "equivalent"
    assert out["transcript"]["pair_sha256"] == ev["pair_sha256"]
    assert TARGET_TEXT in out["transcript"]["user_input"]


def test_assess_pair_model_failure(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    out = OD.assess_pair(evidence=ev, head_rev="abc", target_kind="introduced",
                         tools=[], model="m", model_settings=None,
                         ledger=None,
                         call=lambda **k: {"ok": False, "error": "boom",
                                           "tool_calls": [], "wall_s": 0.0})
    assert out["ok"] is False and "boom" in out["error"]


def test_assess_pair_unparseable_and_invalid(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    kw = dict(evidence=ev, head_rev="abc", target_kind="introduced",
              tools=[], model="m", model_settings=None, ledger=None)
    out = OD.assess_pair(call=_fake_call("not json at all"), **kw)
    assert out["ok"] is False
    out2 = OD.assess_pair(call=_fake_call(json.dumps(_terra(relationship="x"))), **kw)
    assert out2["ok"] is False and "invalid" in out2["error"]


# --- Luna challenge (v2 semantics) -----------------------------------------------

def _challenge_call(value: dict):
    return _fake_call(json.dumps(value))


def _v2_challenge(**over):
    """Full new-schema Luna output."""
    return _luna(**over)


def test_challenge_survives_publishes(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call(_v2_challenge()))
    assert out["ok"] is True and out["challenged"] is True
    assert out["assessment"]["validation"]["level"] == "characterised"
    assert out["fatal_claims"] == []
    assert out["reconcile"]["decision"] == "publish"


def test_challenge_behavioral_core_refutes(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    objection = {"claim": "rounding differs on halves",
                 "span": "cases/case01/billing.py:4-21",
                 "kind": "behavioral", "resolved_by_proposal": False}
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call(_v2_challenge(
            verdict="refuted", shared_component="",
            fatal_objections=[objection], unknowns=[],
            responsibility_holds=True)))
    assert out["assessment"]["relationship"] == "diverged"
    assert out["assessment"]["action"] == "investigate"
    assert out["assessment"]["validation"]["level"] == "unresolved"
    assert out["reconcile"]["decision"] == "suppress"


def test_challenge_peripheral_survives_with_differences(tmp_path):
    # The 03 pattern: real objection, but about replacement safety
    # around a surviving shared core — published with disclosure.
    ev = _evidence_for_fake(tmp_path)
    objection = {"claim": "partial failure differs",
                 "span": "cases/case01/billing.py:4-21",
                 "kind": "behavioral", "resolved_by_proposal": False}
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(opportunity="partial_extraction"), tools=[],
        model="m", model_settings=None, ledger=None,
        call=_challenge_call(_v2_challenge(
            verdict="survives", fatal_objections=[objection],
            replacement_unsafe_reasons=["do not swap wholesale"],
            responsibility_holds=True)))
    assert out["reconcile"]["decision"] == "publish"
    assert out["assessment"]["validation"]["level"] == "characterised"
    assert any("partial failure" in d
               for d in out["assessment"]["observed_differences"])
    assert any("do not swap" in d
               for d in out["assessment"]["observed_differences"])


def test_challenge_uncertain_comparable_preserves(tmp_path):
    # The backoff pattern: speculative objection, comparable core.
    ev = _evidence_for_fake(tmp_path)
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call(_v2_challenge(
            verdict="uncertain", shared_component="",
            unknowns=["vendoring unknown"],
            responsibility_holds=True)))
    assert out["reconcile"]["decision"] == "publish"
    assert out["assessment"]["validation"]["level"] == "unresolved"
    assert any("vendoring unknown" in d
               for d in out["assessment"]["observed_differences"])


def test_challenge_uncertain_uncomparable_suppresses(tmp_path):
    # The 02b pattern: the unknown covers the core behavior itself.
    ev = _evidence_for_fake(tmp_path)
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call(_v2_challenge(
            verdict="uncertain", shared_component="",
            core_comparable=False, unknowns=["values unseen"],
            responsibility_holds=True)))
    assert out["reconcile"]["decision"] == "suppress"
    assert out["assessment"]["relationship"] == "unknown"


def test_challenge_uncited_span_discarded_not_fatal(tmp_path):
    # An objection citing evidence outside the pair is unsupported and
    # discarded; the decision falls back to verdict + comparability.
    ev = _evidence_for_fake(tmp_path)
    objection = {"claim": "invented elsewhere",
                 "span": "elsewhere/evil.py:1-9",
                 "kind": "behavioral", "resolved_by_proposal": False}
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call(_v2_challenge(
            verdict="refuted", shared_component="",
            fatal_objections=[objection], unknowns=[],
            responsibility_holds=True)))
    notes = out["reconcile"]["notes"]
    assert notes["discarded_unsupported"] == ["invented elsewhere"]
    # refuted solely on discarded evidence = unsubstantiated; a
    # comparable core preserves the lead.
    assert out["reconcile"]["decision"] == "publish"


def test_challenge_unreadable_preserves_terra_lead(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_fake_call("not json at all"))
    assert out["ok"] is False and out["challenged"] is False
    assert out["assessment"]["validation"]["level"] == "source_supported"


def test_challenge_invalid_schema_preserves_terra_lead(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call({"verdict": "bogus"}))
    assert out["ok"] is False and out["challenged"] is False


def test_challenge_failure_falls_back_unchallenged(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=lambda **k: {"ok": False, "error": "timeout",
                          "tool_calls": [], "wall_s": 1.0})
    assert out["ok"] is False and out["challenged"] is False
    assert out["assessment"]["validation"]["level"] == "source_supported"


# --- record mapping -> advisory -----------------------------------------------------

def _prov() -> dict:
    return {"detector": "semdup-test", "investigator": {"requested_model": "terra"},
            "challenger": {"requested_model": "luna"}}


def test_record_mapping_config_a_possible(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    rec = OD.to_engine_record(
        repo="r", pr=1, base_rev="b", head_rev="h", from_head="h",
        delta_fingerprint="fp", classification="introduced",
        target=_units()[0], evidence=ev, terra=_terra(),
        challenge=None, retrieval_coverage="full", provenance=_prov())
    adv = AV.classify_record(rec, db, "full")
    assert adv["suppressed"] is False
    assert adv["category"] == "possible_overlap"


def test_record_mapping_config_b_likely(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    challenged = {"challenged": True,
                  "assessment": OD.terra_to_assessment(_terra(), ev) | {
                      "action": "reuse_existing",
                      "validation": {"level": "characterised",
                                     "unresolved": [], "artifacts": []}},
                  "fatal_claims": []}
    rec = OD.to_engine_record(
        repo="r", pr=1, base_rev="b", head_rev="h", from_head="h",
        delta_fingerprint="fp", classification="introduced",
        target=_units()[0], evidence=ev, terra=_terra(),
        challenge=challenged, retrieval_coverage="full",
        provenance=_prov())
    adv = AV.classify_record(rec, db, "full")
    assert adv["suppressed"] is False
    assert adv["category"] == "likely_duplication"


def test_record_mapping_wrapper_suppresses(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    rec = OD.to_engine_record(
        repo="r", pr=1, base_rev="b", head_rev="h", from_head="h",
        delta_fingerprint="fp", classification="introduced",
        target=_units()[0], evidence=ev,
        terra=_terra(wrapper_or_delegation=True),
        challenge=None, retrieval_coverage="full", provenance=_prov())
    adv = AV.classify_record(rec, db, "full")
    assert adv["suppressed"] is True and adv["reason"] == "wrapper_use"


def test_record_mapping_no_overlap_suppresses(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    rec = OD.to_engine_record(
        repo="r", pr=1, base_rev="b", head_rev="h", from_head="h",
        delta_fingerprint="fp", classification="introduced",
        target=_units()[0], evidence=ev,
        terra=_terra(overlap=False, relationship="related_only",
                     strength="none", shared_behavior="",
                     differences=["different purpose"]),
        challenge=None, retrieval_coverage="full", provenance=_prov())
    assert rec["preliminary_action"] == "keep_separate"
    adv = AV.classify_record(rec, db, "full")
    assert adv["suppressed"] is True


def test_record_mapping_downgraded_suppresses_or_discloses(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    asm = OD.terra_to_assessment(_terra(), ev)
    asm = dict(asm, action="investigate", relationship="diverged",
               validation={"level": "unresolved",
                           "unresolved": ["rounding differs"], "artifacts": []},
               observed_differences=["rounding differs"])
    rec = OD.to_engine_record(
        repo="r", pr=1, base_rev="b", head_rev="h", from_head="h",
        delta_fingerprint="fp", classification="introduced",
        target=_units()[0], evidence=ev, terra=_terra(),
        challenge={"challenged": True, "assessment": asm,
                   "fatal_claims": ["rounding differs"]},
        retrieval_coverage="full", provenance=_prov())
    adv = AV.classify_record(rec, db, "full")
    assert adv["suppressed"] is True  # diverged prelim cannot advise


def test_record_mapping_diverged_opportunity_paths(tmp_path):
    # v2: opportunity drives prelim. Legacy diverged without annotation
    # conservatively maps to none (suppress); an annotated partial
    # extraction (the 02a path) maps to extract_shared_part (publishable).
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    kw = dict(repo="r", pr=1, base_rev="b", head_rev="h", from_head="h",
              delta_fingerprint="fp", classification="introduced",
              target=_units()[0], evidence=ev, challenge=None,
              retrieval_coverage="full", provenance=_prov())
    legacy_none = _terra(relationship="diverged", strength="possible",
                         differences=["half-up vs half-even"])
    del legacy_none["opportunity"]
    rec_none = OD.to_engine_record(terra=legacy_none, **kw)
    assert rec_none["preliminary_action"] == "keep_separate"
    assert AV.classify_record(rec_none, db, "full")["suppressed"] is True
    legacy = _terra(relationship="diverged", strength="possible",
                    differences=["out-of-domain edge"])
    del legacy["opportunity"]  # stored SEMDUP-04 outputs lack the field
    legacy["annotated_opportunity"] = "partial_extraction"
    rec_partial = OD.to_engine_record(terra=legacy, **kw)
    assert rec_partial["preliminary_action"] == "extract_shared_part"
    adv = AV.classify_record(rec_partial, db, "full")
    assert adv["suppressed"] is False
    assert adv["category"] == "possible_overlap"


def test_deterministic_wrapper_hit():
    assert OD._deterministic_wrapper_hit(
        "def f():\n    return compute_invoice_total(x)\n",
        "compute_invoice_total") is True
    assert OD._deterministic_wrapper_hit("def f():\n    return 1\n",
                                         "compute_invoice_total") is False


def test_estimate_call_usd():
    rate = {"input": 1.0, "output": 5.0}
    est = OD.estimate_call_usd(8000, rate)
    assert est is not None and 0.005 < est < 0.05
    assert OD.estimate_call_usd(8000, {}) is None


def test_materialize_checkouts(two_commit_repo, tmp_path):
    repo = Path(two_commit_repo["repo"])
    base = two_commit_repo["base_sha"]
    head = two_commit_repo["head_sha"]
    out = OD.materialize_checkouts(repo, base, head, tmp_path / "co")
    assert out["ok"] is True
    assert (out["base_dir"] / "src" / "app.py").exists() or \
        list(out["base_dir"].iterdir())


def test_validate_luna_output():
    assert OD.validate_luna_output(_luna()) == []
    assert OD.validate_luna_output(_luna(verdict="nope")) != []
    assert OD.validate_luna_output(_luna(core_comparable="yes")) != []
    assert OD.validate_luna_output(_luna(verdict="survives",
                                         shared_component="")) != []
    assert OD.validate_luna_output(_luna(
        verdict="refuted", fatal_objections=[],
        responsibility_holds=True)) != []
    assert OD.validate_luna_output(_luna(
        verdict="refuted", shared_component="",
        fatal_objections=[{"claim": "x", "span": "a:1",
                           "kind": "behavioral",
                           "resolved_by_proposal": False}],
        responsibility_holds=True)) == []


def test_opportunity_of():
    assert OD.opportunity_of(_terra()) == "direct_reuse"
    assert OD.opportunity_of(_terra(opportunity="partial_extraction")) == \
        "partial_extraction"
    assert OD.opportunity_of({"relationship": "shared_part"}) == \
        "partial_extraction"
    assert OD.opportunity_of({"relationship": "diverged"}) == "none"
    assert OD.opportunity_of({"relationship": "related_only"}) == "none"


def test_check_process_claims():
    # Confabulated failure with no tool calls -> flagged.
    out = OD.check_process_claims(
        "Source tools could not access the pinned revision.", [])
    assert out["process_claim_unverified"] is True
    out2 = OD.check_process_claims(
        "The checkout did not match the claimed revision.", [])
    assert out2["process_claim_unverified"] is True
    # Honest non-use statements never match.
    out3 = OD.check_process_claims(
        "Callers and tests were not examined.", [])
    assert out3["process_claim_unverified"] is False
    # A real failure with real tool calls is not confabulation.
    out4 = OD.check_process_claims(
        "Source tools could not access the pinned revision.",
        [{"tool": "read_file"}])
    assert out4["claimed_tool_failure"] is True
    assert out4["process_claim_unverified"] is False
    # Real SEMDUP-04 uncertainty strings.
    out5 = OD.check_process_claims(
        "Source tools failed because the checkout did not match.", [])
    assert out5["process_claim_unverified"] is True


def test_render_simple_advisory(tmp_path):
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    rec = OD.to_engine_record(
        repo="r", pr=1, base_rev="b", head_rev="h", from_head="h",
        delta_fingerprint="fp", classification="introduced",
        target=_units()[0], evidence=ev, terra=_terra(),
        challenge=None, retrieval_coverage="full", provenance=_prov())
    adv = AV.classify_record(rec, db, "full")
    assert adv["suppressed"] is False
    text = OD.render_simple_advisory(adv, rec)
    assert text.startswith("**Possible existing functionality**")
    assert "Likely duplication" not in text
    assert "compute_invoice_total" in text
    # Suppressed renders nothing.
    assert OD.render_simple_advisory({"suppressed": True}, rec) == ""
    # Unsafe reasons + open questions are sectioned, not hidden.
    rec2 = dict(rec)
    ch = dict(rec2["challenge"])
    ch["replacement_unsafe_reasons"] = ["do not swap wholesale"]
    ch["luna_unknowns"] = ["lifecycle unknown"]
    rec2["challenge"] = ch
    text2 = OD.render_simple_advisory(adv, rec2)
    assert "Replacement risks identified:" in text2
    assert "Open questions (not verified):" in text2
    # Process-claim note appears only when flagged.
    prov = dict(rec2.get("provenance") or {})
    prov["process_claims"] = {"process_claim_unverified": True,
                              "tool_calls_made": 0,
                              "claimed_tool_failure": True}
    rec2["provenance"] = prov
    assert "Process note:" in OD.render_simple_advisory(adv, rec2)
