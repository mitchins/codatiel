"""SEMDUP-07 Track A tests: parallel editorial shadow (no model calls).

- candidate_pairs: deterministic selection + skip reasons + budget cap.
- run_editorial: degrades without backends; full pass with injected
  Terra/Luna fakes; wrapper short-circuit; unpublishable editor output
  suppresses; never raises.
- worker wiring: editorial runs alongside findings/advisories and is
  persisted separately; duplicate deliveries and identical fingerprints
  cause no repeat work; force-pushes examine only new material;
  grandfather path records baseline without editorial; editorial
  failure keeps the run complete; flag-off disables the pass.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codatiel_i2.semdup_dev import config as C
from codatiel_i2.semdup_dev import editorial_shadow as ES
from codatiel_i2.semdup_dev import index_state as IX
from codatiel_i2.semdup_dev import worker as W

LIB = "def helper(x):\n    return x + 1\n"
NEW = "def helper2(x):\n    return x + 1\n"
NEW2 = "def helper3(x):\n    return x + 1\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", msg)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _repo(tmp_path: Path, base_files: dict, head_files: dict):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, text in base_files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    base = _commit(repo, "base")
    for rel, text in head_files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for rel in set(base_files) - set(head_files):
        (repo / rel).unlink()
    _git(repo, "add", "-A")
    head = _commit(repo, "head")
    return repo, base, head


def _cfg(tmp_path: Path, **kw):
    args = {"env": "dev", "state_dir": tmp_path / "state",
            "repos": ["o/r"],
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


def _backends(monkeypatch):
    from codatiel_i2 import providers as P

    def fake_resolve(name):
        return {"backend": name, "model": f"test-{name}",
                "provider": "test",
                "rate_usd_per_mtok": {"input": 1.0, "output": 1.0},
                "rate_status": "test"}

    def fake_build(name):
        return (f"test-{name}", None)

    monkeypatch.setattr(P, "resolve_backend", fake_resolve)
    monkeypatch.setattr(P, "build_model", fake_build)


def _fake_assess(terra: dict):
    def fake(**kwargs):
        return {"ok": True, "terra": dict(terra),
                "transcript": {"tool_calls": []}}
    return fake


def _fake_edit(decision: str, ok: bool = True):
    def fake(**kwargs):
        if not ok:
            return {"ok": False, "transcript": {"tool_calls": []},
                    "error": "boom", "editor": None}
        return {"ok": True,
                "editor": {"decision": decision,
                           "revised_comment": "use helper()",
                           "reason": "concrete reuse"},
                "transcript": {"tool_calls": []}, "error": ""}
    return fake


def _finding(target_path="new.py", target_sym="helper2",
             counterparts=("lib.py::helper",), status="finding"):
    return {"status": status, "classification": "introduced",
            "target": {"path": target_path, "symbol": target_sym},
            "counterparts": list(counterparts)}


# -- candidate_pairs --------------------------------------------------------

def test_candidate_pairs_selects_and_skips():
    records = [
        _finding(),
        _finding(target_path="a.py", target_sym="f",
                 counterparts=["oracles/x.py::g", "b.py::h#region",
                               "a.py::f", "no-separator",
                               "b.py::h"]),
        {"status": "deferred", "target": {"path": "c.py", "symbol": "q"},
         "counterparts": ["d.py::r"]},
    ]
    pairs, skipped = ES.candidate_pairs(records, 10)
    assert [(p["target"]["symbol"], p["counterpart"]["symbol"])
            for p in pairs] == [("helper2", "helper"), ("f", "h")]
    reasons = {s["reason"] for s in skipped}
    assert {"oracle/fragment excluded", "self-pair excluded",
            "counterpart unparseable"} <= reasons


def test_candidate_pairs_budget_cap():
    rec = _finding(counterparts=(f"m{i}.py::f{i}" for i in range(9)))
    rec["counterparts"] = [f"m{i}.py::f{i}" for i in range(9)]
    pairs, _ = ES.candidate_pairs([rec], 4)
    assert len(pairs) == 4


# -- run_editorial with injected models --------------------------------------

def _indexed(tmp_path: Path):
    repo, base, head = _repo(tmp_path, {"lib.py": LIB}, {"new.py": NEW,
                                                        "lib.py": LIB})
    state = tmp_path / "state"
    out = IX.ensure_base_index(repo, "o/r", head, state,
                               allow_embeddings=False)
    assert out["present"]
    db = IX.base_paths(state, "o/r", head)["db"]
    return repo, head, base, db


def test_run_editorial_needs_no_backend_by_degrading(tmp_path, monkeypatch):
    from codatiel_i2 import providers as P
    monkeypatch.setattr(P, "resolve_backend", _raising)
    out = ES.run_editorial(
        cfg=_cfg(tmp_path), repo_path=tmp_path, merged_db=tmp_path / "x",
        base_rev="b" * 40, head_rev="h" * 40,
        records=[_finding()], run_dir=tmp_path,
        head_tree=None, base_tree=None)
    assert out["enabled"] is True and out["ok"] is True
    assert out["stage"] == "no_head_tree"
    assert not out["published"]


def _raising(name):
    raise RuntimeError("no credentials")


def test_run_editorial_full_pass_with_fakes(tmp_path, monkeypatch):
    _backends(monkeypatch)
    repo, head, base, db = _indexed(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = ES.run_editorial(
        cfg=_cfg(tmp_path), repo_path=repo, merged_db=db,
        base_rev=base, head_rev=head, records=[_finding()],
        run_dir=run_dir, head_tree=tmp_path, base_tree=tmp_path,
        assess_fn=_fake_assess(
            {"overlap": True, "relationship": "dup",
             "opportunity": "direct_reuse", "shared_component": "add-one",
             "differences": []}),
        edit_fn=_fake_edit("KEEP"))
    assert out["ok"] is True
    assert len(out["published"]) == 1
    pub = out["published"][0]
    assert pub["decision"] == "KEEP"
    assert pub["anchors"]["target"]["sha256"]
    assert pub["head_rev"] == head
    assert out["models"]["terra"]["model"] == "test-terra"
    assert out["spend_usd"] == 0.0
    assert (run_dir / "spend_editorial_terra.jsonl").exists()


def test_run_editorial_suppresses_unpublishable(tmp_path, monkeypatch):
    _backends(monkeypatch)
    repo, head, base, db = _indexed(tmp_path)
    out = ES.run_editorial(
        cfg=_cfg(tmp_path), repo_path=repo, merged_db=db,
        base_rev=base, head_rev=head, records=[_finding()],
        run_dir=tmp_path, head_tree=tmp_path, base_tree=tmp_path,
        assess_fn=_fake_assess(
            {"overlap": True, "relationship": "dup",
             "opportunity": "direct_reuse", "shared_component": "s",
             "differences": []}),
        edit_fn=_fake_edit("KEEP", ok=False))
    assert out["ok"] is True
    assert not out["published"]
    assert out["suppressed"][0]["reason"] == "editor_unpublishable"


def test_run_editorial_wrapper_short_circuits(tmp_path, monkeypatch):
    _backends(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "lib.py").write_text("def helper(x):\n    return x + 1\n")
    (repo / "new.py").write_text("def helper2(x):\n    return helper(x)\n")
    _git(repo, "add", "-A")
    head = _commit(repo, "head")
    out = IX.ensure_base_index(repo, "o/r", head, tmp_path / "state",
                               allow_embeddings=False)
    db = IX.base_paths(tmp_path / "state", "o/r", head)["db"]
    called = []

    def assess(**kw):
        called.append("assess")
        return _fake_assess({})(**kw)

    def edit(**kw):
        called.append("edit")
        return _fake_edit("KEEP")(**kw)

    res = ES.run_editorial(
        cfg=_cfg(tmp_path), repo_path=repo, merged_db=db,
        base_rev=head, head_rev=head, records=[_finding()],
        run_dir=tmp_path, head_tree=tmp_path, base_tree=tmp_path,
        assess_fn=assess, edit_fn=edit)
    assert called == []
    assert res["suppressed"][0]["reason"] == "deterministic_drop"


# -- worker wiring -----------------------------------------------------------

canned = {"assessment": {"action": "reuse_existing",
                         "relationship": "duplicate",
                         "member_notes": [],
                         "proposal": {},
                         "observed_differences": [],
                         "remain_separate": [],
                         "validation": {}},
          "verdict": {"recommended_family": ["lib.py::helper"],
                      "action": "reuse_existing"},
          "stage_d": {"fatal_claims": []},
          "finalists": ["lib.py::helper"],
          "finalist_truncated": [],
          "retrieval_coverage": "full",
          "wall_s": 0.1}


def _wire(monkeypatch, editorial_ret=None, editorial_raise=False):
    from codatiel_i2.semdup_vendored.semdup import staged as STmod
    monkeypatch.setattr(STmod, "investigate",
                        lambda *a, **k: dict(canned))
    calls = []
    if editorial_raise:
        def fake_run(**kw):
            calls.append(kw)
            raise RuntimeError("editorial boom")
    else:
        def fake_run(**kw):
            calls.append(kw)
            return dict(editorial_ret or {"enabled": True, "ok": True,
                                         "published": [], "suppressed": [],
                                         "spend_usd": 0.0, "wall_s": 0.0})
    monkeypatch.setattr(W.ES, "run_editorial", fake_run)
    return calls


def _delta(tmp_path, head_extra=None, base_extra=None):
    base_files = {"lib.py": LIB}
    head_files = {"lib.py": LIB, "new.py": NEW}
    if base_extra:
        base_files.update(base_extra)
    if head_extra:
        head_files.update(head_extra)
    return _repo(tmp_path, base_files, head_files)


def _process(tmp_path, repo, base, head, pr=7, delivery="d1"):
    cfg = _cfg(tmp_path)
    built = IX.ensure_base_index(repo, "o/r", base, cfg.state_dir,
                                 allow_embeddings=False)
    assert built["present"], built
    store = _store(cfg.state_dir)
    try:
        out = W.process_delta(cfg=cfg, store=store, repo_path=repo,
                              repo="o/r", pr=pr, base_rev=base,
                              head_rev=head, delivery_id=delivery)
        return out, store
    finally:
        store.close()


def test_parallel_shadow_runs_and_persists_separately(tmp_path, monkeypatch):
    from codatiel_i2.semdup_vendored.semdup import staged as STmod  # noqa
    calls = _wire(monkeypatch, editorial_ret={
        "enabled": True, "ok": True,
        "published": [{"target": {"path": "new.py", "symbol": "helper2"},
                       "counterpart": {"path": "lib.py",
                                       "symbol": "helper"},
                       "head_rev": "h", "decision": "KEEP",
                       "revised_comment": "use helper()",
                       "reason": "concrete", "anchors": {},
                       "inference": {}}],
        "suppressed": [], "spend_usd": 0.0, "wall_s": 1.0})
    repo, base, head = _delta(tmp_path)
    out, _ = _process(tmp_path, repo, base, head)
    assert out["status"] == "complete"
    assert len(calls) == 1
    run_dir = Path(out["run_dir"])
    ed = json.loads((run_dir / "editorial.json").read_text())
    assert ed["ok"] is True and len(ed["published"]) == 1
    run = json.loads((run_dir / "run.json").read_text())
    assert run["editorial"]["published"] == 1
    assert run["editorial"]["spend_usd"] == 0.0
    # Engine path untouched: findings + advisories persist separately.
    findings = json.loads((run_dir / "findings.json").read_text())
    assert findings["records"] and \
        findings["records"][0]["status"] == "finding"
    assert (run_dir / "advisories.json").exists()


def test_duplicate_delivery_causes_no_repeat_work(tmp_path, monkeypatch):
    calls = _wire(monkeypatch)
    repo, base, head = _delta(tmp_path)
    out1, _ = _process(tmp_path, repo, base, head, delivery="dup-1")
    assert out1["status"] == "complete"
    out2, _ = _process(tmp_path, repo, base, head, delivery="dup-1")
    assert out2["status"] == "skipped"
    assert len(calls) == 1


def test_same_fingerprint_skips_and_force_push_is_incremental(
        tmp_path, monkeypatch):
    calls = _wire(monkeypatch)
    repo, base, head = _delta(tmp_path)
    out1, _ = _process(tmp_path, repo, base, head, delivery="e1")
    assert out1["status"] == "complete"
    out2, _ = _process(tmp_path, repo, base, head, delivery="e2")
    assert out2["status"] == "skipped"
    assert len(calls) == 1
    # Force-push style: same base, new head with one more file.
    (repo / "new2.py").write_text(NEW2)
    _git(repo, "add", "-A")
    head2 = _commit(repo, "head2")
    out3, _ = _process(tmp_path, repo, base, head2, delivery="e3")
    assert out3["status"] == "complete"
    assert len(calls) == 2
    run_dir = Path(out3["run_dir"])
    findings = json.loads((run_dir / "findings.json").read_text())
    targets = {(r.get("target") or {}).get("symbol")
               for r in findings["records"]}
    assert "helper3" in targets and "helper2" not in targets


def test_editorial_failure_keeps_run_complete(tmp_path, monkeypatch):
    _wire(monkeypatch, editorial_raise=True)
    repo, base, head = _delta(tmp_path)
    out, _ = _process(tmp_path, repo, base, head)
    assert out["status"] == "complete"
    ed = json.loads((Path(out["run_dir"]) / "editorial.json").read_text())
    assert ed["ok"] is False and "boom" in ed["error"]


def test_grandfather_baseline_runs_no_editorial(tmp_path, monkeypatch):
    calls = _wire(monkeypatch)
    repo, base, head = _delta(tmp_path)
    cfg = _cfg(tmp_path)
    built = IX.ensure_base_index(repo, "o/r", base, cfg.state_dir,
                                 allow_embeddings=False)
    assert built["present"], built
    store = _store(cfg.state_dir)
    try:
        out = W.process_delta(cfg=cfg, store=store, repo_path=repo,
                              repo="o/r", pr=11, base_rev=base,
                              head_rev=head, delivery_id="g1",
                              grandfather=True)
    finally:
        store.close()
    assert out["status"] == "grandfathered"
    assert calls == []


def test_flag_off_disables_editorial(tmp_path, monkeypatch):
    calls = _wire(monkeypatch)
    repo, base, head = _delta(tmp_path)
    cfg = _cfg(tmp_path, editorial_shadow=False)
    built = IX.ensure_base_index(repo, "o/r", base, cfg.state_dir,
                                 allow_embeddings=False)
    assert built["present"], built
    store = _store(cfg.state_dir)
    try:
        out = W.process_delta(cfg=cfg, store=store, repo_path=repo,
                              repo="o/r", pr=9, base_rev=base,
                              head_rev=head, delivery_id="f1")
    finally:
        store.close()
    assert out["status"] == "complete"
    assert calls == []
    ed = json.loads((Path(out["run_dir"]) / "editorial.json").read_text())
    assert ed == {"enabled": False}
