"""DEV-01 deterministic integration tests (no model calls).

Covers acceptance items that must hold without inference:
  #4  preliminary actionable verdict downgraded by the challenger is
      never published as actionable;
  #5  introduced/worsened vs pre-existing classification;
  #6  rename/move is not fictitious duplication;
  #7  identical symbol names in different directories resolve exactly;
  #8  replayed PR events cause no repeated inference;
  #9  changed-file overlay matches a fresh-index reference;
  #10 missing embeddings / stale indexes give honest coverage/status.

Plus: GAP-4 exact-first resolution, E-5 served-id verification, and
DEV-only config guards (TEST env refused).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from codatiel_i2.semdup_dev import config as C
from codatiel_i2.semdup_dev import delta as D
from codatiel_i2.semdup_dev import findings as F
from codatiel_i2.semdup_dev import index_state as IX
from codatiel_i2.semdup_dev import merged_view as MV
from codatiel_i2.semdup_dev import resolve as R
from codatiel_i2.semdup_dev import worker as W
from codatiel_i2.semdup_vendored.semdup import check as CHK
from codatiel_i2.semdup_vendored.semdup import index_cmd as IC
from codatiel_i2.semdup_vendored.semdup import retrieve as RT
from codatiel_i2.semdup_vendored.semdup import staged as ST


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", msg)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _repo_with(base_files: dict, head_files: dict, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, text in base_files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    base = _commit(repo, "base")
    for rel in set(head_files) - set(base_files):
        pass
    for rel, text in head_files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for rel in set(base_files) - set(head_files):
        (repo / rel).unlink()
    _git(repo, "add", "-A")
    head = _commit(repo, "head")
    return repo, base, head


def _cfg(tmp_path: Path, repos=("o/r",), **kw):
    args = {"env": "dev", "state_dir": tmp_path / "state",
            "repos": list(repos),
            "investigator_url": "http://127.0.0.1:9/v1",
            "investigator_model": "m", "investigator_expected": "m",
            "challenger_url": "http://127.0.0.1:9/v1",
            "challenger_model": "m", "challenger_expected": "m",
            "allow_embeddings": False}
    args.update(kw)
    return C.SemdupDevConfig(**args)


def _store(state_dir: Path):
    from codatiel_i2 import event_store as store_mod
    state_dir.mkdir(parents=True, exist_ok=True)
    return store_mod.Store(state_dir / "events.sqlite")


# -- #4: challenger downgrade never published as actionable ----------------

def test_downgraded_verdict_never_actionable():
    rep = {"verdict": {"action": "reuse_existing",
                       "recommended_family": ["e.py::g"],
                       "proposed_boundary": "g()",
                       "target_responsibility": "x"},
           "assessment": {"action": "investigate",
                          "relationship": "unknown",
                          "proposal": None, "observed_differences": [],
                          "remain_separate": ["challenger fatal"],
                          "member_notes": [],
                          "validation": {"level": "unresolved",
                                         "unresolved": ["fatal"]}},
           "stage_d": {"challenged": True, "fatal_claims": ["fatal"]},
           "finalists": [], "retrieval_coverage": "reduced",
           "wall_s": 1.0, "report_sha256": "x"}
    rec = F.build_finding(repo="o/r", pr=1, base_rev="b" * 40,
                           head_rev="h" * 40, from_head="b" * 40,
                           delta_fingerprint="f" * 16,
                           target={"path": "a.py", "symbol": "f"},
                           report=rep,
                           provenance={"detector": "t"},
                           classification="introduced")
    assert rec["status"] == "investigate"
    assert rec["recommended_action"] == "investigate"
    assert rec["preliminary_published"] is False
    assert rec["preliminary_action"] == "reuse_existing"


def test_missing_challenge_defers_actionable_verdict():
    rep = {"verdict": {"action": "extract_shared_part",
                       "recommended_family": ["e.py::g"],
                       "proposed_boundary": "g()"},
           "finalists": [], "retrieval_coverage": "reduced",
           "wall_s": 1.0, "report_sha256": "x"}
    rec = F.build_finding(repo="o/r", pr=1, base_rev="b" * 40,
                           head_rev="h" * 40, from_head="b" * 40,
                           delta_fingerprint="f" * 16,
                           target={"path": "a.py", "symbol": "f"},
                           report=rep, provenance={},
                           classification="introduced")
    assert rec["status"] == "deferred"
    assert rec["recommended_action"] == "investigate"


# -- #5/#6: classification ----------------------------------------------------

PY_F = "def f(x):\n    return x + 1\n"
PY_G = "def g(x):\n    return x * 2\n"
PY_F_TWEAK = "def f(x):\n    return x + 2\n"
PY_COPY = "def h(x):\n    return x + 1\n"


def _two_revs(tmp_path: Path):
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    return base, head


def test_introduced_vs_preexisting(tmp_path: Path):
    base, head = _two_revs(tmp_path)
    (base / "a.py").write_text(PY_F + "\n" + PY_G)
    (head / "a.py").write_text(PY_F + "\n" + PY_G)
    (head / "b.py").write_text(PY_COPY)
    base_db, head_db = tmp_path / "b.sqlite", tmp_path / "h.sqlite"
    IC.build(base, base_db)
    IC.build(head, head_db)
    out = D.classify_delta(base_db, head_db)
    assert ("b.py", "h") in out["units"]["added"]
    assert ("a.py", "f") in out["units"]["unchanged"]
    assert ("a.py", "g") in out["units"]["unchanged"]
    assert ("b.py", "h") in out["introduced_or_changed"]


def test_move_is_not_new_code(tmp_path: Path):
    base, head = _two_revs(tmp_path)
    (base / "a.py").write_text(PY_F)
    (head / "pkg").mkdir()
    (head / "pkg" / "a.py").write_text(PY_F)
    base_db, head_db = tmp_path / "b.sqlite", tmp_path / "h.sqlite"
    IC.build(base, base_db)
    IC.build(head, head_db)
    out = D.classify_delta(base_db, head_db)
    assert ("pkg/a.py", "f") in out["units"]["moved"]
    assert ("pkg/a.py", "f") not in out["introduced_or_changed"]


def test_changed_member_is_worsened_candidate(tmp_path: Path):
    base, head = _two_revs(tmp_path)
    (base / "a.py").write_text(PY_F)
    (head / "a.py").write_text(PY_F_TWEAK)
    base_db, head_db = tmp_path / "b.sqlite", tmp_path / "h.sqlite"
    IC.build(base, base_db)
    IC.build(head, head_db)
    out = D.classify_delta(base_db, head_db)
    assert ("a.py", "f") in out["units"]["changed"]
    assert ("a.py", "f") in out["introduced_or_changed"]


# -- #7: exact resolution -------------------------------------------------------

def test_same_symbol_different_dirs(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "pkg1").mkdir()
    (base / "pkg2").mkdir()
    (base / "pkg1" / "util.py").write_text(PY_F)
    (base / "pkg2" / "util.py").write_text(PY_G)
    db = tmp_path / "b.sqlite"
    IC.build(base, db)
    conn = sqlite3.connect(str(db))
    try:
        r1 = R.resolve_target(conn, "pkg1/util.py", "f")
        r2 = R.resolve_target(conn, "pkg2/util.py", "g")
        assert r1["ok"] and r1["path"] == "pkg1/util.py"
        assert r2["ok"] and r2["path"] == "pkg2/util.py"
        # basename-only path is refused, never guessed.
        bad = R.resolve_target(conn, "other/util.py", "f")
        assert not bad["ok"] and "basename" in bad["detail"]
        # engine exact-first: same basename, same symbol, two dirs.
        row = ST._unit_row(conn, "pkg2/util.py", "g")
        assert row["path"] == "pkg2/util.py"
    finally:
        conn.close()


def test_ambiguous_bare_symbol_raises():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE units(id INTEGER PRIMARY KEY, path TEXT,"
                 " symbol TEXT, start_line INT, end_line INT, text TEXT)")
    conn.execute("INSERT INTO units VALUES(1,'a/util.py','f',1,2,'t1')")
    conn.execute("INSERT INTO units VALUES(2,'b/util.py','f',1,2,'t2')")
    try:
        with pytest.raises(LookupError):
            ST._unit_row(conn, "util.py", "f")
        row = ST._unit_row(conn, "b/util.py", "f")
        assert row["path"] == "b/util.py" and row["text"] == "t2"
    finally:
        conn.close()


# -- #9: overlay == fresh reference ----------------------------------------------

def test_overlay_matches_fresh_reference(tmp_path: Path):
    from codatiel_i2.semdup_vendored.semdup import overlay as OV
    base = tmp_path / "base"
    base.mkdir()
    (base / "a.py").write_text(PY_F + "\n" + PY_G)
    (base / "gone.py").write_text(PY_F)
    base_db = tmp_path / "b.sqlite"
    IC.build(base, base_db)
    head = tmp_path / "head"
    head.mkdir()
    (head / "a.py").write_text(PY_F_TWEAK + "\n" + PY_G + "\n" + PY_COPY)
    stats = OV.build_overlay(base_db, head, tmp_path / "ov", embed=False)
    assert stats["deleted"] == ["gone.py"]
    merged_db = tmp_path / "merged.sqlite"
    merged = MV.build_merged_view(base_db, tmp_path / "ov", merged_db)
    assert merged["ok"] and "reduced" in merged["retrieval_coverage"]
    ref_db = tmp_path / "ref.sqlite"
    IC.build(head, ref_db)

    def sig(db):
        c = sqlite3.connect(str(db))
        try:
            return sorted(c.execute(
                "SELECT path, symbol, text FROM units").fetchall())
        finally:
            c.close()
    assert sig(merged_db) == sig(ref_db)
    # retrieval parity on the merged view vs the fresh reference.
    for db in (merged_db, ref_db):
        c = sqlite3.connect(str(db))
        try:
            tid = c.execute("SELECT id FROM units WHERE path=? AND symbol=?",
                            ("a.py", "h")).fetchone()[0]
            det, _ = RT.all_channel_neighbors(c, tid, k=12, emb=None)
        finally:
            c.close()
        got = sorted(f"{e['path']}::{e['symbol']}" for e in det)
        if db == merged_db:
            merged_got = got
        else:
            assert got == merged_got


# -- #10: honest coverage ----------------------------------------------------------

def test_missing_sidecars_report_reduced(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "a.py").write_text(PY_F)
    base_db = tmp_path / "b.sqlite"
    IC.build(base, base_db)
    head = tmp_path / "head"
    head.mkdir()
    (head / "a.py").write_text(PY_F_TWEAK)
    from codatiel_i2.semdup_vendored.semdup import overlay as OV
    OV.build_overlay(base_db, head, tmp_path / "ov", embed=False)
    merged = MV.build_merged_view(base_db, tmp_path / "ov",
                                  tmp_path / "m.sqlite")
    assert merged["ok"] and merged["emb"] is None
    assert "reduced" in merged["retrieval_coverage"]


def test_stale_overlay_detected(tmp_path: Path):
    repo, base, head = _repo_with({"a.py": PY_F}, {"a.py": PY_F_TWEAK},
                                  tmp_path)
    cfg = _cfg(tmp_path)
    assert IX.ensure_base_index(repo, "o/r", base,
                                cfg.state_dir,
                                allow_embeddings=False)["present"]
    base_db = IX.base_paths(cfg.state_dir, "o/r", base)["db"]
    built = IX.build_overlay(repo, "o/r", 1, base, head, cfg.state_dir,
                             base_db, allow_embeddings=False)
    assert built["ok"]
    assert IX.overlay_status(cfg.state_dir, "o/r", 1, head,
                             base_db)["present"]
    # A different base DB under the same rev is stale, never trusted.
    other = tmp_path / "other.sqlite"
    IC.build(tmp_path / "repo", other)
    if other.read_bytes() == base_db.read_bytes():
        (tmp_path / "repo" / "zzz.py").write_text(PY_G)
        subprocess.run(["git", "-C", str(tmp_path / "repo"), "add", "-A"],
                       check=True, capture_output=True)
        IC.build(tmp_path / "repo", other)
    st = IX.overlay_status(cfg.state_dir, "o/r", 1, head, other)
    assert not st["present"] and st.get("stale")


# -- #8: no repeated inference ------------------------------------------------------

def _fake_report(action="investigate"):
    return {"verdict": {"action": "investigate",
                        "recommended_family": [],
                        "proposed_boundary": None},
            "assessment": {"action": action, "relationship": "unknown",
                           "proposal": None, "observed_differences": [],
                           "remain_separate": [], "member_notes": [],
                           "validation": {"level": "characterised",
                                          "unresolved": []}},
            "stage_d": {"challenged": True, "fatal_claims": []},
            "finalists": [], "retrieval_coverage": "reduced: test",
            "wall_s": 0.1, "report_sha256": "t"}


def test_replays_cause_no_repeated_inference(tmp_path: Path, monkeypatch):
    repo, base, head = _repo_with({"a.py": PY_F}, {"a.py": PY_F_TWEAK},
                                  tmp_path)
    cfg = _cfg(tmp_path)
    store = _store(cfg.state_dir)
    calls = {"n": 0}

    def fake_investigate(*a, **k):
        calls["n"] += 1
        return _fake_report()

    monkeypatch.setattr(ST, "investigate", fake_investigate)
    assert IX.ensure_base_index(repo, "o/r", base, cfg.state_dir,
                                allow_embeddings=False)["present"]
    kw = {"cfg": cfg, "store": store, "repo_path": repo, "repo": "o/r",
          "pr": 7, "base_rev": base, "head_rev": head}
    first = W.process_delta(delivery_id="d1", **kw)
    assert first["status"] in ("complete", "partial", "deferred")
    n_after_first = calls["n"]
    replay = W.process_delta(delivery_id="d1", **kw)
    assert replay["status"] == "skipped" and calls["n"] == n_after_first
    new_delivery = W.process_delta(delivery_id="d2", **kw)
    assert new_delivery["status"] == "skipped" and calls["n"] == n_after_first
    store.close()


def test_missing_base_index_defers_without_inference(tmp_path: Path,
                                                     monkeypatch):
    repo, base, head = _repo_with({"a.py": PY_F}, {"a.py": PY_F_TWEAK},
                                  tmp_path)
    cfg = _cfg(tmp_path)
    store = _store(cfg.state_dir)
    calls = {"n": 0}

    def fake_investigate(*a, **k):
        calls["n"] += 1
        return _fake_report()

    monkeypatch.setattr(ST, "investigate", fake_investigate)
    out = W.process_delta(cfg=cfg, store=store, repo_path=repo, repo="o/r",
                          pr=3, base_rev=base, head_rev=head,
                          delivery_id="dx")
    assert out["status"] in ("deferred", "indexing")
    assert calls["n"] == 0
    store.close()


# -- overlay fusion mapping (synthetic sidecars, no model) ----------------

def test_fuse_embeddings_id_mapping(tmp_path: Path):
    import numpy as np
    base_db = tmp_path / "abcdef1234567890.sqlite"
    base_db.write_bytes(b"base-db-bytes")
    over_dir = tmp_path / "ov"
    over_dir.mkdir()
    base_vecs = np.eye(4, dtype="float32")
    np.savez_compressed(base_db.parent / "abcdef1234567890.harrier.npz",
                        ids=np.array([11, 12, 13, 14]),
                        vectors=base_vecs,
                        model=np.array("m"), sha=np.array("s"))
    over_vecs = np.eye(4, dtype="float32") * 2
    np.savez_compressed(over_dir / "overlay_harrier_ids.npz",
                        ids=np.array([51, 52]),
                        vectors=over_vecs[:2],
                        model=np.array("m"), sha=np.array("s"))
    provenance = {1: ["base", 11], 2: ["base", 12], 3: ["base", 99],
                  4: ["overlay", 51], 5: ["overlay", 52]}
    examinable = {1, 2, 4, 5}
    emb, coverage = MV._fuse_embeddings(base_db, over_dir, provenance,
                                        examinable)
    assert coverage == "full" and emb is not None
    assert emb["ids"] == [1, 2, 4, 5]
    assert emb["model"] == "m"
    # row 3 (base id 99, vectorless) is excluded but not examinable.
    assert 3 not in emb["ids"]
    # a missing examinable vector forces reduced, honestly.
    emb2, cov2 = MV._fuse_embeddings(base_db, over_dir, provenance,
                                     examinable | {3})
    assert emb2 is not None and cov2.startswith("reduced:")
    assert 3 not in emb2["ids"]


# -- E-5: served identity --------------------------------------------------------------

def test_served_id_mismatch_fails_closed(monkeypatch):
    from codatiel_i2.semdup_vendored.semdup import inference as I

    class Resp:
        status = 200
        headers = {"x-request-id": "t"}

        def read(self):
            return json.dumps(
                {"model": "impostor",
                 "choices": [{"message": {"content": "{}"}}],
                 "usage": {}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp())
    with pytest.raises(I.ModelIdentityError):
        I.chat("http://x/v1", "wanted", [{"role": "user", "content": "hi"}],
               expected_model_id="wanted")


# -- config guards --------------------------------------------------------------------------

def test_test_env_refused(monkeypatch):
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_DEV_REPOS", "o/r")
    with pytest.raises(C.SemdupConfigError):
        C.config_from_env()


def test_empty_repos_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("CODATIEL_ENV", "dev")
    monkeypatch.setenv("CODATIEL_DEV_STATE", str(tmp_path))
    monkeypatch.delenv("CODATIEL_DEV_REPOS", raising=False)
    with pytest.raises(C.SemdupConfigError):
        C.config_from_env()
