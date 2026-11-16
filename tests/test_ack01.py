"""CODATIEL-ACK-01: early GitHub acknowledgement via one idempotent comment.

No network, no real gh: all GitHub access goes through an injectable
fake (mirroring test_prod01_publisher.py). Covers:

- HOLD contract: switch off / unarmed / operator pause refuse with
  zero network traffic;
- lifecycle rendering: queued, deferred, in_progress, completed,
  superseded, failed;
- "reviewed" appears only on completion, never before;
- idempotence: repeats reconcile to already-current, no edit spam;
- head changes PATCH the same comment and demote the old head to a
  bounded history (supersession visible, no comment spam);
- crash-between-write-and-anything reconciles (state lives in the
  comment body); concurrent same-head syncs converge;
- merged/closed PRs never get a comment; unreachable comment list
  never licenses a write;
- pipeline wiring: draft/ceiling deferrals, claim-time in_progress
  before inference, completion with findings link, stale supersede,
  failure — all best-effort, none affecting review/spend/email.
"""

from __future__ import annotations

import pytest

from codatiel_i2 import event_pipeline as P
from codatiel_i2 import event_store as S
from codatiel_i2.github import ack as A
from codatiel_i2.github import comments as C

A40 = "a" * 40
B40 = "b" * 40
C40 = "c" * 40
D40 = "d" * 40

REPO = "o/r"
PR = 7


class FakeGH:
    """Dual-purpose gh fake with PATCH support. Records writes."""

    def __init__(self, *, head=C40, base=A40, state="open",
                 merged=False, draft=False, comments=(),
                 fail_reads=False):
        self.head = head
        self.base = base
        self.state = state
        self.merged = merged
        self.draft = draft
        self.comments = list(comments)
        self.fail_reads = fail_reads
        self.posts: list[dict] = []
        self.patches: list[dict] = []

    def __call__(self, path, *args):
        if path == "POST":
            _, repo, pr, body = (path,) + tuple(args)
            comment = {"id": 1000 + len(self.posts), "body": body}
            self.posts.append({"repo": repo, "pr": pr, "body": body})
            self.comments.append(comment)
            return comment
        if path == "PATCH":
            _, repo, cid, body = (path,) + tuple(args)
            for c in self.comments:
                if str(c.get("id")) == str(cid):
                    c["body"] = body
                    updated = dict(c)
                    break
            else:
                updated = {"id": cid, "body": body}
                self.comments.append(updated)
            self.patches.append({"repo": repo, "id": cid, "body": body})
            return updated
        if self.fail_reads:
            raise RuntimeError("gh api failed: boom")
        if path.endswith(f"/pulls/{PR}"):
            return {"head": {"sha": self.head},
                    "base": {"sha": self.base},
                    "state": self.state, "merged": self.merged,
                    "draft": self.draft}
        if "comments" in path:
            return list(self.comments)
        raise AssertionError(f"unexpected gh path {path!r}")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(C.SWITCH_ENV, "on")
    st = S.Store(tmp_path / "events.sqlite")
    st.meta_set(C.ARMED_META_KEY, "1000.0")
    yield st
    st.close()


def _sync(store, gh, head, state, **kw):
    return A.sync_ack(repo=REPO, pr=PR, head=head, state=state,
                      store=store, gh=gh, **kw)


# -- HOLD contract --------------------------------------------------------

def test_switch_off_refuses_with_zero_traffic(tmp_path, monkeypatch):
    monkeypatch.delenv(C.SWITCH_ENV, raising=False)
    st = S.Store(tmp_path / "e.sqlite")
    gh = FakeGH()
    out = _sync(st, gh, C40, "queued")
    assert out["outcome"] == "refused"
    assert gh.posts == [] and gh.patches == []
    st.close()


