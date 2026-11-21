"""Shared SEMDUP budget allocation (ACTIVATE-02 Phase 3, isolated worktree).

Flag-gated (SEMDUP_SHARED_BUDGET=1), default off. No inference; testkit
fakes with spend-recording stubs so ceilings actually bind.
"""
import pytest

from codatiel_i2.semdup_dev import editorial_shadow as ES
from tests import semdup_testkit as TK


def test_terra_admitted_table():
    A = ES.terra_admitted
    assert A(0.05, 0.01, 1.0, 0.01) == (True, "ok")
    # completable but reserve would break
    assert A(0.04, 0.01, 0.055, 0.02) == (False, "editorial-reserve")
    # not completable
    assert A(0.05, 0.01, 0.05, 0.0) == (False, "shared-remainder-exhausted")
    assert A(None, 0.01, 1.0, 0.01) == (True, "ok")
    assert A(None, 0.01, 0.005, 0.01) == (False, "shared-remainder-exhausted")
    assert A(0.05, None, 1.0, 0.01) == (True, "ok")
    assert A(0.05, None, 0.005, 0.01) == (False, "shared-remainder-exhausted")
    assert A(None, 0.05, 0.03, 0.0) == (False, "shared-remainder-exhausted")
    assert A(0.05, 0.01, None, 0.01) == (
        False, "shared-remainder-exhausted")
    assert A(0.05, 0.01, float("nan"), 0.01) == (
        False, "shared-remainder-exhausted")
    assert A("junk", 0.01, 1.0, 0.01) == (
        False, "shared-remainder-exhausted")
    # boundary admits
    assert A(0.04, 0.01, 0.06, 0.01) == (True, "ok")


def test_editorial_reserve_bounds():
    R = ES.editorial_reserve_usd
    assert R(2.0, 0, 0.001) == pytest.approx(0.01)  # floor, idle Luna
    assert R(2.0, 3, 0.001) == pytest.approx(0.01)  # floor wins
    assert R(2.0, 20, 0.001) == pytest.approx(0.02)  # scales
    assert R(2.0, 10000, 0.001) == pytest.approx(1.0)  # capped at C/2
    assert R(0.0, 5, 0.001) == pytest.approx(0.0)  # no cap, no reserve
    assert R(2.0, 5, None) == pytest.approx(0.01)  # default mean
    assert R(2.0, 5, -1.0) == pytest.approx(0.01)  # bad mean -> default


def test_shared_remaining_unknown():
    assert ES.shared_remaining_usd(1.0, 0.2, 0.1, 0.0, 0.0) == pytest.approx(
        0.7)
    assert ES.shared_remaining_usd(1.0, None, 0.1, 0.0, 0.0) is None
    assert ES.shared_remaining_usd(1.0, 0.2, 0.1, None, 0.0) is None
    assert ES.shared_remaining_usd(1.0, 0.2, 0.1) is None  # unbounded
    assert ES.shared_remaining_usd(1.0, 0.9, 0.5, 0.0, 0.0) == 0.0  # clamped


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("SEMDUP_SHARED_BUDGET", raising=False)
    assert ES.shared_budget_enabled() is False
    monkeypatch.setenv("SEMDUP_SHARED_BUDGET", "1")
    assert ES.shared_budget_enabled() is True
    monkeypatch.setenv("SEMDUP_SHARED_BUDGET", "yes")
    assert ES.shared_budget_enabled() is False


def _spending_assess(terra, input_tokens):
    calls = {"n": 0}

    def fake(**kwargs):
        calls["n"] += 1
        ledger = kwargs.get("ledger")
        if ledger is not None:
            from types import SimpleNamespace
            try:
                ledger.record_response(
                    f"test-terra-{calls['n']}",
                    SimpleNamespace(requests=1,
                                    input_tokens=input_tokens,
                                    output_tokens=10),
                    cache_in_input=True)
            except Exception:
                pass
        return {"ok": True, "terra": dict(terra),
                "transcript": {"tool_calls": []}}
    return fake


def _pairs(n):
    return [TK.pair("a.py", f"s{i}", "b.py", f"s{i}") for i in range(n)]


def _indexed(tmp_path, monkeypatch, pairs):
    from codatiel_i2.semdup_dev import editorial_shadow as ES2
    TK.backends(monkeypatch)
    base = {f"{n}.py": "".join(f"def s{i}():\n    return {i}\n"
                               for i in range(8))
            for n in ("a", "b")}
    head = dict(base)
    head["a.py"] = base["a.py"] + "# head\n"
    repo, base_rev, head_rev, db = TK.indexed_two_file_repo(
        tmp_path, base, head)
    return repo, base_rev, head_rev, db


