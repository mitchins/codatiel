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
            "differences": [], "wrapper_or_delegation": False,
            "ownership_lifecycle_difference": False, "uncertainty": "none"}
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


# --- Luna challenge ---------------------------------------------------------------

def _challenge_call(value: dict):
    return _fake_call(json.dumps(value))


def test_challenge_upheld_characterises(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call({"fatal_objections": [], "unknowns": [],
                              "responsibility_holds": True}))
    assert out["ok"] is True and out["challenged"] is True
    assert out["assessment"]["validation"]["level"] == "characterised"
    assert out["fatal_claims"] == []


def test_challenge_behavioral_fatal_diverges(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    objection = {"claim": "rounding differs on halves",
                 "span": "cases/case01/billing.py:4-21",
                 "kind": "behavioral", "resolved_by_proposal": False}
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call({"fatal_objections": [objection],
                              "unknowns": [], "responsibility_holds": True}))
    assert out["assessment"]["relationship"] == "diverged"
    assert out["assessment"]["action"] == "investigate"
    assert out["assessment"]["validation"]["level"] == "unresolved"


def test_challenge_uncited_span_fails_closed(tmp_path):
    ev = _evidence_for_fake(tmp_path)
    objection = {"claim": "invented elsewhere",
                 "span": "elsewhere/evil.py:1-9",
                 "kind": "behavioral", "resolved_by_proposal": False}
    out = OD.challenge_pair(
        evidence=ev, terra=_terra(), tools=[], model="m",
        model_settings=None, ledger=None,
        call=_challenge_call({"fatal_objections": [objection],
                              "unknowns": [], "responsibility_holds": True}))
    assert out["assessment"]["relationship"] == "unknown"


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


def test_record_mapping_diverged_honest_suppress(tmp_path):
    # A Terra diverged-overlap keeps prelim investigate (honest) and the
    # frozen contract suppresses it — the mechanism is principled, not a
    # validation failure.
    db = _db(tmp_path)
    ev = OD.build_pair_evidence(db, *_units())
    rec = OD.to_engine_record(
        repo="r", pr=1, base_rev="b", head_rev="h", from_head="h",
        delta_fingerprint="fp", classification="introduced",
        target=_units()[0], evidence=ev,
        terra=_terra(relationship="diverged", strength="possible",
                     differences=["half-up vs half-even"]),
        challenge=None, retrieval_coverage="full", provenance=_prov())
    assert rec["preliminary_action"] == "investigate"
    adv = AV.classify_record(rec, db, "full")
    assert adv["suppressed"] is True


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
