"""PROD-01 reconciliation seam: operator pause gates GitHub publishing.

Effective publish rule: env switch on AND no operator pause.
Absent operator file = no constraint. Present file (pause unset or
true) = HOLD. Corrupt file = HOLD with the loud error.

Self-contained (no cross-test-module imports).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codatiel_i2 import event_store as S
from codatiel_i2.github import comments as C

A40 = "a" * 40
H40 = "c" * 40
REPO = "o/r"
PR = 7


class FakeGH:
    def __init__(self, *, head=H40):
        self.head = head
        self.posts: list[dict] = []

    def __call__(self, path, *args):
        if path == "POST":
            _, repo, pr, body = (path,) + tuple(args)
            comment = {"id": 1000 + len(self.posts), "body": body}
            self.posts.append({"repo": repo, "pr": pr, "body": body})
            return comment
        if path.endswith(f"/pulls/{PR}"):
            return {"head": {"sha": self.head},
                    "base": {"sha": A40},
                    "state": "open", "merged": False, "draft": False}
        if "files" in path:
            return [{"filename": "src/w.py"}]
        if "comments" in path:
            return []
        raise AssertionError(f"unexpected gh path {path!r}")


def _adv():
    return {
        "finding_id": "agent-000", "detector": "contracts",
        "disposition": "advisory_grounded",
        "substantive_assessment": "UNASSESSED",
        "publish_eligible": False,
        "bad_outcome": "The widget leaks the token on retry.",
        "evidence_ids": ["ev1"],
        "changed_anchor": {"path": "src/w.py", "line_start": 10,
                           "line_end": 14},
        "raw": {"title": "Token leak on retry", "claim": "same",
                "path": "src/w.py", "line_start": 10, "line_end": 14,
                "detector": "contracts", "revision": "head",
                "evidence_ids": ["ev1"]},
    }


def _make_run(tmp_path: Path):
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)
    (d / "run.json").write_text(json.dumps({
        "run_id": "r1", "repo": REPO,
        "base_sha": A40, "head_sha": H40,
        "config": {"model": "terra:t"}}))
    (d / "shadow_result.json").write_text(json.dumps({
        "status": "complete",
        "counts": {"advisory_grounded": 1},
        "advisory": [_adv()]}))
    (d / "metrics.json").write_text(json.dumps({
        "status": "complete", "model": "terra:t",
        "spend": {"usd_estimate": 0.01}}))
    (d / "semdup.json").write_text(json.dumps(
        {"status": "ok", "coverage": "full", "spend_usd": 0.01,
         "reason": "examined 1 targets, 1 pairs; 0 approved",
         "targets": [], "pairs_examined": 1, "skipped": [],
         "published": [], "suppressed": []}))
    from codatiel_i2 import review_identity as _RI
    _RI.bind_run(d, repo_slug=REPO, pr_number=PR,
                 base_sha=A40, head_sha=H40, run_id="r1",
                 binder="test")
    return d


def _armed_review(store, run_dir: Path, *, now=1000.0):
    C.arm(store, now=now)
    return store.review_record(
        repo=REPO, pr=PR, kind="full", base_sha=A40, head_sha=H40,
        diff_sha="d" * 40, files=1, pipeline="terra_only",
        status="complete", terra_findings=1, luna_added=0,
        cost_usd=0.01, cost_unknown=False, run_dir=str(run_dir),
        now=now + 10.0)


@pytest.fixture
def store(tmp_path: Path):
    st = S.Store(tmp_path / "events.sqlite")
    yield st
    st.close()


def _isolate_state(tmp_path, monkeypatch):
    sd = tmp_path / "state"
    sd.mkdir(exist_ok=True)
    monkeypatch.setenv("CODATIEL_DEV_STATE", str(sd))
    monkeypatch.delenv("CODATIEL_TEST_STATE", raising=False)
    return sd


def test_absent_operator_file_is_no_constraint(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    _isolate_state(tmp_path, monkeypatch)
    assert C.operator_pause_reason() is None


def test_present_pause_holds_with_zero_posts(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    sd = _isolate_state(tmp_path, monkeypatch)
    (sd / "operator.json").write_text(
        json.dumps({"pause_github_comments": True}))
    d = _make_run(tmp_path)
    gh = FakeGH()
    out = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert out["outcome"] == "refused"
    assert "pause" in out["reason"].lower()
    assert gh.posts == []
    assert store.publications_list() == []


def test_unset_pause_defaults_to_hold(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    sd = _isolate_state(tmp_path, monkeypatch)
    (sd / "operator.json").write_text(json.dumps({}))
    d = _make_run(tmp_path)
    out = C.post_review_comment(run_dir=d, store=store, gh=FakeGH())
    assert out["outcome"] == "refused"
    assert "pause" in out["reason"].lower()


def test_explicit_unpause_allows_publish(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    sd = _isolate_state(tmp_path, monkeypatch)
    (sd / "operator.json").write_text(
        json.dumps({"pause_github_comments": False}))
    d = _make_run(tmp_path)
    _armed_review(store, d)
    gh = FakeGH()
    out = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert out["outcome"] == "posted", out
    assert len(gh.posts) == 1


def test_corrupt_operator_file_holds(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    sd = _isolate_state(tmp_path, monkeypatch)
    (sd / "operator.json").write_text("{not json")
    d = _make_run(tmp_path)
    gh = FakeGH()
    out = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert out["outcome"] == "refused"
    assert "unreadable" in out["reason"] or "invalid" in out["reason"]
    assert gh.posts == []
