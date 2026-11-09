"""SEMDUP-03 advisory layer tests (deterministic, no model calls).

Covers: advisory eligibility (possible/likely), source-reference
validation, negative cases (wrapper, keep-separate, divergent, ambiguous),
coverage disclosure, cache eviction bound, and replay determinism.
"""

import json
import sqlite3

from codatiel_i2.semdup_dev import advisory as AV
from codatiel_i2.semdup_dev import embcache as EC


_DB_SEQ = [0]


def _db(tmp_path, units):
    _DB_SEQ[0] += 1
    p = tmp_path / f"head_view_{_DB_SEQ[0]}.sqlite"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE units(path TEXT, symbol TEXT, kind TEXT,"
                 " start_line INT, end_line INT, text TEXT)")
    for u in units:
        conn.execute("INSERT INTO units VALUES(?,?,?,?,?,?)", u)
    conn.commit()
    conn.close()
    return p


def _rec(**kw):
    base = {
        "detector": "semdup-dev-01",
        "status": "investigate",
        "classification": "introduced",
        "target": {"path": "a/new.py", "symbol": "helper"},
        "preliminary_action": "reuse_existing",
        "recommended_action": "investigate",
        "relationship": "unknown",
        "counterparts": ["a/old.py::helper"],
        "material_differences": [],
        "must_remain_separate": [],
        "member_notes": [],
        "proposed_boundary": {"interface": "bounded retries"},
        "challenge": {"fatal_claims": [], "validation_level": "characterised"},
        "evidence": {"finalists": ["a/old.py::helper"],
                     "guards": {"wrapper_guard": {"triggered": False},
                                "ownership_guard": {"fired": False}}},
        "provenance": {"detector": "semdup-dev-01", "engine_sha": "x",
                       "investigator": {"requested_model": "m"},
                       "served_model_check": {"verified": True}},
        "repo": "r", "pr": 1, "base_rev": "b" * 40, "head_rev": "h" * 40,
        "from_head": "b" * 40, "delta_fingerprint": "fp",
    }
    base.update(kw)
    return base


def _units():
    return [
        ("a/new.py", "helper", "function", 1, 10, "def helper():\n  return 1\n"),
        ("a/old.py", "helper", "function", 1, 10, "def helper():\n  return 1\n"),
    ]


def test_possible_overlap_eligible(tmp_path):
    db = _db(tmp_path, _units())
    out = AV.classify_record(_rec(), db, "full")
    assert out["category"] == "possible_overlap"
    assert out["target"]["path"] == "a/new.py"
    assert out["counterpart"]["path"] == "a/old.py"
    assert "bounded retries" in out["shared_behavior"]
    assert out["evidence"]["target_text_lines"] == 2
    md = AV.render_markdown(out)
    assert "a/new.py::helper" in md and "a/old.py::helper" in md


def test_likely_duplication_requires_equivalent_and_clean(tmp_path):
    db = _db(tmp_path, _units())
    out = AV.classify_record(_rec(status="finding",
                                  recommended_action="reuse_existing",
                                  relationship="equivalent"), db, "full")
    assert out["category"] == "likely_duplication"
    # Shared-part findings never qualify as likely duplication.
    out2 = AV.classify_record(_rec(status="finding",
                                   recommended_action="extract_shared_part",
                                   relationship="shared_part"), db, "full")
    assert out2["category"] == "possible_overlap"
    # Nor do findings with named material differences (e.g. HALF_UP/EVEN).
    out3 = AV.classify_record(
        _rec(status="finding", recommended_action="extract_shared_part",
             relationship="shared_part",
             material_differences=["rounding mode HALF_UP vs HALF_EVEN"]),
        db, "full")
    assert out3["category"] == "possible_overlap"


def test_wrapper_use_suppressed(tmp_path):
    db = _db(tmp_path, _units())
    rec = _rec()
    rec["evidence"]["guards"]["wrapper_guard"] = {"triggered": True}
    out = AV.classify_record(rec, db, "full")
    assert out["suppressed"] and out["reason"] == "wrapper_use"


def test_keep_separate_suppressed(tmp_path):
    db = _db(tmp_path, _units())
    out = AV.classify_record(_rec(preliminary_action="keep_separate"), db, "full")
    assert out["suppressed"] and out["reason"] == "prelim_no_overlap_claim"


def test_divergent_prelim_suppressed(tmp_path):
    db = _db(tmp_path, _units())
    out = AV.classify_record(
        _rec(preliminary_action="investigate",
             material_differences=["rounding differs"]), db, "full")
    # prelim investigate with a claimed counterpart is not an overlap
    # assertion the layer can independently verify -> suppress.
    assert out["suppressed"] and out["reason"] == "prelim_no_overlap_claim"


def test_single_unambiguous_pair_eligible(tmp_path):
    units = [("a/new.py", "helper", "function", 1, 10, "def helper():\n  return 1\n"),
             ("a/old.py", "helper", "function", 1, 10, "def helper():\n  return 1\n"),
             ("oracles/o.py", "main", "function", 1, 5, "def main():\n  pass\n")]
    db = _db(tmp_path, units)
    rec = _rec(preliminary_action="investigate", counterparts=[],
               evidence={"finalists": ["a/old.py::helper",
                                       "a/new.py::helper#region@3",
                                       "oracles/o.py::main"],
                         "guards": {"wrapper_guard": {"triggered": False},
                                    "ownership_guard": {"fired": False}}})
    out = AV.classify_record(rec, db, "full")
    assert out["category"] == "possible_overlap"
    assert out["counterpart"]["path"] == "a/old.py"


