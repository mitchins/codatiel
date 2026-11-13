"""PROD-01 Track A, task A3: review lifecycle.

No paid inference: scripted models through the real SDK runtime, real
git repos, fixture transports. Pins the lifecycle matrix:

- draft -> ready with UNCHANGED head triggers a review, even when the
  poller re-ingests under the same (repo, PR, head) delivery ID;
- new commits -> fresh reviews; unchanged heads -> no duplicate
  inference (replay-blocked or unchanged-skipped);
- close/merge -> no review; reopen with identical head -> no review;
  reopen with a new head -> fresh review;
- poll overlap stays serialized (atomic claim, one winner);
- GitHub comment publishing never disturbs email (email attempted
  first, publish outcome only annotates the reason; HOLD by default).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

agents = pytest.importorskip("agents")

from codatiel_i2 import event_pipeline as P  # noqa: E402
from codatiel_i2 import event_store as S  # noqa: E402
from codatiel_i2 import runtime_sdk  # noqa: E402

REPO = "o/r"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def chain(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "evt@t")
    _git(root, "config", "user.name", "evt")
    (root / "app.py").write_text("LIMIT = 100\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "A")
    a = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 10\n")
    (root / "feat.py").write_text("X = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "B")
    b = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 5\n")
    _git(root, "commit", "-qam", "C")
    c = _git(root, "rev-parse", "HEAD")
    return {"root": root, "A": a, "B": b, "C": c}


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: Path,
         cap: str = "20") -> None:
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
    monkeypatch.delenv("CODATIEL_GITHUB_COMMENTS", raising=False)


@pytest.fixture
def cfg_store(chain: dict, tmp_path: Path,
              monkeypatch: pytest.MonkeyPatch):
    _env(monkeypatch, tmp_path, chain["root"])
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
    yield cfg, store
    store.close()


def _terra_factory():
    text = json.dumps({"status": "silence", "summary": "ok",
                       "findings": []})

    def _mk(role: str):
        script = [runtime_sdk.scripted_tool_call(
            call_id="t1", name="read_file",
            arguments={"path": "app.py", "revision": "head",
                       "start_line": 1, "end_line": 0},
            response_id="r1"),
            runtime_sdk.scripted_final(text, response_id="f1")]
        return runtime_sdk.ScriptedModel(script), "scripted:terra-test"
    return _mk


def pr_event(action: str, number: int, base: str, head: str,
             draft: bool = False, merged: bool = False,
             created: str = "9999-01-01T00:00:00Z",
             branch: str = "f") -> dict:
    state = "closed" if (action == "closed" or merged) else "open"
    return {"action": action, "repository": {"full_name": REPO},
            "pull_request": {
                "number": number, "state": state, "draft": draft,
                "merged": merged, "created_at": created,
                "base": {"sha": base}, "head": {"sha": head,
                                               "ref": branch}}}


def _ingest(cfg, store, raw: dict, did: str, **kw):
    ev, err = P.normalize_event(raw, did)
    assert err == "", err
    return P.process_event(cfg=cfg, store=store, ev=ev, **kw)


def _runs(cfg) -> list[Path]:
    return sorted((cfg.state_dir / "runs").rglob("run.json"))


def _poll_id(pr: int, head: str) -> str:
    return f"poll-o-r-{pr}-{head[:12]}"


def test_draft_ready_same_head_same_delivery_reviews(cfg_store, chain):
    """The poller keys delivery IDs by (repo, PR, head): the mark-ready
    tick repeats the deferred delivery ID. That repeat must re-root
    into a full review, not replay-block forever."""
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    did = _poll_id(23, chain["B"])
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["B"], draft=True), did,
        **kw)
    assert row["decision"] == "skipped:draft", row
    assert _runs(cfg) == []
    # Same head, same delivery ID, now marked ready (draft=false).
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["B"], draft=False), did,
        **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 1
    # A third identical tick is a genuine replay: no second review.
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["B"], draft=False), did,
        **kw)
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == 1


def test_draft_sync_same_head_stays_deferred(cfg_store, chain):
    """Draft ticks that stay drafts never review, same ID or not."""
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    did = _poll_id(23, chain["B"])
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["B"], draft=True), did,
        **kw)
    assert row["decision"] == "skipped:draft", row
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["B"], draft=True), did,
        **kw)
    assert row["decision"] in ("skipped:draft", "skipped:replay"), row
    assert _runs(cfg) == []


def test_new_commits_review_unchanged_heads_do_not(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    row = _ingest(cfg, store, pr_event(
        "opened", 23, chain["A"], chain["B"]), "d1", **kw)
    assert row["decision"] == "completed", row
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["C"]), "d2", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 2
    # Exact redelivery: replay-blocked.
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["C"]), "d2", **kw)
    assert row["decision"] == "skipped:replay", row
    # New delivery, same head: unchanged, no inference.
    row = _ingest(cfg, store, pr_event(
        "synchronize", 23, chain["A"], chain["C"]), "d3", **kw)
    assert row["decision"] == "skipped:unchanged", row
    assert len(_runs(cfg)) == 2


def test_close_merge_reopen_matrix(cfg_store, chain):
    cfg, store = cfg_store
    kw = {"model_factory": _terra_factory()}
    row = _ingest(cfg, store, pr_event(
        "opened", 23, chain["A"], chain["B"]), "d1", **kw)
    assert row["decision"] == "completed", row
    row = _ingest(cfg, store, pr_event(
        "closed", 23, chain["A"], chain["B"], merged=True), "d2",
        **kw)
    assert row["decision"] == "skipped:closed", row
    # Reopen with the identical head: nothing new to review.
    row = _ingest(cfg, store, pr_event(
        "reopened", 23, chain["A"], chain["B"]), "d3", **kw)
    assert row["decision"] == "skipped:unchanged", row
    assert len(_runs(cfg)) == 1
    # Reopen carrying a new head: fresh review of the delta.
    row = _ingest(cfg, store, pr_event(
        "reopened", 23, chain["A"], chain["C"]), "d4", **kw)
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 2


def test_concurrent_claim_serializes(cfg_store, chain):
    """Poll overlap: the atomic claim admits one worker; the loser
    queues instead of double-spending."""
    cfg, store = cfg_store
    assert store.claim_for_review(REPO, 23, base=chain["A"],
                                  head=chain["B"]) == "claimed"
    assert store.claim_for_review(REPO, 23, base=chain["A"],
                                  head=chain["C"]) == "queued"
    assert store.claim_for_review(REPO, 23, base=chain["A"],
                                  head=chain["B"]) == "already"


def _email_bits(reason: str) -> str:
    """The notified= outcome fragment (transport result, if any)."""
    marker = "notified="
    idx = reason.find(marker)
    assert idx != -1, reason
    tail = reason[idx:]
    cut = tail.find("; github-comment")
    return tail[:cut] if cut != -1 else tail


def test_publish_hold_keeps_email_independent(cfg_store, chain,
                                              monkeypatch):
    """HOLD default: no publish bits, email outcome untouched."""
    cfg, store = cfg_store
    monkeypatch.delenv("CODATIEL_GITHUB_COMMENTS", raising=False)
    row = _ingest(cfg, store, pr_event(
        "opened", 23, chain["A"], chain["B"]), "d1",
        model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    assert "github-comment" not in row["reason"]
    # The no-key transport failure is environmental; what matters is
    # the publish path adds nothing and changes nothing.
    assert _email_bits(row["reason"]).startswith("notified=")


def test_publish_unarmed_annotates_but_email_still_sends(cfg_store, chain,
                                                         monkeypatch):
    """Switch on but not armed: publish refuses, email outcome stands
    byte-identical to the HOLD run."""
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    cfg, store = cfg_store
    row = _ingest(cfg, store, pr_event(
        "opened", 23, chain["A"], chain["B"]), "d1",
        model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    assert "github-comment refused" in row["reason"]
    assert "arming" in row["reason"]
    hold_reason = ("full review complete (terra 0, luna-exclusive 0; "
                   "notified=False (RuntimeError: SMTP2GO_API_KEY is "
                   "not set in the environment)")
    assert _email_bits(row["reason"]) == _email_bits(hold_reason)


def test_cli_github_arm_and_publish_hold(cfg_store, monkeypatch):
    """Operator CLI: arming watermark is immutable; publishing under
    HOLD refuses without network."""
    from codatiel_i2.cli import cmd_events

    cfg, store = cfg_store

    class Args:
        op = "github-arm"
        payload = delivery_id = signature = ""
        pr = repo = head = base = run_dir = ""
        stale_after = -1.0
        ceiling = -1.0
        superseded_by = None

    out = cmd_events(Args())
    assert out["status"] == "ok", out
    assert store.meta_get("github_comments_armed_at")
    out2 = cmd_events(Args())
    assert out2["status"] == "failed", out2
    assert "immutable" in out2["error"]
