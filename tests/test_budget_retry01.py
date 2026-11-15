"""BUDGET-RETRY-01: bounded re-drive of pure budget refusals.

A ceiling-skipped (repo, PR, head) that is re-ingested under the same
deterministic delivery ID may be reviewed once spend budget is
available again. Bounds (all pinned here):

- same (repo, PR, head) as the refused row;
- refusal carried no spend (cost None or $0);
- no review has since covered the head;
- the live PR head positively confirms currency (unverifiable fails
  closed);
- at most BUDGET_RETRY_MAX_ATTEMPTS re-drives (counter survives
  re-refusals);
- every attempt re-checks all spend caps before any inference.

No paid inference: scripted models through the real runtime, real git
repos, fixture transports. A raising model factory proves the
no-inference paths.
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
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "B")
    b = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("LIMIT = 5\n")
    _git(root, "commit", "-qam", "C")
    c = _git(root, "rev-parse", "HEAD")
    return {"root": root, "A": a, "B": b, "C": c}


@pytest.fixture
def cfg_store(chain: dict, tmp_path: Path,
              monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", REPO)
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", "20")
    monkeypatch.setenv("CODATIEL_TEST_TO", "test-ops@example.com")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "codatiel-test@example.com")
    monkeypatch.setenv("CODATIEL_TEST_REPO_PATHS", f"{REPO}={chain['root']}")
    monkeypatch.setenv("REVIEW_PIPELINE", "terra_only")
    monkeypatch.setenv("CODATIEL_EVENTS_TRANSPORT", "fixture")
    monkeypatch.delenv("CODATIEL_WEBHOOK_SECRET", raising=False)
    cfg = P.config_from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = S.Store(cfg.state_dir / "events.sqlite")
    P.enable(cfg=cfg, store=store, discover=lambda r: ([], ""))
    yield cfg, store
    store.close()


def _terra_factory(final: dict | None = None):
    text = json.dumps(final if final is not None else
                      {"status": "silence", "summary": "ok", "findings": []})

    def _mk(role: str):
        script = [runtime_sdk.scripted_tool_call(
            call_id="t1", name="read_file",
            arguments={"path": "app.py", "revision": "head",
                       "start_line": 1, "end_line": 0},
            response_id="r1"),
            runtime_sdk.scripted_final(text, response_id="f1")]
        return runtime_sdk.ScriptedModel(script), "scripted:terra-test"
    return _mk


def _refusing_factory():
    def _mk(role: str):
        raise AssertionError("no inference is authorized on this path")
    return _mk


def _raw(action: str, number: int, base: str, head: str) -> dict:
    return {"action": action, "repository": {"full_name": REPO},
            "pull_request": {
                "number": number, "state": "open", "draft": False,
                "merged": False, "created_at": "9999-01-01T00:00:00Z",
                "base": {"sha": base}, "head": {"sha": head, "ref": "f"}}}


def _ingest(cfg, store, raw: dict, did: str, **kw):
    ev, err = P.normalize_event(raw, did)
    assert err == "", err
    return P.process_event(cfg=cfg, store=store, ev=ev, **kw)


def _runs(cfg) -> list[Path]:
    return sorted((cfg.state_dir / "runs").rglob("run.json"))


def _live(head: str | None):
    return lambda repo, pr: head


def _ceiling_refusal(cfg, store, monkeypatch, number: int, base: str,
                     head: str, did: str):
    """Drive one pure skipped:ceiling refusal (no inference)."""
    cfg.spend_cap_usd = 0.5  # reserve ($1) no longer fits
    monkeypatch.setattr(P, "get_live_head", _live(head))
    row = _ingest(cfg, store, _raw("opened", number, base, head), did,
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:ceiling", row
    assert row["cost_usd"] == 0.0, row
    return row


# -- happy path ---------------------------------------------------------------
def test_ceiling_refusal_retried_when_budget_opens(cfg_store, chain,
                                                   monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 1, a, b, "br0")
    assert _runs(cfg) == []
    # Budget opens; the same head re-ingested under the same delivery
    # ID reviews exactly once.
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, _raw("synchronize", 1, a, b), "br0",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 1
    final = store.delivery_get("br0")
    assert final["decision"] == "completed", final
    detail = json.loads(final["detail"] or "{}")
    assert detail.get("budget_retry_attempts") == 1, detail
    assert detail.get("budget_retry_from_decision") == "skipped:ceiling"
    cp = store.checkpoint_get(REPO, 1)
    assert cp["last_reviewed_head"] == b
    # A further duplicate is replay-blocked: no second review, no new
    # delivery row, dedup intact.
    row = _ingest(cfg, store, _raw("synchronize", 1, a, b), "br0",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == 1
    n = store.cx.execute(
        "SELECT COUNT(*) FROM deliveries WHERE delivery_id='br0'").fetchone()
    assert n[0] == 1


# -- bounds -------------------------------------------------------------------
def test_no_retry_while_budget_closed_and_attempts_bounded(cfg_store, chain,
                                                           monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 2, a, b, "br1")
    # Budget still closed: each re-ingest re-drives (live, same head)
    # but the spend cap refuses again with no inference; the attempt
    # counter advances exactly once per cycle.
    for n in range(1, P.BUDGET_RETRY_MAX_ATTEMPTS + 1):
        row = _ingest(cfg, store, _raw("synchronize", 2, a, b), "br1",
                      model_factory=_refusing_factory())
        assert row["decision"] == "skipped:ceiling", (n, row)
        assert _runs(cfg) == []
        got = store.delivery_get("br1")
        detail = json.loads(got["detail"] or "{}")
        assert detail.get("budget_retry_attempts") == n, (n, detail)
    # Attempts exhausted: further duplicates are replay-blocked and the
    # counter never moves again.
    row = _ingest(cfg, store, _raw("synchronize", 2, a, b), "br1",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    got = store.delivery_get("br1")
    assert "ceiling" in (json.loads(got["detail"] or "{}").get(
        "original_decision") or ""), got
    assert _prior_attempts(got) == P.BUDGET_RETRY_MAX_ATTEMPTS
    assert _runs(cfg) == []


def _prior_attempts(row: dict) -> int:
    return P._prior_retry_attempts(row)


def test_no_retry_when_head_moved(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b, c = chain["A"], chain["B"], chain["C"]
    _ceiling_refusal(cfg, store, monkeypatch, 3, a, b, "br2")
    # The refused head is no longer live: replay-blocked, no inference.
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(c))
    row = _ingest(cfg, store, _raw("synchronize", 3, a, b), "br2",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert _runs(cfg) == []
    # The replay wrapper preserves the ceiling refusal as original —
    # and the retry still fires later if the refused head is live
    # again (wrapper unwrap path) with budget open.
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, _raw("synchronize", 3, a, b), "br2",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    assert len(_runs(cfg)) == 1


def test_no_retry_when_head_already_reviewed(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 4, a, b, "br3")
    # The head gets reviewed through a different delivery (explicit
    # re-drive shape): the stale ceiling row must never cause a second
    # review of the same head.
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, _raw("synchronize", 4, a, b), "br3-explicit",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    row = _ingest(cfg, store, _raw("synchronize", 4, a, b), "br3",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == 1


def test_no_retry_when_live_head_unverifiable(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    _ceiling_refusal(cfg, store, monkeypatch, 5, a, b, "br4")
    # gh unreachable: fail closed, replay-blocked, no inference — the
    # refusal is retried on a later tick, not on a guess.
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(None))
    row = _ingest(cfg, store, _raw("synchronize", 5, a, b), "br4",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert _runs(cfg) == []
    assert _prior_attempts(store.delivery_get("br4")) == 0


def test_no_retry_when_refusal_carried_spend(cfg_store, chain, monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    # A spend-carrying ceiling row is not a pure refusal (defensive:
    # the pipeline never writes one, but the guard must hold).
    store.delivery_record(
        delivery_id="br5", event_type="pull_request", action="synchronize",
        repo=REPO, pr=6, head_sha=b, decision="skipped:ceiling",
        reason="forged spend-carrying refusal", cost_usd=5.0,
        detail={"operator_gate": True})
    monkeypatch.setattr(P, "get_live_head", _live(b))
    ev, err = P.normalize_event(_raw("synchronize", 6, a, b), "br5")
    assert err == "", err
    row = P.process_event(cfg=cfg, store=store, ev=ev,
                          model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert _runs(cfg) == []
    assert store.delivery_get("br5")["cost_usd"] == 5.0


def test_no_retry_for_non_ceiling_terminal_rows(cfg_store, chain,
                                                monkeypatch):
    cfg, store = cfg_store
    a, b = chain["A"], chain["B"]
    cfg.spend_cap_usd = 20.0
    monkeypatch.setattr(P, "get_live_head", _live(b))
    row = _ingest(cfg, store, _raw("opened", 7, a, b), "br6",
                  model_factory=_terra_factory())
    assert row["decision"] == "completed", row
    # Completed rows are never re-driven, even with the head live.
    row = _ingest(cfg, store, _raw("synchronize", 7, a, b), "br6",
                  model_factory=_refusing_factory())
    assert row["decision"] == "skipped:replay", row
    assert len(_runs(cfg)) == 1


# -- store compare-and-set ----------------------------------------------------
def test_reopen_budget_is_compare_and_set(cfg_store, chain):
    cfg, store = cfg_store
    b = chain["B"]
    store.delivery_record(
        delivery_id="br7", event_type="pull_request", action="synchronize",
        repo=REPO, pr=8, head_sha=b, decision="skipped:ceiling",
        reason="cap", cost_usd=0.0, detail={})
    # Stale attempt (recorded counter is 0, caller claims 3): the row
    # is left untouched so the caller falls back to replay.
    row = store.delivery_reopen_budget("br7", attempt=3, max_attempts=5)
    assert row["decision"] == "skipped:ceiling", row
    # Fresh attempt reclaims exactly once.
    row = store.delivery_reopen_budget("br7", attempt=1, max_attempts=5)
    assert row["decision"] == "in_progress", row
    detail = json.loads(row["detail"] or "{}")
    assert detail["budget_retry_attempts"] == 1
    assert detail["budget_retry_from_decision"] == "skipped:ceiling"
    # A second reclaim with the same attempt loses the race.
    row = store.delivery_reopen_budget("br7", attempt=1, max_attempts=5)
    assert row["decision"] == "in_progress", row
    assert json.loads(row["detail"] or "{}")[
        "budget_retry_attempts"] == 1
