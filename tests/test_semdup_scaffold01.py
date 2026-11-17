"""SEMDUP-SCAFFOLD-01: deterministic pre-examination suppression of
test setup/teardown/fixture scaffolding.

Failing-first: scaffold helpers do not exist yet.

Rule (all pre-inference, no model judgment):
1. Both units live in test-only code (existing _is_test_path plus
   *-Tests directory convention).
2. Skeleton-shared lines (string literals masked, all whitespace
   stripped, trivial/comment lines dropped) are non-empty.
3. No shared line carries an assertion/verdict token.
4. Every shared line lies in the setup region (before the first
   assertion) or teardown region (after the last assertion) of BOTH
   units.
Fail open otherwise (including unparseable input).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codatiel_i2.semdup_dev import editorial_shadow as ES


FA_SETUP = (
    "def test_alpha(tmp_path):\n"
    "    root = tmp_path / \"alpha\"\n"
    "    root.mkdir()\n"
    "    store = make_store(root)\n"
    "    store.write(\"x\")\n"
    "    assert store.read() == \"x\"\n"
    "    assert store.count() == 1\n"
)
FB_SETUP = (
    "def test_beta(tmp_path):\n"
    "    root = tmp_path / \"beta\"\n"
    "    root.mkdir()\n"
    "    store = make_store(root)\n"
    "    store.write(\"y\")\n"
    "    assert store.names() == [\"beta\"]\n"
    "    assert store.count() == 2\n"
)
FA_LOGIC = (
    "def test_gamma(tmp_path):\n"
    "    root = tmp_path / \"gamma\"\n"
    "    root.mkdir()\n"
    "    store = make_store(root)\n"
    "    assert store.read() == \"expected\"\n"
    "    assert store.count() == 3\n"
)
FB_LOGIC = (
    "def test_delta(tmp_path):\n"
    "    root = tmp_path / \"delta\"\n"
    "    root.mkdir()\n"
    "    store = make_store(root)\n"
    "    assert store.read() == \"expected\"\n"
    "    assert store.count() == 3\n"
)


def _u(path, text):
    return {"path": path, "text": text}


def test_scaffold_shared_setup_skips():
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", FA_SETUP), _u("tests/test_b.py", FB_SETUP))
    assert dec["skip"] is True
    assert dec["shared"] >= 3


def test_scaffold_shared_assertions_fail_open():
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", FA_LOGIC), _u("tests/test_b.py", FB_LOGIC))
    assert dec["skip"] is False


def test_scaffold_non_test_path_fails_open():
    dec = ES.scaffold_preskip_decision(
        _u("src/mod.py", FA_SETUP), _u("tests/test_b.py", FB_SETUP))
    assert dec["skip"] is False


def test_scaffold_mixed_pair_fails_open():
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", FA_SETUP), _u("src/mod.py", FB_SETUP))
    assert dec["skip"] is False


def test_scaffold_no_shared_lines_fails_open():
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", "def test_a():\n    assert f(1) == 1\n"),
        _u("tests/test_b.py", "def test_b():\n    assert g(2) == 2\n"))
    assert dec["skip"] is False


def test_scaffold_midbody_sharing_fails_open():
    # Shared lines sit mid-body in A (after its first assertion) even
    # though they are setup-positioned in B: the region gate is
    # positional per unit, so this fails open (target side).
    a = ("def test_a():\n"
         "    assert ready_a()\n"
         "    x = setup()\n"
         "    y = use(x)\n"
         "    assert y == 1\n")
    b = ("def test_b():\n"
         "    x = setup()\n"
         "    y = use(x)\n"
         "    assert ready_b()\n"
         "    assert y == 2\n")
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", a), _u("tests/test_b.py", b))
    assert dec["skip"] is False
    assert "target" in dec["detail"]


def test_scaffold_teardown_only_sharing_skips():
    a = ("def test_a():\n"
         "    assert run() == 1\n"
         "    store.close()\n"
         "    cleanup(store)\n")
    b = ("def test_b():\n"
         "    assert run() == 2\n"
         "    store.close()\n"
         "    cleanup(store)\n")
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", a), _u("tests/test_b.py", b))
    assert dec["skip"] is True


def test_scaffold_literal_variants_in_asserts_fail_open():
    # Same test with literal-only differences inside assertions: the
    # shared lines carry verdicts, so this fails open.
    a = ("def test_a():\n"
         "    root = tmp / \"alpha\"\n"
         "    assert ok(root)\n")
    b = ("def test_b():\n"
         "    root = tmp / \"beta\"\n"
         "    assert ok(root)\n")
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", a), _u("tests/test_b.py", b))
    assert dec["skip"] is False


def test_scaffold_literal_variants_in_setup_skip():
    # Same setup modulo literal values, divergent assertions: skip.
    a = ("def test_a():\n"
         "    root = tmp / \"alpha\"\n"
         "    store = make_store(root)\n"
         "    assert store.count() == 1\n")
    b = ("def test_b():\n"
         "    root = tmp / \"beta\"\n"
         "    store = make_store(root)\n"
         "    assert store.done()\n")
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", a), _u("tests/test_b.py", b))
    assert dec["skip"] is True
    assert dec["shared"] >= 2


def test_scaffold_unittest_style_oracle_fails_open():
    a = ("def test_a(self):\n"
         "    store = make_store(\"a\")\n"
         "    self.assertEqual(store.read(), \"a\")\n")
    b = ("def test_b(self):\n"
         "    store = make_store(\"b\")\n"
         "    self.assertEqual(store.read(), \"b\")\n")
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", a), _u("tests/test_b.py", b))
    assert dec["skip"] is False


def test_scaffold_empty_unit_text_fails_open():
    dec = ES.scaffold_preskip_decision(
        {"path": "tests/test_a.py", "text": ""},
        {"path": "tests/test_b.py", "text": "x = 1\n"})
    assert dec["skip"] is False
    assert dec["detail"] == "empty unit"


def test_scaffold_trivial_closers_ignored():
    assert ES._scaffold_trivial("});") is True
    assert ES._scaffold_trivial("},") is True
    assert ES._scaffold_trivial('""""""') is True
    assert ES._scaffold_trivial("let x = 1") is False


def test_scaffold_garbage_never_raises():
    assert ES.scaffold_preskip_decision(None, None)["skip"] is False
    assert ES.scaffold_preskip_decision({}, {})["skip"] is False
    assert ES.scaffold_preskip_decision(
        {"path": "t.py"}, {"path": "t.py"})["skip"] is False


def test_scaffold_row_cache_contract(tmp_path):
    from codatiel_i2.semdup_dev import cache as CACHE
    row = {"path": "suppressed", "reason": "scaffold_setup",
           "completed": True, "budget_refused": False,
           "target": {"path": "a.py", "symbol": "f"},
           "counterpart": {"path": "b.py", "symbol": "g"},
           "head_rev": "h", "_scaffold_rule": ES.SCAFFOLD_RULE}
    assert ES.scaffold_cacheable(row) is False
    assert ES.scaffold_cacheable({"reason": "terra_no_lead"}) is True
    ident = {"k": "v"}
    CACHE.store(tmp_path, "o/r", 7, ident, row)
    got = CACHE.lookup(tmp_path, "o/r", 7, ident)
    assert got is not None and got.get("reason") == "scaffold_setup"
    from codatiel_i2.semdup_dev import stage as ST
    key = ST._pair_key({"path": "a.py", "symbol": "f"},
                       {"path": "b.py", "symbol": "g"})
    pairs = [{"target": {"path": "a.py", "symbol": "f"},
              "counterpart": {"path": "b.py", "symbol": "g"}}]
    stale = dict(row, **{"_scaffold_rule": "scaffold-00"})
    new, reused = ST._partition_new(pairs, {key: stale})
    assert len(new) == 1 and not reused
    new2, reused2 = ST._partition_new(pairs, {key: dict(row)})
    assert not new2 and len(reused2) == 1


def test_scaffold_throws_signature_not_verdict_but_throw_is():
    sig_a = ("def test_a():\n"
             "    x = setup()\n"
             "    assert x.ok()\n")
    sig_b = ("def test_b():\n"
             "    x = setup()\n"
             "    assert x.done()\n")
    # 'setup()' shared, asserts differ -> skip (no throw involved).
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", sig_a), _u("tests/test_b.py", sig_b))
    assert dec["skip"] is True
    # A bare `throw` statement is a failure oracle: fail open.
    ta = ("func testA() async throws {\n"
          "    let x = make()\n"
          "    if bad { throw MyError() }\n"
          "    assert x.ok()\n")
    tb = ("func testB() async throws {\n"
          "    let x = make()\n"
          "    if bad { throw MyError() }\n"
          "    assert x.done()\n")
    dec = ES.scaffold_preskip_decision(
        _u("Tests/a.swift", ta), _u("Tests/b.swift", tb))
    assert dec["skip"] is False
    # ...while a `throws` signature alone is not a verdict.
    tc = ("func testC() async throws {\n"
          "    let x = make()\n"
          "    assert x.ok()\n")
    td = ("func testD() async throws {\n"
          "    let x = make()\n"
          "    assert x.done()\n")
    dec = ES.scaffold_preskip_decision(
        _u("Tests/a.swift", tc), _u("Tests/b.swift", td))
    assert dec["skip"] is True


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", msg)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _repo2(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a_test.py").write_text(
        "def helper_a(tmp_path):\n"
        "    root = tmp_path / \"a\"\n"
        "    root.mkdir()\n"
        "    return root\n")
    (repo / "b_test.py").write_text(
        "def helper_b(tmp_path):\n"
        "    root = tmp_path / \"b\"\n"
        "    root.mkdir()\n"
        "    return root\n")
    _git(repo, "add", "-A")
    base = _commit(repo, "base")
    (repo / "b_test.py").write_text(
        "def helper_b(tmp_path):\n"
        "    root = tmp_path / \"b2\"\n"
        "    root.mkdir()\n"
        "    return root\n")
    _git(repo, "add", "-A")
    head = _commit(repo, "head")
    return repo, base, head


def _indexed(tmp_path: Path):
    from codatiel_i2.semdup_dev import index_state as IX
    repo, base, head = _repo2(tmp_path)
    state = tmp_path / "state"
    out = IX.ensure_base_index(repo, "o/r", head, state,
                               allow_embeddings=False)
    assert out["present"]
    return repo, base, head, IX.base_paths(state, "o/r", head)["db"]


def _backends(monkeypatch):
    from codatiel_i2 import providers as P

    def fake_resolve(name):
        return {"backend": name, "model": f"test-{name}",
                "provider": "test",
                "rate_usd_per_mtok": {"input": 1.0, "output": 1.0},
                "rate_status": "test"}

    monkeypatch.setattr(P, "resolve_backend", fake_resolve)
    monkeypatch.setattr(P, "build_model",
                        lambda name: (f"test-{name}", None))


def _publish_assess(**kw):
    return {"ok": True,
            "terra": {"overlap": True, "relationship": "equivalent",
                      "opportunity": "direct_reuse",
                      "shared_component": "x", "differences": []},
            "transcript": {"tool_calls": [], "final_text": "{}"}}


def _publish_edit(**kw):
    return {"ok": True,
            "editor": {"decision": "KEEP", "revised_comment": "share it",
                       "reason": "concrete"},
            "transcript": {"tool_calls": []}, "error": ""}


def test_integration_scaffold_pair_skipped_pre_examination(tmp_path,
                                                           monkeypatch):
    _backends(monkeypatch)
    repo, base, head, db = _indexed(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls = []

    def assess(**kw):
        calls.append("assess")
        return _publish_assess(**kw)

    def edit(**kw):
        calls.append("edit")
        return _publish_edit(**kw)

    out = ES.review_candidate_pairs(
        [{"target": {"path": "a_test.py", "symbol": "helper_a"},
          "counterpart": {"path": "b_test.py", "symbol": "helper_b"},
          "classification": "introduced"}],
        repo="o/r", repo_path=repo, merged_db=db, base_rev=base,
        head_rev=head, head_tree=tmp_path, base_tree=tmp_path,
        run_dir=run_dir, ceiling_usd=2.0, assess_fn=assess,
        edit_fn=edit)
    assert out["ok"] is True
    assert calls == []
    assert out["scaffold_skipped"] == 1
    assert len(out["published"]) == 0
    sup = out["suppressed"]
    assert len(sup) == 1 and sup[0]["reason"] == "scaffold_setup"
    full = out["pairs"][0]
    assert full.get("completed") is True
    assert full.get("budget_refused") is False
    assert out["mirror_skipped"] == 0


def test_integration_assertion_sharing_pair_examined(tmp_path,
                                                     monkeypatch):
    _backends(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a_test.py").write_text(
        "def test_a(tmp_path):\n"
        "    assert compute(tmp_path) == 1\n")
    (repo / "b_test.py").write_text(
        "def test_b(tmp_path):\n"
        "    assert compute(tmp_path) == 1\n")
    _git(repo, "add", "-A")
    base = _commit(repo, "base")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    head = out.stdout.strip()
    from codatiel_i2.semdup_dev import index_state as IX
    state = tmp_path / "state"
    assert IX.ensure_base_index(repo, "o/r", head, state,
                                allow_embeddings=False)["present"]
    db = IX.base_paths(state, "o/r", head)["db"]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls = []

    def assess(**kw):
        calls.append("assess")
        return _publish_assess(**kw)

    def edit(**kw):
        calls.append("edit")
        return _publish_edit(**kw)

    out = ES.review_candidate_pairs(
        [{"target": {"path": "a_test.py", "symbol": "test_a"},
          "counterpart": {"path": "b_test.py", "symbol": "test_b"},
          "classification": "introduced"}],
        repo="o/r", repo_path=repo, merged_db=db, base_rev=base,
        head_rev=head, head_tree=tmp_path, base_tree=tmp_path,
        run_dir=run_dir, ceiling_usd=2.0, assess_fn=assess,
        edit_fn=edit)
    assert calls == ["assess", "edit"]
    assert out["scaffold_skipped"] == 0
    assert len(out["published"]) == 1
