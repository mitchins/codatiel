"""SEMDUP-MIRROR-01: pre-examination mirror suppression.

The mirror verdict moves ahead of paid examination, but ONLY after the
opposite direction has an established published verdict (earlier in this
run, or a same-content continuation/cache hit). A reverse candidate whose
opposite direction was examined and REJECTED is always examined.

Failing-first: unordered_mirror_key + known_published_keys do not exist
yet, so these tests fail until the implementation lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codatiel_i2.semdup_dev import editorial_shadow as ES
from tests import semdup_testkit as T


FA = "def fa(x):\n    return x + 1\n"
FB = "def fb(x):\n    return x + 2\n"


def _judge(tmp_path, monkeypatch, pairs, assess, edit,
           known_published_keys=None):
    T.backends(monkeypatch)
    repo, base, head, db = T.indexed_two_file_repo(
        tmp_path, {"a.py": FA, "b.py": FB},
        {"b.py": FB + "# head\n"})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    kw = {}
    if known_published_keys is not None:
        kw["known_published_keys"] = known_published_keys
    return ES.review_candidate_pairs(
        pairs, repo="o/r", repo_path=repo, merged_db=db,
        base_rev=base, head_rev=head, head_tree=tmp_path,
        base_tree=tmp_path, run_dir=run_dir, ceiling_usd=2.0,
        assess_fn=assess, edit_fn=edit, **kw)


def test_unordered_mirror_key_matches_dedup():
    from codatiel_i2.semdup_dev import stage as ST
    k1 = ES.unordered_mirror_key({"path": "a.py", "symbol": "f"},
                                 {"path": "b.py", "symbol": "g"})
    k2 = ES.unordered_mirror_key({"path": "b.py", "symbol": "g"},
                                 {"path": "a.py", "symbol": "f"})
    assert k1 == k2
    # Same format the post-hoc mirror consolidation uses: sorted
    # path::symbol pair.
    assert k1 == "~~".join(sorted(("a.py::f", "b.py::g")))
    # _dedup_mirrors routes through the shared key: break the key and
    # the dedup breaks too.
    import codatiel_i2.semdup_dev.editorial_shadow as ESmod
    real = ESmod.unordered_mirror_key
    try:
        ESmod.unordered_mirror_key = lambda *a: (_ for _ in ()).throw(
            AssertionError("key fn must be used"))
        with pytest.raises(AssertionError):
            ST._dedup_mirrors([{"target": {"path": "a.py", "symbol": "f"},
                                "counterpart": {"path": "b.py",
                                                "symbol": "g"}}])
    finally:
        ESmod.unordered_mirror_key = real
    # And unchanged behavior: twins collapse, first kept.
    kept, mirrors = ST._dedup_mirrors(
        [{"target": {"path": "a.py", "symbol": "f"},
          "counterpart": {"path": "b.py", "symbol": "g"}},
         {"target": {"path": "b.py", "symbol": "g"},
          "counterpart": {"path": "a.py", "symbol": "f"}}])
    assert len(kept) == 1 and len(mirrors) == 1
    assert mirrors[0]["reason"] == "mirror_of_published"


def test_mirror_key_is_symbol_granular_by_design():
    # Same path::symbol at different lines shares one key — matching
    # the post-hoc dedup, which would collapse the twin even after a
    # paid examination. The pre-skip saves that examination; no
    # publication is lost versus the old behavior.
    k1 = ES.unordered_mirror_key({"path": "a.py", "symbol": "f"},
                                 {"path": "b.py", "symbol": "g"})
    assert ES.mirror_skip_decision({"path": "b.py", "symbol": "g"},
                                   {"path": "a.py", "symbol": "f"},
                                   {k1}) is True


@pytest.mark.parametrize("first,kept_sym", [
    (("a.py", "fa", "b.py", "fb"), "fa"),
    (("b.py", "fb", "a.py", "fa"), "fb"),
])
def test_first_published_wins_regardless_of_order(
        tmp_path, monkeypatch, first, kept_sym):
    # Both directions examined in arrival order would each publish;
    # the pre-skip keeps exactly the first and never examines the twin.
    calls, assess, edit = T.recording_calls()
    a, b = ("a.py", "fa", "b.py", "fb"), ("b.py", "fb", "a.py", "fa")
    first_pair = T.pair(*first)
    second_pair = T.pair(*(b if first == a else a))
    out = _judge(tmp_path, monkeypatch, [first_pair, second_pair],
                 assess, edit)
    assert out["ok"] is True
    assert len(out["published"]) == 1
    assert out["published"][0]["target"]["symbol"] == kept_sym
    assert calls == ["assess", "edit"]
    assert out["mirror_skipped"] == 1
    assert out["suppressed"][0]["reason"] == "mirror_of_published"
    assert out["mirror_saved_usd_est"] >= 0.0
    assert out["suppressed"][0]["reason"] == "mirror_of_published"


def test_no_skip_when_twin_rejected(tmp_path, monkeypatch):
    calls = []

    def assess(**kw):
        calls.append("assess")
        return T.reject_assess(**kw)

    out = _judge(tmp_path, monkeypatch,
                 [T.pair("a.py", "fa", "b.py", "fb"),
                  T.pair("b.py", "fb", "a.py", "fa")],
                 assess, lambda **kw: (_ for _ in ()).throw(
                     AssertionError("edit must not run")))
    assert calls == ["assess", "assess"]
    assert out["mirror_skipped"] == 0
    reasons = {r["reason"] for r in out["suppressed"]}
    assert reasons == {"terra_no_lead"}
    assert not out["published"]


def test_known_published_keys_skip_without_examination(tmp_path,
                                                        monkeypatch):
    key = ES.unordered_mirror_key({"path": "a.py", "symbol": "fa"},
                                  {"path": "b.py", "symbol": "fb"})

    def assess(**kw):
        raise AssertionError("assess must not run")

    def edit(**kw):
        raise AssertionError("edit must not run")

    out = _judge(tmp_path, monkeypatch,
                 [T.pair("a.py", "fa", "b.py", "fb"),
                  T.pair("b.py", "fb", "a.py", "fa")],
                 assess, edit, known_published_keys={key})
    assert out["mirror_skipped"] == 2
    assert not out["published"]
    assert {r["reason"] for r in out["suppressed"]} == \
        {"mirror_of_published"}


def test_skipped_rows_never_cached_or_revisited(tmp_path, monkeypatch):
    from codatiel_i2.semdup_dev import cache as CACHE
    seen = []

    def assess(**kw):
        return T.publish_assess(**kw)

    def edit(**kw):
        return T.publish_edit(**kw)

    def on_pair(row):
        seen.append(row)

    T.backends(monkeypatch)
    repo, base, head, db = T.indexed_two_file_repo(
        tmp_path, {"a.py": FA, "b.py": FB},
        {"b.py": FB + "# head\n"})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ES.review_candidate_pairs(
        [T.pair("a.py", "fa", "b.py", "fb"),
         T.pair("b.py", "fb", "a.py", "fa")],
        repo="o/r", repo_path=repo, merged_db=db, base_rev=base,
        head_rev=head, head_tree=tmp_path, base_tree=tmp_path,
        run_dir=run_dir, ceiling_usd=2.0, assess_fn=assess,
        edit_fn=edit, on_pair=on_pair)
    skipped = [r for r in seen if r.get("reason") == "mirror_of_published"]
    assert len(skipped) == 1
    row = skipped[0]
    assert row.get("completed") is False
    assert row.get("budget_refused") is False
    assert not row.get("assess_ran")
    # Pin the persistence contract: the gates that keep verdicts out
    # of the cache/continuation must keep rejecting preskip rows.
    assert CACHE.is_completed_row(row) is False
    assert CACHE.is_budget_stop_row(row) is False


def test_run_editorial_mirror_skip(tmp_path, monkeypatch):
    from codatiel_i2.semdup_dev import config as C
    T.backends(monkeypatch)
    repo, base, head, db = T.indexed_two_file_repo(
        tmp_path, {"a.py": FA, "b.py": FB},
        {"b.py": FB + "# head\n"})
    cfg = C.SemdupDevConfig(env="dev", state_dir=tmp_path / "state",
                            repos=["o/r"],
                            investigator_url="http://127.0.0.1:9/v1",
                            investigator_model="m",
                            investigator_expected="m",
                            challenger_url="http://127.0.0.1:9/v1",
                            challenger_model="m",
                            challenger_expected="m",
                            allow_embeddings=False)
    records = [
        {"status": "finding", "classification": "introduced",
         "target": {"path": "a.py", "symbol": "fa"},
         "counterparts": ["b.py::fb"]},
        {"status": "finding", "classification": "introduced",
         "target": {"path": "b.py", "symbol": "fb"},
         "counterparts": ["a.py::fa"]},
    ]
    out = ES.run_editorial(
        cfg=cfg, repo_path=repo, merged_db=db, base_rev=base,
        head_rev=head, records=records, run_dir=tmp_path,
        head_tree=tmp_path, base_tree=tmp_path,
        assess_fn=T.publish_assess, edit_fn=T.publish_edit)
    assert out["ok"] is True
    assert len(out["published"]) == 1
    assert out["mirror_skipped"] == 1
    assert out["suppressed"][0]["reason"] == "mirror_of_published"


MIRROR_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "mirror01_replay.json")
    .read_text())["cases"]


def test_mirror_seed_keys_only_published():
    from codatiel_i2.semdup_dev import stage as ST
    twin = {"target": {"path": "a.py", "symbol": "f"},
            "counterpart": {"path": "b.py", "symbol": "g"}}
    rows = [
        dict(twin, path="published"),
        dict(twin, path="suppressed", reason="terra_no_lead"),
        dict(twin, path="suppressed", reason="deterministic_drop"),
        dict(twin, path="suppressed", reason="budget_stop",
             budget_refused=True, completed=False),
        dict(twin, path="suppressed", reason="stale_superseded"),
        {"target": {"path": "a.py", "symbol": "f"}, "path": "published"},
        "garbage",
        None,
    ]
    seeds = ST.mirror_seed_keys(rows)
    assert list(seeds) == [ES.unordered_mirror_key(
        twin["target"], twin["counterpart"])]
    assert ST.mirror_seed_keys([]) == {}
    assert ST.mirror_seed_keys(None) == {}


def test_record_preskip_savings_unknown_and_empty():
    out = {"mirror_skipped": 2, "scaffold_skipped": 0,
           "pairs": [{"assess_ran": True}]}
    ES._record_preskip_savings(out, None, None, __import__("time").monotonic())
    assert out["mirror_saved_usd_est"] is None
    assert out["mirror_saved_wall_s_est"] is not None
    out = {"mirror_skipped": 1, "scaffold_skipped": 0, "pairs": []}
    ES._record_preskip_savings(out, 1.0, 1.0, __import__("time").monotonic())
    assert out["mirror_saved_usd_est"] is None
    assert out["mirror_saved_wall_s_est"] is None
    out = {"mirror_skipped": 0, "scaffold_skipped": 0, "pairs": []}
    ES._record_preskip_savings(out, None, None, __import__("time").monotonic())
    assert out["mirror_saved_usd_est"] == 0.0


@pytest.mark.parametrize("case", MIRROR_CASES,
                         ids=[c["key"] for c in MIRROR_CASES])
def test_historical_mirror_would_preskip(case):
    """Every historical mirror had a published twin; with the twin
    established, the new rule skips pre-examination and keeps the
    published direction."""
    kept = case["kept_target"], case["kept_counterpart"]
    mirror = case["mirror_target"], case["mirror_counterpart"]
    key = ES.unordered_mirror_key(*kept)
    assert key == case["key"]
    assert key == ES.unordered_mirror_key(*mirror)
    # Twin established -> skip fires for the mirror direction...
    assert ES.mirror_skip_decision(mirror[0], mirror[1], {key}) is True
    # ...and never fires for a pair with no published twin.
    assert ES.mirror_skip_decision(mirror[0], mirror[1], set()) is False
