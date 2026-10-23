"""Codatiel v0.1.0 watcher acceptance gates (deterministic, no inference).

Issue §Acceptance: first-seen, unchanged-skip, debounce, BASE advance,
stale-during-run, restart catch-up, duplicate discovery, review failure,
spend exhaustion, SMTP retry without re-review, ambiguous receipt,
read-only GitHub scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codatiel_i2 import watcher as W


def make_cfg(tmp_path: Path, **over) -> W.WatcherConfig:
    kw = dict(repos=["o/r"], poll_interval_s=300, debounce_s=120,
              state_path=tmp_path / "state.json",
              runs_root=tmp_path / "runs", work_root=tmp_path / "work",
              recipients=["ops@example.com"], sender="codatiel@example.com",
              spend_total_cap_usd=5.0, spend_seed_usd=0.0,
              spend_daily_cap_usd=2.0)
    kw.update(over)
    return W.WatcherConfig(**kw)


def fake_run(tmp_path: Path, name: str, *, status: str = "complete",
             usd: float = 0.05, model: str = "luna:test",
             findings: int = 0) -> str:
    d = tmp_path / "runs" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(
        {"run_id": name, "repo": "o/r", "pr_number": 1,
         "base_sha": "b" * 40, "head_sha": "h" * 40,
         "config": {"model": model}}))
    (d / "metrics.json").write_text(json.dumps(
        {"status": status, "model": model, "tool_calls": 5,
         "instructions_id": "canonical-minimal-0.1@x",
         "spend": {"usd_estimate": usd, "accounting_status":
                   "CONFIRMED_USAGE"}}))
    (d / "shadow_result.json").write_text(json.dumps(
        {"status": status, "advisory": [], "silence": {"summary": "quiet"}}))
    (d / "findings.jsonl").write_text("")
    (d / "evidence.jsonl").write_text("")
    return str(d)


class Harness:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.prs: list[dict] = []
        self.reviews = 0
        self.haiku_reviews = 0
        self.sent: list[dict] = []
        self.receipt: dict = {"succeeded": 1, "failed": 0, "email_id": "e1"}
        self.fail_review = False
        self.live: dict | None = None

    def discover(self, repo: str, limit: int = 20):
        return list(self.prs), None

    def fetch(self, repo: str, number: int):
        if self.live is not None:
            return dict(self.live), None
        for pr in self.prs:
            if int(pr["number"]) == number:
                return {"number": number,
                        "baseRefOid": pr["baseRefOid"],
                        "headRefOid": pr["headRefOid"]}, None
        return None, "not found"

    def review_luna(self, repo: str, number: int):
        self.reviews += 1
        if self.fail_review:
            return {"status": "failed", "stage": "agent",
                    "error": "boom", "run_dir": None}
        return {"status": "complete", "stage": "done",
                "run_dir": fake_run(self.tmp, f"luna-{self.reviews}"),
                "model": "luna"}

    def review_haiku(self, repo: str, number: int):
        self.haiku_reviews += 1
        return {"status": "complete", "stage": "done",
                "run_dir": fake_run(self.tmp, f"haiku-{self.haiku_reviews}"),
                "model": "haiku"}

    def deliver(self, run_dir: Any, subject: str, body: str):
        self.sent.append({"run_dir": run_dir, "subject": subject,
                          "body": body})
        return dict(self.receipt)


def run_poll(cfg, state, h: Harness, now: float, day: str = "2026-10-23"):
    return W.poll_once(cfg, state, now=now, day=day, discover=h.discover,
                       fetch_shas=h.fetch, review_luna=h.review_luna,
                       review_haiku=h.review_haiku, deliver=h.deliver)


def settle(cfg, state, h: Harness, now: float, day: str = "2026-10-23"):
    """Prime debounce, then poll past the window; return the live summary."""
    run_poll(cfg, state, h, now=now, day=day)
    return run_poll(cfg, state, h, now=now + cfg.debounce_s + 5, day=day)


def pr(number=1, base="b" * 40, head="h" * 40):
    return {"number": number, "baseRefOid": base, "headRefOid": head}


def test_first_seen_pr_gets_review_and_email(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]
    out = settle(cfg, state, h, now=1000.0)
    assert h.reviews == 1 and len(h.sent) == 1
    assert out["reviewed"][0]["delivered"] is True
    assert "o/r#1" in h.sent[0]["subject"]


def test_unchanged_sha_skips_second_poll(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]
    settle(cfg, state, h, now=1000.0)
    out = run_poll(cfg, state, h, now=2000.0)
    assert h.reviews == 1 and out["skipped"][0]["why"] == "unchanged_sha"


def test_changed_head_debounces_then_reviews(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]
    settle(cfg, state, h, now=1000.0)
    h.prs = [pr(head="n" * 40)]
    out = run_poll(cfg, state, h, now=1250.0)
    assert h.reviews == 1 and out["skipped"][0]["why"] == "debounce"
    out = run_poll(cfg, state, h, now=2000.0)
    assert h.reviews == 2 and len(h.sent) == 2


def test_base_advance_triggers_new_review(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]
    settle(cfg, state, h, now=1000.0)
    h.prs = [pr(base="c" * 40)]
    run_poll(cfg, state, h, now=2000.0)  # new pair starts debounce
    out = run_poll(cfg, state, h, now=2200.0)
    assert h.reviews == 2


def test_stale_head_during_run_is_marked_and_labeled(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]
    h.live = {"number": 1, "baseRefOid": "b" * 40,
              "headRefOid": "z" * 40}
    out = settle(cfg, state, h, now=1000.0)
    assert out["reviewed"][0]["stale_during_run"] is True
    assert out["emails"][0]["stale"] is True
    assert "STALE-DURING-RUN" in h.sent[0]["body"]


def test_restart_catch_up_skips_delivered_pairs(tmp_path):
    cfg, h = make_cfg(tmp_path), Harness(tmp_path)
    state = W.default_state()
    h.prs = [pr()]
    settle(cfg, state, h, now=1000.0)
    W.save_state(cfg.state_path, state)
    # fresh process, same volume
    h2, state2 = Harness(tmp_path), W.load_state(cfg.state_path)
    h2.prs = [pr()]
    out = run_poll(cfg, state2, h2, now=5000.0)
    assert h2.reviews == 0 and out["skipped"][0]["why"] == "unchanged_sha"


def test_duplicate_discovery_reviews_once(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr(), pr()]
    out = settle(cfg, state, h, now=1000.0)
    assert h.reviews == 1


def test_review_failure_is_retryable_without_email(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs, h.fail_review = [pr()], True
    out = settle(cfg, state, h, now=1000.0)
    assert h.sent == [] and "error" in out["reviewed"][0]
    h.fail_review = False
    out = run_poll(cfg, state, h, now=2000.0)
    assert h.reviews == 2 and len(h.sent) == 1


def test_spend_exhaustion_pauses_with_single_notice(tmp_path):
    cfg = make_cfg(tmp_path, spend_total_cap_usd=0.05)
    h, state = Harness(tmp_path), W.default_state()
    h.prs = [pr()]
    settle(cfg, state, h, now=1000.0)  # spends 0.05, hits cap
    assert h.reviews == 1
    h.prs = [pr(head="n" * 40)]
    run_poll(cfg, state, h, now=5000.0)  # new pair starts debounce
    out = run_poll(cfg, state, h, now=5200.0)
    assert out["paused"] is True
    paused_emails = [e for e in h.sent if "paused-by-budget" in e["subject"]]
    assert len(paused_emails) == 1
    reviews_before = h.reviews
    run_poll(cfg, state, h, now=6000.0)
    assert h.reviews == reviews_before
    assert len([e for e in h.sent if "paused-by-budget" in e["subject"]]) == 1


def test_smtp_failure_retries_delivery_without_new_inference(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]
    h.receipt = {"succeeded": 0, "failed": 1, "failures": ["5xx"]}
    out = settle(cfg, state, h, now=1000.0)
    assert out["emails"][0]["result"] == "failed"
    assert out["reviewed"][0]["delivered"] is False
    h.receipt = {"succeeded": 1, "failed": 0, "email_id": "e2"}
    reviews_before = h.reviews
    out = run_poll(cfg, state, h, now=2000.0)
    assert h.reviews == reviews_before, "no second inference for transport failure"
    assert out["emails"][0]["redelivery"] is True
    assert out["reviewed"][0]["delivered"] is True
    assert out["reviewed"][0].get("redelivered") is True
    first = h.sent[0]
    assert (first["subject"], first["body"]) == W.build_review_email(
        Path(first["run_dir"]))


def test_transport_exception_does_not_lose_poll_state(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]

    def boom(*a: object) -> dict:
        raise ConnectionError("smtp down")

    run_poll(cfg, state, h, now=1000.0)  # prime debounce
    out = W.poll_once(cfg, state, now=2000.0, day="2026-10-23",
                      discover=h.discover, fetch_shas=h.fetch,
                      review_luna=h.review_luna,
                      review_haiku=h.review_haiku, deliver=boom)
    assert out["reviewed"][0]["delivery"] == "transport-error"
    assert out["reviewed"][0]["delivered"] is False


def test_missing_run_artifacts_skip_delivery(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]

    def ghost_luna(repo: str, number: int):
        h.reviews += 1
        return {"status": "complete", "stage": "done",
                "run_dir": str(tmp_path / "ghost"), "model": "luna"}

    run_poll(cfg, state, h, now=1000.0)  # prime debounce
    out = W.poll_once(cfg, state, now=2000.0, day="2026-10-23",
                      discover=h.discover, fetch_shas=h.fetch,
                      review_luna=ghost_luna,
                      review_haiku=h.review_haiku, deliver=h.deliver)
    assert h.sent == []
    assert "missing" in out["reviewed"][0]["error"]
    assert out["reviewed"][0]["usd"] == "unknown"


def test_ambiguous_receipt_classification_and_replay_idempotency(tmp_path):
    assert W.classify_receipt({"succeeded": 2}) == "delivered"
    assert W.classify_receipt({"failed": 1}) == "failed"
    assert W.classify_receipt({"accepted_by_transport": True}) == "ambiguous"
    assert W.classify_receipt(None) == "ambiguous"
    # identical rebuild => replay would suppress a delivered duplicate
    d = Path(fake_run(tmp_path, "rep1"))
    subj, body = W.build_review_email(d)
    assert (subj, body) == W.build_review_email(d)


def test_read_only_github_scope(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], **kw: object) -> object:
        calls.append(cmd)
        assert cmd[:2] == ["gh", "pr"], f"non-pr gh call: {cmd}"
        assert cmd[2] in ("list", "view"), f"mutating gh call: {cmd}"

        class P:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return P()

    monkeypatch.setattr("subprocess.run", fake_run_cmd)
    W.discover_prs("o/r", 5)
    W.fetch_pr_shas("o/r", 1)
    assert [c[2] for c in calls] == ["list", "view"]


def test_haiku_follow_up_only_on_trigger_status(tmp_path):
    cfg, h, state = make_cfg(tmp_path), Harness(tmp_path), W.default_state()
    h.prs = [pr()]
    run_poll(cfg, state, h, now=1000.0)  # prime debounce

    def incomplete_luna(repo: str, number: int):
        h.reviews += 1
        return {"status": "incomplete_budget_exhausted", "stage": "done",
                "run_dir": fake_run(tmp_path, f"luna-{h.reviews}"),
                "model": "luna"}

    out = W.poll_once(cfg, state, now=2000.0, day="2026-10-23",
                      discover=h.discover, fetch_shas=h.fetch,
                      review_luna=incomplete_luna,
                      review_haiku=h.review_haiku, deliver=h.deliver)
    assert h.haiku_reviews == 1
    assert any(r.get("second_pass") for r in out["reviewed"])
    assert "(conditional Haiku follow-up)" in h.sent[1]["subject"]


def test_packaged_guidebook_matches_repo_copy():
    import hashlib
    from pathlib import Path as _P
    repo = _P("docs/reviewer_methodology/guidebook_port.md")
    packaged = _P("codatiel_i2/guidebook_port.md")
    assert packaged.exists()
    assert (hashlib.sha256(packaged.read_bytes()).hexdigest()
            == hashlib.sha256(repo.read_bytes()).hexdigest())