def test_unarmed_refuses_with_zero_traffic(tmp_path, monkeypatch):
    monkeypatch.setenv(C.SWITCH_ENV, "on")
    st = S.Store(tmp_path / "e.sqlite")
    gh = FakeGH()
    out = _sync(st, gh, C40, "queued")
    assert out["outcome"] == "refused"
    assert gh.posts == [] and gh.patches == []
    st.close()


def test_operator_pause_refuses(store, monkeypatch):
    monkeypatch.setattr(C, "operator_pause_reason",
                        lambda: "operator pause is set")
    gh = FakeGH()
    out = _sync(store, gh, C40, "queued")
    assert out["outcome"] == "refused"
    assert "pause" in out["reason"]
    assert gh.posts == [] and gh.patches == []


def test_bad_head_and_state_refused(store):
    gh = FakeGH()
    assert _sync(store, gh, "notasha", "queued")["outcome"] == "refused"
    assert _sync(store, gh, C40, "bogus")["outcome"] == "refused"
    assert gh.posts == [] and gh.patches == []


# -- lifecycle ------------------------------------------------------------

def test_queued_posts_then_reconciles_current(store):
    gh = FakeGH()
    first = _sync(store, gh, C40, "queued", detail="waiting.")
    assert first["outcome"] == "posted"
    assert len(gh.posts) == 1
    second = _sync(store, gh, C40, "queued", detail="waiting.")
    assert second["outcome"] == "already-current"
    assert len(gh.posts) == 1 and gh.patches == []


def test_reviewed_never_shown_before_completion(store):
    gh = FakeGH()
    for state in ("queued", "deferred", "in_progress"):
        out = _sync(store, gh, C40, state, detail="x")
        assert out["outcome"] in ("posted", "updated")
    body = gh.comments[0]["body"]
    assert "reviewed" not in body
    done = _sync(store, gh, C40, "completed")
    assert "reviewed" in gh.comments[0]["body"]
    assert done["outcome"] == "updated"


def test_head_change_patches_and_keeps_history(store):
    gh = FakeGH()
    assert _sync(store, gh, C40, "queued")["outcome"] == "posted"
    out = _sync(store, gh, D40, "in_progress", detail="run.")
    assert out["outcome"] == "updated"
    assert len(gh.posts) == 1 and len(gh.patches) == 1
    body = gh.comments[0]["body"]
    assert D40[:12] in body and C40[:12] in body
    assert "Previous heads" in body


def test_completed_links_findings_comment(store):
    gh = FakeGH()
    _sync(store, gh, C40, "in_progress")
    out = _sync(store, gh, C40, "completed",
                findings_comment_id="6904971847")
    assert out["outcome"] == "updated"
    assert "#issuecomment-6904971847" in gh.comments[0]["body"]


def test_superseded_names_live_head(store):
    gh = FakeGH()
    _sync(store, gh, C40, "in_progress")
    out = _sync(store, gh, C40, "superseded", live_head=D40,
                detail="partial cost recorded.")
    assert out["outcome"] == "updated"
    body = gh.comments[0]["body"]
    assert D40[:12] in body and "partial cost recorded" in body


def test_history_bounded(store):
    gh = FakeGH()
    heads = [(f"{i:02x}" + "0" * 38) for i in range(8)]
    _sync(store, gh, heads[0], "queued")
    for h in heads[1:]:
        _sync(store, gh, h, "superseded", live_head=heads[0])
    body = gh.comments[0]["body"]
    assert "Previous heads" in body
    assert body.count("- `") <= A.MAX_HISTORY
    assert heads[-1][:12] in body  # current head present


def test_merged_pr_never_gets_comment(store):
    gh = FakeGH(state="closed", merged=True)
    out = _sync(store, gh, C40, "queued")
    assert out["outcome"] == "refused"
    assert "merged" in out["reason"]
    assert gh.posts == [] and gh.patches == []


def test_unreachable_list_never_licenses_write(store):
    gh = FakeGH(fail_reads=True)
    out = _sync(store, gh, C40, "queued")
    assert out["outcome"] == "error"
    assert gh.posts == [] and gh.patches == []