def test_multiple_candidates_suppressed(tmp_path):
    db = _db(tmp_path, _units())
    rec = _rec(counterparts=["a/old.py::helper", "a/other.py::helper"])
    out = AV.classify_record(rec, db, "full")
    assert out["suppressed"] and out["reason"] == "multiple_candidates"


def test_no_candidate_suppressed(tmp_path):
    db = _db(tmp_path, _units())
    rec = _rec(preliminary_action="reuse_existing", counterparts=[])
    out = AV.classify_record(rec, db, "full")
    assert out["suppressed"] and out["reason"] == "no_candidate"


def test_reduced_coverage_never_advisory(tmp_path):
    db = _db(tmp_path, _units())
    out = AV.classify_record(_rec(), db, "reduced: 1 unit without vectors")
    assert out["suppressed"] and out["reason"] == "coverage_not_full"
    assert AV.render_markdown(out) == ""


def test_deferred_record_suppressed(tmp_path):
    db = _db(tmp_path, _units())
    out = AV.classify_record(_rec(status="deferred"), db, "full")
    assert out["suppressed"] and out["reason"] == "status_not_actionable"


def test_target_unresolved_suppressed(tmp_path):
    db = _db(tmp_path, [u for u in _units() if u[0] != "a/new.py"])
    out = AV.classify_record(_rec(), db, "full")
    assert out["suppressed"] and out["reason"] == "target_unresolved"


def test_counterpart_unresolved_suppressed(tmp_path):
    db = _db(tmp_path, [u for u in _units() if u[0] != "a/old.py"])
    out = AV.classify_record(_rec(), db, "full")
    assert out["suppressed"] and out["reason"] == "counterpart_unresolved"


def test_oracle_counterpart_rejected(tmp_path):
    db = _db(tmp_path, _units() + [("oracles/o.py", "main", "function", 1, 5,
                                    "def main():\n  pass\n")])
    rec = _rec(counterparts=["oracles/o.py::main"])
    out = AV.classify_record(rec, db, "full")
    assert out["suppressed"] and out["reason"] == "no_candidate"


def test_same_symbol_different_dirs_not_cross_resolved(tmp_path):
    units = [("a/new.py", "helper", "function", 1, 10, "def helper():\n  return 1\n"),
             ("b/old.py", "helper", "function", 1, 10, "def helper():\n  return 2\n"),
             ("c/old.py", "helper", "function", 1, 10, "def helper():\n  return 3\n")]
    db = _db(tmp_path, units)
    rec = _rec(counterparts=["b/old.py::helper"])
    out = AV.classify_record(rec, db, "full")
    assert not out.get("suppressed")
    assert out["counterpart"]["path"] == "b/old.py"
    assert "return 2" in out["evidence"]["excerpt_counterpart"]
    # Ambiguous anchor (same path+symbol twice) suppresses.
    units2 = units + [("b/old.py", "helper", "function", 20, 30,
                       "def helper():\n  return 4\n")]
    db2 = _db(tmp_path, units2)
    out2 = AV.classify_record(rec, db2, "full")
    assert out2["suppressed"] and out2["reason"] == "counterpart_unresolved"


def test_fragment_counterpart_rejected(tmp_path):
    db = _db(tmp_path, _units())
    rec = _rec(counterparts=["a/old.py::helper#region@3"])
    out = AV.classify_record(rec, db, "full")
    assert out["suppressed"] and out["reason"] == "no_candidate"


def test_differences_disclosed_in_rendering(tmp_path):
    db = _db(tmp_path, _units())
    rec = _rec(material_differences=["email values lowercased"],
               challenge={"fatal_claims": ["unaccounted conditional"],
                          "validation_level": "unresolved"})
    out = AV.classify_record(rec, db, "full")
    assert out["category"] == "possible_overlap"
    assert "email values lowercased" in out["differences"]
    assert "unaccounted conditional" in out["differences"]
    md = AV.render_markdown(out)
    assert "Known differences" in md and "email values lowercased" in md


def test_replay_deterministic(tmp_path):
    db = _db(tmp_path, _units())
    rec = _rec()
    a1 = AV.advisories_for_run([rec], db, "full", enabled=True)
    a2 = AV.advisories_for_run([rec], db, "full", enabled=True)
    assert json.dumps(a1, sort_keys=True) == json.dumps(a2, sort_keys=True)


def test_disabled_switch_degrades_to_none(tmp_path):
    db = _db(tmp_path, _units())
    assert AV.advisories_for_run([_rec()], db, "full", enabled=False) == []


def test_eviction_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(EC, "MAX_CACHE_VECTORS", 10)
    monkeypatch.setattr(EC, "MAX_CACHE_BYTES", 10 ** 12)
    cpath = EC.cache_path(tmp_path, "harrier", "m")
    cpath.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cpath))
    EC._ensure_schema(conn)
    import time as _t
    for i in range(25):
        conn.execute("INSERT OR REPLACE INTO vecs(key, dim, vec, last_used)"
                     " VALUES(?,?,?,?)", (f"k{i}", 2, b"\x00" * 8, float(i)))
    conn.commit()
    evicted = EC._enforce_bound(conn)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
    conn.close()
    assert evicted == 15 and n == 10
    st = EC.cache_stats(tmp_path, "harrier", "m")
    assert st["vectors"] == 10 and st["max_vectors"] == 10


def test_cache_key_stable_and_content_addressed():
    k1 = EC.content_key("harrier", "m", "text")
    k2 = EC.content_key("harrier", "m", "text")
    k3 = EC.content_key("harrier", "m", "other")
    assert k1 == k2 and k1 != k3