def test_shared_terra_uses_idle_luna_headroom(tmp_path, monkeypatch):
    """Run-2 scenario: Luna idle, fixed split strands Terra at half."""
    from codatiel_i2.semdup_dev import editorial_shadow as ES2
    repo, base, head, db = _indexed(tmp_path, monkeypatch, None)
    run = tmp_path / "run"
    run.mkdir()
    assess = _spending_assess(
        {"overlap": False, "relationship": "unrelated",
         "opportunity": "none", "shared_component": "",
         "differences": []}, 10000)  # $0.01/exam at test rate
    kw = dict(repo="o/r", repo_path=repo, merged_db=db,
              base_rev=base, head_rev=head, head_tree=tmp_path,
              base_tree=tmp_path, run_dir=run, ceiling_usd=0.10,
              assess_fn=assess, edit_fn=TK.fake_edit("DROP"),
              on_pair=None, is_stale=None, known_published_keys={},
              pr=1)
    monkeypatch.delenv("SEMDUP_SHARED_BUDGET", raising=False)
    fixed = ES2.review_candidate_pairs(_pairs(8), **kw)
    monkeypatch.setenv("SEMDUP_SHARED_BUDGET", "1")
    run2 = tmp_path / "run2"
    run2.mkdir()
    kw["run_dir"] = run2
    shared = ES2.review_candidate_pairs(_pairs(8), **kw)
    n_fixed = sum(1 for r in fixed["pairs"] if r.get("assess_ran"))
    n_shared = sum(1 for r in shared["pairs"] if r.get("assess_ran"))
    assert n_fixed < 8  # fixed split binds Terra at half
    assert n_shared == 8  # shared spends Luna's idle headroom
    assert n_shared > n_fixed
    assert float(shared.get("spend_usd") or 0) <= 0.10 + 1e-9


def test_shared_luna_completes_all_leads(tmp_path, monkeypatch):
    """Leads' edits always complete under shared allocation."""
    from codatiel_i2.semdup_dev import editorial_shadow as ES2
    repo, base, head, db = _indexed(tmp_path, monkeypatch, None)
    edits = []

    def edit(**kwargs):
        edits.append(1)
        return TK.fake_edit("KEEP")(**kwargs)

    assess = _spending_assess(
        {"overlap": True, "relationship": "equivalent",
         "opportunity": "direct_reuse",
         "shared_component": "add-one", "differences": []}, 5000)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("SEMDUP_SHARED_BUDGET", "1")
    out = ES2.review_candidate_pairs(
        _pairs(4), repo="o/r", repo_path=repo, merged_db=db,
        base_rev=base, head_rev=head, head_tree=tmp_path,
        base_tree=tmp_path, run_dir=run, ceiling_usd=0.10,
        assess_fn=assess, edit_fn=edit,
        on_pair=None, is_stale=None, known_published_keys={}, pr=1)
    assert len(edits) == 4
    assert len(out.get("published", [])) == 4


def test_reserve_zero_outstanding_is_floor():
    # F1 regression: idle Luna reserves the floor, never a phantom edit.
    assert ES.editorial_reserve_usd(2.0, 0, 0.05) == pytest.approx(0.01)
    assert ES.editorial_reserve_usd(2.0, 0, None) == pytest.approx(0.01)


def test_shared_release_lifecycle():
    st = ES.new_shared_state()
    assert st == {"outstanding": 0, "luna_edits": 0, "luna_spent": 0.0}
    st["outstanding"] = 3
    ES._shared_release(st)
    assert st["outstanding"] == 2
    ES._shared_release(st)
    ES._shared_release(st)
    ES._shared_release(st)  # clamped, never negative
    assert st["outstanding"] == 0
    assert ES.shared_mean_edit_usd(st) == pytest.approx(
        ES.SHARED_DEFAULT_MEAN_EDIT_USD)
    st.update({"luna_edits": 4, "luna_spent": 0.008})
    assert ES.shared_mean_edit_usd(st) == pytest.approx(0.002)


def test_reserve_refusal_is_recorded(tmp_path, monkeypatch):
    """A pair blocked by the editorial reserve is recorded, not dropped."""
    from codatiel_i2.semdup_dev import editorial_shadow as ES2
    repo, base, head, db = _indexed(tmp_path, monkeypatch, None)
    run = tmp_path / "run"
    run.mkdir()
    assess = _spending_assess(
        {"overlap": False, "relationship": "unrelated",
         "opportunity": "none", "shared_component": "",
         "differences": []}, 10000)
    monkeypatch.setenv("SEMDUP_SHARED_BUDGET", "1")
    out = ES2.review_candidate_pairs(
        _pairs(4), repo="o/r", repo_path=repo, merged_db=db,
        base_rev=base, head_rev=head, head_tree=tmp_path,
        base_tree=tmp_path, run_dir=run, ceiling_usd=0.015,
        assess_fn=assess, edit_fn=TK.fake_edit("DROP"),
        on_pair=None, is_stale=None, known_published_keys={}, pr=1)
    reasons = [r.get("reason") for r in out.get("suppressed", [])]
    assert "budget_stop" in reasons
    row = next(r for r in out["suppressed"]
               if r.get("reason") == "budget_stop")
    assert "editorial reserve" in row.get("detail", "")
    assert row.get("budget_reason") == "editorial-reserve"