def test_crash_gap_reconciles_without_duplicate(store):
    body = A.render_ack(repo=REPO, pr=PR, head=C40, state="queued")
    gh = FakeGH(comments=[{"id": 4242, "body": body}])
    out = _sync(store, gh, C40, "queued")
    assert out["outcome"] == "already-current"
    assert gh.posts == [] and gh.patches == []


def test_concurrent_same_head_converges(store):
    gh = FakeGH()
    first = _sync(store, gh, C40, "in_progress", detail="run.")
    second = _sync(store, gh, C40, "in_progress", detail="run.")
    assert first["outcome"] == "posted"
    assert second["outcome"] == "already-current"
    assert len(gh.posts) == 1 and gh.patches == []


# -- pipeline wiring ------------------------------------------------------

def _env(monkeypatch, tmp_path, root, cap="20.0"):
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", cap)
    monkeypatch.setenv("CODATIEL_TEST_TO", "test-ops@example.com")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "codatiel-test@example.com")
    monkeypatch.setenv("CODATIEL_TEST_REPO_PATHS", f"{REPO}={root}")
    monkeypatch.setenv("REVIEW_PIPELINE", "terra_only")
    monkeypatch.setenv("CODATIEL_EVENTS_TRANSPORT", "fixture")
    monkeypatch.delenv("CODATIEL_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv(C.SWITCH_ENV, "on")


def _ev(head, base, draft=False, did="d1"):
    raw = {"action": "synchronize", "repository": {"full_name": REPO},
           "pull_request": {
               "number": 7, "state": "open", "draft": draft,
               "merged": False, "created_at": "9999-01-01T00:00:00Z",
               "base": {"sha": base}, "head": {"sha": head, "ref": "f"}}}
    ev, err = P.normalize_event(raw, did)
    assert err == "", err
    return ev


@pytest.fixture
def gitroot(tmp_path):
    import subprocess
    root = tmp_path / "repo"
    root.mkdir()
    for a in (["init", "-b", "main"],
              ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *a], check=True,
                       capture_output=True, timeout=60)
    (root / "app.py").write_text("LIMIT = 100\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "A"],
                   check=True, capture_output=True, timeout=60)
    a = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                       check=True, capture_output=True, text=True,
                       timeout=60).stdout.strip()
    (root / "app.py").write_text("LIMIT = 5\n")
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "B"],
                   check=True, capture_output=True, timeout=60)
    b = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                       check=True, capture_output=True, text=True,
                       timeout=60).stdout.strip()
    return root, a, b


def _cfg_store(tmp_path, monkeypatch, root, cap="20.0"):
    _env(monkeypatch, tmp_path, root, cap)
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
    return cfg, store


def test_pipeline_draft_requests_deferred_ack(tmp_path, monkeypatch,
                                              gitroot):
    root, a, b = gitroot
    cfg, store = _cfg_store(tmp_path, monkeypatch, root)
    calls = []
    monkeypatch.setattr(P, "request_ack",
                        lambda **kw: calls.append(kw) or {"outcome": "ok"})
    out = P.process_event(cfg=cfg, store=store, ev=_ev(b, a, draft=True))
    assert out["decision"] == "skipped:draft"
    assert calls and calls[0]["state"] == "deferred"
    assert calls[0]["head"] == b
    store.close()


def test_pipeline_ceiling_requests_deferred_ack(tmp_path, monkeypatch,
                                                gitroot):
    root, a, b = gitroot
    cfg, store = _cfg_store(tmp_path, monkeypatch, root, cap="0.0001")
    calls = []
    monkeypatch.setattr(P, "request_ack",
                        lambda **kw: calls.append(kw) or {"outcome": "ok"})
    out = P.process_event(cfg=cfg, store=store, ev=_ev(b, a))
    assert out["decision"] == "skipped:ceiling"
    assert calls and calls[0]["state"] == "deferred"
    store.close()


