"""Shared deterministic-test scaffolding for semdup suites.

Git repos, fake backends/models and indexed-DB helpers shared by the
parallel editorial/mirror/scaffold suites so the suites do not
duplicate one another (keeps the copy-paste gate quiet). No
production code; no inference.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def commit(repo: Path, msg: str) -> str:
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", msg)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def empty_commit(repo: Path, msg: str) -> str:
    """Empty HEAD commit (keeps line spans stable across revisions)."""
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def backends(monkeypatch: pytest.MonkeyPatch) -> None:
    from codatiel_i2 import providers as P

    def fake_resolve(name):
        return {"backend": name, "model": f"test-{name}",
                "provider": "test",
                "rate_usd_per_mtok": {"input": 1.0, "output": 1.0},
                "rate_status": "test"}

    monkeypatch.setattr(P, "resolve_backend", fake_resolve)
    monkeypatch.setattr(P, "build_model",
                        lambda name: (f"test-{name}", None))


def fake_assess(terra: dict):
    def fake(**kwargs):
        return {"ok": True, "terra": dict(terra),
                "transcript": {"tool_calls": []}}
    return fake


def fake_edit(decision: str, ok: bool = True):
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


def publish_assess(**kwargs):
    """Terra fake that always finds a reusable overlap."""
    return fake_assess(
        {"overlap": True, "relationship": "equivalent",
         "opportunity": "direct_reuse",
         "shared_component": "add-one", "differences": []})(**kwargs)


def reject_assess(**kwargs):
    """Terra fake that finds no overlap (pair is examined, then dropped)."""
    return fake_assess(
        {"overlap": False, "relationship": "unrelated",
         "opportunity": "none",
         "shared_component": "", "differences": []})(**kwargs)


def publish_edit(**kwargs):
    return fake_edit("KEEP")(**kwargs)


def pair(t_path: str, t_sym: str, c_path: str, c_sym: str) -> dict:
    return {"target": {"path": t_path, "symbol": t_sym},
            "counterpart": {"path": c_path, "symbol": c_sym},
            "classification": "introduced"}


def recording_calls():
    """Shared (calls, assess, edit) triple; both append call names."""
    calls: list = []

    def assess(**kwargs):
        calls.append("assess")
        return publish_assess(**kwargs)

    def edit(**kwargs):
        calls.append("edit")
        return publish_edit(**kwargs)

    return calls, assess, edit


def run_judged(tmp_path: Path, monkeypatch, base_files: dict,
               head_files: dict, pairs: list, assess, edit, **kw):
    """Index a two-file repo and run one review_candidate_pairs call."""
    from codatiel_i2.semdup_dev import editorial_shadow as ES
    backends(monkeypatch)
    repo, base, head, db = indexed_two_file_repo(
        tmp_path, base_files, head_files)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return ES.review_candidate_pairs(
        pairs, repo="o/r", repo_path=repo, merged_db=db,
        base_rev=base, head_rev=head, head_tree=tmp_path,
        base_tree=tmp_path, run_dir=run_dir, ceiling_usd=2.0,
        assess_fn=assess, edit_fn=edit, **kw)


def two_file_repo(tmp_path: Path, base_files: dict, head_files: dict):
    """Git repo whose HEAD updates ``head_files`` over ``base_files``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for rel, text in base_files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    base = commit(repo, "base")
    for rel, text in head_files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    head = commit(repo, "head")
    return repo, base, head


def indexed_two_file_repo(tmp_path: Path, base_files: dict,
                           head_files: dict):
    from codatiel_i2.semdup_dev import index_state as IX
    repo, base, head = two_file_repo(tmp_path, base_files, head_files)
    state = tmp_path / "state"
    out = IX.ensure_base_index(repo, "o/r", head, state,
                               allow_embeddings=False)
    assert out["present"]
    return repo, base, head, IX.base_paths(state, "o/r", head)["db"]
