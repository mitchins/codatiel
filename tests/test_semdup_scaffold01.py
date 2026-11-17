"""SEMDUP-SCAFFOLD-01: deterministic pre-examination suppression of
test setup/teardown/fixture scaffolding.

Rule (all pre-inference, no model judgment):
1. Both units live in test-only code (existing _is_test_path plus
   *-Tests directory convention).
2. At least one recognized assertion exists in either unit or its
   sent file context (pairs with no verifiable verdict anywhere fail
   open, including helper-only and smoke-test pairs).
3. Skeleton-shared lines (string literals masked, brace-adjacent
   whitespace normalized, trivial/comment lines dropped) are
   non-empty.
4. No shared line carries an assertion/verdict token.
5. Every shared line lies in the setup region (before the first
   assertion) or teardown region (after the last assertion) of BOTH
   units.
Fail open otherwise (including unparseable input).
"""

from __future__ import annotations

from codatiel_i2.semdup_dev import editorial_shadow as ES
from tests import semdup_testkit as T


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


def _pair(t_path, t_sym, c_path, c_sym):
    return T.pair(t_path, t_sym, c_path, c_sym)


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


def test_scaffold_go_verdict_style_fails_open():
    a = ("func TestA(t *testing.T) {\n"
         "    x := setup(\"a\")\n"
         "    if x.N != 1 { t.Errorf(\"n=%d\", x.N) }\n"
         "}\n")
    b = ("func TestB(t *testing.T) {\n"
         "    x := setup(\"b\")\n"
         "    if x.N != 1 { t.Errorf(\"n=%d\", x.N) }\n"
         "}\n")
    dec = ES.scaffold_preskip_decision(
        _u("pkg/foo_test.go", a), _u("pkg/foo_test.go", b))
    assert dec["skip"] is False


def test_scaffold_contests_dir_is_not_test_only():
    dec = ES.scaffold_preskip_decision(
        {"path": "src/Contests/runner.py",
         "text": "def run_all():\n    setup()\n    assert done()\n"},
        {"path": "src/Contests/other.py",
         "text": "def run_all():\n    setup()\n    assert done()\n"})
    assert dec["skip"] is False
    assert ES._scaffold_is_test_path("PresentationTests/Foo.swift") is True
    assert ES._scaffold_is_test_path("src/Contests/runner.py") is False


def test_scaffold_spacing_normalization():
    # Inner spacing is preserved: distinct token sequences never merge.
    a = "def test_a():\n    x = load(a, b)\n    assert x.ok()\n"
    b = "def test_b():\n    x = load(a,b)\n    assert x.done()\n"
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", a), _u("tests/test_b.py", b))
    assert dec["skip"] is True
    c = "def test_c():\n    x = aandb\n    assert x.ok()\n"
    d = "def test_d():\n    x = a and b\n    assert x.done()\n"
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_c.py", c), _u("tests/test_d.py", d))
    assert dec["skip"] is False


def test_scaffold_assignment_spacing_normalized():
    # Only spacing around '=' differs: same setup line.
    a = "def test_a():\n    total = count(x)\n    assert total == 1\n"
    b = "def test_b():\n    total=count(x)\n    assert total == 2\n"
    dec = ES.scaffold_preskip_decision(
        _u("tests/test_a.py", a), _u("tests/test_b.py", b))
    assert dec["skip"] is True


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


def test_scaffold_context_assertions_count_for_union():
    # Assert-free units with no verifiable context fail open ...
    a = {"path": "tests/a_test.py",
         "text": "def setup_a():\n    x = make()\n    return x\n"}
    b = ("def setup_b():\n    x = make()\n    return x\n")
    dec = ES.scaffold_preskip_decision(a, {"path": "tests/b_test.py",
                                           "text": b})
    assert dec["skip"] is False
    assert dec["detail"] == "no recognized assertions"
    # ... while the same pair with a verified context skips.
    ctx = ["    #expect(store.count() == 1)"]
    a2 = dict(a, context=ctx)
    b2 = {"path": "tests/b_test.py", "text": b, "context": ctx}
    dec = ES.scaffold_preskip_decision(a2, b2)
    assert dec["skip"] is True


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


def test_integration_scaffold_pair_skipped_pre_examination(tmp_path,
                                                           monkeypatch):
    base_files = {
        "a_test.py": ("def test_a(tmp_path):\n"
                      "    root = tmp_path / \"a\"\n"
                      "    root.mkdir()\n"
                      "    store = make_store(root)\n"
                      "    assert store.count() == 1\n"),
        "b_test.py": ("def test_b(tmp_path):\n"
                      "    root = tmp_path / \"b\"\n"
                      "    root.mkdir()\n"
                      "    store = make_store(root)\n"
                      "    assert store.names() == [\"b\"]\n"),
    }
    head_files = dict(base_files)
    head_files["b_test.py"] = head_files["b_test.py"] + "# head\n"
    calls, assess, edit = T.recording_calls()
    out = T.run_judged(tmp_path, monkeypatch, base_files, head_files,
                       [_pair("a_test.py", "test_a",
                              "b_test.py", "test_b")],
                       assess, edit)
    assert out["ok"] is True
    assert calls == []
    assert out["scaffold_skipped"] == 1
    assert len(out["published"]) == 0
    sup = out["suppressed"]
    assert len(sup) == 1 and sup[0]["reason"] == "scaffold_setup"
    full = out["pairs"][0]
    assert full.get("completed") is True
    assert full.get("budget_refused") is False
    assert full.get("_scaffold_rule") == ES.SCAFFOLD_RULE
    assert out["mirror_skipped"] == 0


def test_integration_helper_pair_without_asserts_fails_open(tmp_path,
                                                             monkeypatch):
    # Neither side verifies anything: without a recognizable verdict
    # the pair could be unasserted logic, so it must be examined.
    base_files = {
        "a_test.py": ("def helper_a(tmp_path):\n"
                      "    root = tmp_path / \"a\"\n"
                      "    root.mkdir()\n"
                      "    return root\n"),
        "b_test.py": ("def helper_b(tmp_path):\n"
                      "    root = tmp_path / \"b\"\n"
                      "    root.mkdir()\n"
                      "    return root\n"),
    }
    head_files = dict(base_files)
    head_files["b_test.py"] = base_files["b_test.py"] + "# head\n"
    calls, assess, edit = T.recording_calls()
    out = T.run_judged(tmp_path, monkeypatch, base_files, head_files,
                       [_pair("a_test.py", "helper_a",
                              "b_test.py", "helper_b")],
                       assess, edit)
    assert calls == ["assess", "edit"]
    assert out["scaffold_skipped"] == 0
    assert len(out["published"]) == 1


def test_integration_assertion_sharing_pair_examined(tmp_path,
                                                        monkeypatch):
    base_files = {
        "a_test.py": "def test_a(tmp_path):\n    assert compute(tmp_path) == 1\n",
        "b_test.py": "def test_b(tmp_path):\n    assert compute(tmp_path) == 1\n",
    }
    head_files = dict(base_files)
    head_files["b_test.py"] = base_files["b_test.py"] + "# head\n"
    calls, assess, edit = T.recording_calls()
    out = T.run_judged(tmp_path, monkeypatch, base_files, head_files,
                       [_pair("a_test.py", "test_a",
                              "b_test.py", "test_b")],
                       assess, edit)
    assert calls == ["assess", "edit"]
    assert out["scaffold_skipped"] == 0
    assert len(out["published"]) == 1