def test_pipeline_stale_requests_superseded_with_live(tmp_path,
                                                      monkeypatch, gitroot):
    root, a, b = gitroot
    cfg, store = _cfg_store(tmp_path, monkeypatch, root)
    monkeypatch.setattr(P, "get_live_head", lambda repo, pr: a)
    calls = []
    monkeypatch.setattr(P, "request_ack",
                        lambda **kw: calls.append(kw) or {"outcome": "ok"})
    out = P.process_event(cfg=cfg, store=store, ev=_ev(b, a))
    assert out["decision"] == "skipped:stale_head"
    assert calls and calls[0]["state"] == "superseded"
    assert calls[0]["live_head"] == a
    store.close()


def test_pipeline_claim_emits_in_progress_before_inference(
        tmp_path, monkeypatch, gitroot):
    import json
    from codatiel_i2 import runtime_sdk
    root, a, b = gitroot
    cfg, store = _cfg_store(tmp_path, monkeypatch, root)
    order: list[str] = []
    real_run = P.run_review_for_range

    def _spy(*args, **kwargs):
        order.append("inference")
        text = json.dumps({"status": "silence", "summary": "ok",
                           "findings": []})

        def _mk(role: str):
            script = [runtime_sdk.scripted_tool_call(
                call_id="t1", name="read_file",
                arguments={"path": "app.py", "revision": "head",
                           "start_line": 1, "end_line": 0},
                response_id="r1"),
                runtime_sdk.scripted_final(text, response_id="f1")]
            return runtime_sdk.ScriptedModel(script), "scripted:t"
        kwargs["model_factory"] = _mk
        return real_run(*args, **kwargs)

    monkeypatch.setattr(P, "run_review_for_range", _spy)
    gh = FakeGH(head=b, base=a)
    store.meta_set(C.ARMED_META_KEY, "1.0")
    out = P.process_event(cfg=cfg, store=store, ev=_ev(b, a, did="d-claim"),
                          ack_gh=gh)
    assert out["decision"] == "completed"
    assert order == ["inference"]
    assert gh.posts, "in_progress ack must precede paid inference"
    first = gh.posts[0]["body"]
    assert b[:12] in first and "reviewed" not in first
    store.close()


def test_pipeline_failed_requests_failed_ack(tmp_path, monkeypatch,
                                             gitroot):
    root, a, b = gitroot
    cfg, store = _cfg_store(tmp_path, monkeypatch, root)
    monkeypatch.setattr(
        P, "run_review_for_range",
        lambda **kw: {"status": "failed", "stage": "t", "error": "boom",
                      "cost_usd": 0.0, "run_dir": ""})
    calls = []
    monkeypatch.setattr(P, "request_ack",
                        lambda **kw: calls.append(kw) or {"outcome": "ok"})
    out = P.process_event(cfg=cfg, store=store, ev=_ev(b, a, did="d-fail"))
    assert out["decision"] == "failed"
    states = [c["state"] for c in calls]
    assert "in_progress" in states and "failed" in states
    store.close()


def test_pipeline_replay_is_silent(tmp_path, monkeypatch, gitroot):
    root, a, b = gitroot
    cfg, store = _cfg_store(tmp_path, monkeypatch, root)
    calls = []
    monkeypatch.setattr(P, "request_ack",
                        lambda **kw: calls.append(kw) or {"outcome": "ok"})
    first = P.process_event(cfg=cfg, store=store, ev=_ev(b, a, draft=True))
    assert first["decision"] == "skipped:draft"
    n = len(calls)
    assert n >= 1
    second = P.process_event(cfg=cfg, store=store, ev=_ev(b, a, draft=True))
    assert second["decision"] == "skipped:replay"
    assert len(calls) == n, "replays must not re-acknowledge"
    store.close()
