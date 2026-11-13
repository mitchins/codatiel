"""PROD-01 Track A, task A2: GitHub publisher.

No network, no real gh: all GitHub access goes through an injectable
fake. Covers the HOLD contract:

- global disable switch (default off refuses with zero posts);
- immutable review identity binding (misaddressed/tampered runs
  refuse);
- stale-head refusal (live head moved after review);
- closed/merged lifecycle refusal (no replay after close/merge);
- duplicate-delivery idempotency (same run posts once; retries
  reconcile);
- crash recovery (post persisted nowhere, comment exists ->
  reconcile finds it, no second post);
- historical-backlog refusal (only reviews ingested after arming
  publish);
- incomplete/budget-refused reviews never publish;
- comment format: commit-pinned, separate correctness/reuse sections,
  source links, honest confidence labels, no approval/review action.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codatiel_i2 import event_store as S
from codatiel_i2.github import comments as C

A40 = "a" * 40
B40 = "b" * 40
H40 = "c" * 40

REPO = "o/r"
PR = 7


class FakeGH:
    """Injectable gh transport. Records posts; serves scripted reads."""

    def __init__(self, *, head=H40, base=A40, state="open", merged=False,
                 files=("src/w.py",), comments=(), fail_reads=False):
        self.head = head
        self.base = base
        self.state = state
        self.merged = merged
        self.files = list(files)
        self.comments = list(comments)
        self.fail_reads = fail_reads
        self.posts: list[dict] = []

    def __call__(self, path, *args):
        if path == "POST":
            _, repo, pr, body = (path,) + tuple(args)
            comment = {"id": 1000 + len(self.posts), "body": body}
            self.posts.append({"repo": repo, "pr": pr, "body": body})
            self.comments.append(comment)
            return comment
        if self.fail_reads:
            raise RuntimeError("gh api failed: boom")
        if path.endswith(f"/pulls/{PR}"):
            return {"head": {"sha": self.head},
                    "base": {"sha": self.base},
                    "state": self.state, "merged": self.merged,
                    "draft": False}
        if "files" in path:
            return [{"filename": f} for f in self.files]
        if "comments" in path:
            return list(self.comments)
        raise AssertionError(f"unexpected gh path {path!r}")


def _adv(n="agent-000", path="src/w.py"):
    return {
        "finding_id": n, "detector": "contracts",
        "disposition": "advisory_grounded",
        "substantive_assessment": "UNASSESSED",
        "publish_eligible": False,
        "bad_outcome": "The widget leaks the token on retry.",
        "evidence_ids": ["ev1"],
        "changed_anchor": {"path": path, "line_start": 10,
                           "line_end": 14},
        "raw": {"title": "Token leak on retry", "claim": "same",
                "path": path, "line_start": 10, "line_end": 14,
                "detector": "contracts", "revision": "head",
                "evidence_ids": ["ev1"]},
    }


def _make_run(tmp_path: Path, name="run", *, status="complete",
              advisory=None, head=H40, base=A40, repo_slug=REPO,
              pr=PR, run_id="r1"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "run.json").write_text(json.dumps({
        "run_id": run_id, "repo": repo_slug,
        "base_sha": base, "head_sha": head,
        "config": {"model": "terra:t"}}))
    (d / "shadow_result.json").write_text(json.dumps({
        "status": status,
        "counts": {"advisory_grounded": len(advisory or [])},
        "advisory": advisory if advisory is not None else [_adv()]}))
    (d / "metrics.json").write_text(json.dumps({
        "status": status, "model": "terra:t",
        "spend": {"usd_estimate": 0.01}}))
    (d / "semdup.json").write_text(json.dumps(
        {"status": "ok", "coverage": "full", "spend_usd": 0.01,
         "reason": "examined 1 targets, 1 pairs; 0 approved",
         "targets": [], "pairs_examined": 1, "skipped": [],
         "published": [], "suppressed": []}))
    from codatiel_i2 import review_identity as _RI
    _RI.bind_run(d, repo_slug=repo_slug, pr_number=pr,
                 base_sha=base, head_sha=head, run_id=run_id,
                 binder="test")
    return d


@pytest.fixture
def store(tmp_path: Path):
    st = S.Store(tmp_path / "events.sqlite")
    yield st
    st.close()


def _armed_review(store, run_dir: Path, *, now=1000.0):
    C.arm(store, now=now)
    return store.review_record(
        repo=REPO, pr=PR, kind="full", base_sha=A40, head_sha=H40,
        diff_sha="d" * 40, files=1, pipeline="terra_only",
        status="complete", terra_findings=1, luna_added=0,
        cost_usd=0.01, cost_unknown=False, run_dir=str(run_dir),
        now=now + 10.0)


def test_switch_off_by_default_refuses_with_zero_posts(
        tmp_path, store, monkeypatch):
    monkeypatch.delenv("CODATIEL_GITHUB_COMMENTS", raising=False)
    assert not C.is_enabled()
    d = _make_run(tmp_path)
    out = C.post_review_comment(run_dir=d, store=store,
                                gh=FakeGH())
    assert out["outcome"] == "refused"
    assert "HOLD" in out["reason"] or "off" in out["reason"]
    assert store.publications_list() == []


def test_explicit_off_refuses(tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "off")
    d = _make_run(tmp_path)
    out = C.post_review_comment(run_dir=d, store=store, gh=FakeGH())
    assert out["outcome"] == "refused"


def test_unarmed_refuses_even_when_switched_on(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    out = C.post_review_comment(run_dir=d, store=store, gh=FakeGH())
    assert out["outcome"] == "refused"
    assert "arming" in out["reason"]


def test_identity_binding_refuses_misaddressed_run(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    C.arm(store, now=1000.0)
    # Tamper the bound head: stored artifacts disagree with identity.
    run = json.loads((d / "run.json").read_text())
    run["head_sha"] = "d" * 40
    (d / "run.json").write_text(json.dumps(run))
    out = C.post_review_comment(run_dir=d, store=store, gh=FakeGH())
    assert out["outcome"] == "refused"
    assert "identity" in out["reason"]


def test_historical_backlog_never_publishes(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    # Review ingested BEFORE arming: finished_ts < watermark.
    C.arm(store, now=2000.0)
    store.review_record(
        repo=REPO, pr=PR, kind="full", base_sha=A40, head_sha=H40,
        diff_sha="d" * 40, files=1, pipeline="terra_only",
        status="complete", terra_findings=1, luna_added=0,
        cost_usd=0.01, cost_unknown=False, run_dir=str(d),
        now=1000.0)
    gh = FakeGH()
    out = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert out["outcome"] == "refused"
    assert "backlog" in out["reason"]
    assert gh.posts == []


def test_happy_path_posts_one_pinned_comment(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    _armed_review(store, d)
    gh = FakeGH()
    out = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert out["outcome"] == "posted", out
    assert len(gh.posts) == 1
    body = gh.posts[0]["body"]
    # Marker binds the review identity.
    assert f"run=r1 head={H40}" in body
    # Comment identifies the reviewed commit.
    assert H40[:12] in body
    # Correctness section + source link + honest confidence label.
    assert "Token leak on retry" in body
    assert f"https://github.com/{REPO}/blob/{H40}/src/w.py" in body
    assert "Needs human review" in body
    # Advisory format: no review action.
    assert "approves nothing" in body
    assert "requests no changes" in body
    lowered = body.lower()
    assert "approv" in lowered and "request-changes" not in lowered
    row = store.publication_get(REPO, PR, H40, "r1")
    assert row is not None and row["outcome"] == "posted"
    assert row["comment_id"] == str(out["comment_id"])


def test_duplicate_delivery_idempotent(tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    _armed_review(store, d)
    gh = FakeGH()
    first = C.post_review_comment(run_dir=d, store=store, gh=gh)
    second = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert first["outcome"] == "posted"
    assert second["outcome"] == "already-posted"
    assert len(gh.posts) == 1
    assert store.publications_list(REPO, PR) and len(
        store.publications_list(REPO, PR)) == 1


def test_crash_between_post_and_persist_reconciles(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    _armed_review(store, d)
    from codatiel_i2 import review_identity as _RI
    ident = _RI.load_identity(d)
    from codatiel_i2 import digest_model as _DM
    body = C.build_comment_body(_DM.build_review_model(d), ident)
    # The post happened but the process died before persisting.
    gh = FakeGH(comments=[{"id": 4242, "body": body}])
    assert store.publication_get(REPO, PR, H40, "r1") is None
    out = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert out["outcome"] == "posted", out
    assert gh.posts == []  # no second post
    row = store.publication_get(REPO, PR, H40, "r1")
    assert row["comment_id"] == "4242"
    assert "reconciled" in row["reason"]


def test_stale_head_refused(tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    _armed_review(store, d)
    gh = FakeGH(head="e" * 40)  # PR moved after the review
    out = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert out["outcome"] == "refused"
    assert "moved" in out["reason"]
    assert gh.posts == []
    row = store.publication_get(REPO, PR, H40, "r1")
    assert row is not None and row["outcome"] == "refused"


def test_closed_or_merged_never_replays(tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    _armed_review(store, d)
    for kw in ({"state": "closed"}, {"merged": True}):
        gh = FakeGH(**kw)
        out = C.post_review_comment(run_dir=d, store=store, gh=gh)
        assert out["outcome"] == "refused", kw
        assert "lifecycle" in out["reason"] or "close" in out["reason"], kw
        assert gh.posts == [], kw


def test_incomplete_or_budget_refused_never_publishes(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    for status in ("incomplete_budget_exhausted", "unresolved_empty_final",
                   "failed", "inconclusive"):
        d = _make_run(tmp_path, name=f"run-{status}", status=status,
                       run_id=f"r-{status}")
        C.arm(store, now=1000.0) if C.armed_at(store) is None else None
        store.review_record(
            repo=REPO, pr=PR, kind="full", base_sha=A40, head_sha=H40,
            diff_sha="d" * 40, files=1, pipeline="terra_only",
            status=status, terra_findings=0, luna_added=0,
            cost_usd=None, cost_unknown=True, run_dir=str(d),
            now=1010.0)
        gh = FakeGH()
        out = C.post_review_comment(run_dir=d, store=store, gh=gh)
        assert out["outcome"] == "refused", status
        assert "not complete" in out["reason"], status
        assert gh.posts == [], status


def test_empty_review_has_nothing_to_publish(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path, advisory=[])
    (d / "semdup.json").write_text(json.dumps(
        {"status": "ok", "coverage": "full", "spend_usd": 0.01,
         "reason": "examined 0 targets; nothing to check",
         "targets": [], "pairs_examined": 0, "skipped": [],
         "published": [], "suppressed": []}))
    _armed_review(store, d)
    out = C.post_review_comment(run_dir=d, store=store, gh=FakeGH())
    assert out["outcome"] == "refused"
    assert "no material content" in out["reason"]


def test_superseded_pr_refused(tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    _armed_review(store, d)
    gh = FakeGH()
    out = C.post_review_comment(run_dir=d, store=store, gh=gh,
                                superseded_by=38)
    assert out["outcome"] == "refused"
    assert "supersed" in out["reason"]
    assert gh.posts == []


def test_unreachable_live_state_refuses_not_posts(
        tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    d = _make_run(tmp_path)
    _armed_review(store, d)
    gh = FakeGH(fail_reads=True)
    out = C.post_review_comment(run_dir=d, store=store, gh=gh)
    assert out["outcome"] == "refused"
    assert gh.posts == []


def test_arm_is_immutable(tmp_path):
    st = S.Store(tmp_path / "arm.sqlite")
    try:
        C.arm(st, now=1000.0)
        with pytest.raises(S.StoreError, match="immutable"):
            C.arm(st, now=2000.0)
    finally:
        st.close()
