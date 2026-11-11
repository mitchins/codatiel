"""IDENTITY-01: bound review identity across render and delivery.

Reproduces the crossed-DEV-email incident (Needletail #10 findings
labelled as YT2WIKI #35 and vice versa) and proves the fix: the
(repository, PR, base SHA, head SHA, run ID) identity is bound at
review creation and re-verified at every render/delivery step. A
caller-supplied label may only confirm the binding; any mismatch
refuses with zero outbound bytes.

Also covers: same-slug wrong-PR refusal, unbound-run rules,
concurrent swapped sends, resend-after-refusal, retry-with-wrong
label, operator bind-identity (corroborated vs unattested),
stage/run binding agreement, the publication-eligibility check
(head/threads/supersession), and per-run delivery history.
"""

import json
import subprocess
import threading
from pathlib import Path

import pytest

from codatiel_i2 import review_identity as RI

NEEDLE = ("mitchins/needletail-harness", 10)
YT = ("mitchins/yt2wiki", 35)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _mk_run(tmp_path: Path, name: str, slug: str, pr: int,
            base: str | None = None, head: str | None = None,
            bind: bool = True) -> Path:
    """A minimal bound run dir: run.json + shadow_result + metrics."""
    base = base or "a" * 40
    head = head or "b" * 40
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    owner, repo = slug.split("/")
    (d / "run.json").write_text(json.dumps({
        "run_id": "r1", "repo": f"/x/clones/{owner}__{repo}",
        "base_sha": base, "head_sha": head,
        "config": {"model": "scripted:x"}}))
    (d / "shadow_result.json").write_text(json.dumps(
        {"status": "complete", "counts": {}, "advisory": []}))
    (d / "metrics.json").write_text(json.dumps(
        {"status": "complete", "model": "scripted:x",
         "spend": {"usd_estimate": 0.01}}))
    if bind:
        RI.bind_run(d, repo_slug=slug, pr_number=pr, base_sha=base,
                    head_sha=head, run_id="r1", binder="test")
    return d


def _ctx(slug: str, pr: int) -> str:
    return f"dev:{slug}#{pr}"


# -- binding unit behaviour -------------------------------------------

def test_bind_roundtrip_and_idempotent(tmp_path: Path):
    d = _mk_run(tmp_path, "r", *NEEDLE, bind=False)
    first = RI.bind_run(d, repo_slug=NEEDLE[0], pr_number=NEEDLE[1],
                        base_sha="a" * 40, head_sha="b" * 40, run_id="r1",
                        binder="test")
    assert first["repo_slug"] == NEEDLE[0]
    assert RI.load_identity(d) == first
    again = RI.bind_run(d, repo_slug=NEEDLE[0], pr_number=NEEDLE[1],
                        base_sha="a" * 40, head_sha="b" * 40, run_id="r1",
                        binder="other")
    assert again == first  # safe retry, identical binding


def test_rebind_different_refused(tmp_path: Path):
    d = _mk_run(tmp_path, "r", *NEEDLE)
    with pytest.raises(ValueError, match="refusing rebind"):
        RI.bind_run(d, repo_slug=NEEDLE[0], pr_number=11,
                    base_sha="a" * 40, head_sha="b" * 40, run_id="r1",
                    binder="test")


def test_bind_rejects_bad_shapes(tmp_path: Path):
    d = _mk_run(tmp_path, "r", *NEEDLE, bind=False)
    with pytest.raises(ValueError, match="invalid repo slug"):
        RI.bind_run(d, repo_slug="not a slug", pr_number=10,
                    base_sha="a" * 40, head_sha="b" * 40, run_id="r1")
    with pytest.raises(ValueError, match="invalid PR number"):
        RI.bind_run(d, repo_slug=NEEDLE[0], pr_number=0,
                    base_sha="a" * 40, head_sha="b" * 40, run_id="r1")
    with pytest.raises(ValueError, match="invalid base SHA"):
        RI.bind_run(d, repo_slug=NEEDLE[0], pr_number=10,
                    base_sha="abc123", head_sha="b" * 40, run_id="r1")


def test_bind_rejects_artifact_disagreement(tmp_path: Path):
    d = _mk_run(tmp_path, "r", *NEEDLE, bind=False)
    (d / "semdup.json").write_text(json.dumps(
        {"status": "ok", "repo": YT[0]}))
    with pytest.raises(ValueError, match="does not match semdup repo"):
        RI.bind_run(d, repo_slug=NEEDLE[0], pr_number=NEEDLE[1],
                    base_sha="a" * 40, head_sha="b" * 40, run_id="r1")


def test_decode_clone_slug():
    assert RI.decode_clone_slug(
        "/x/clones/mitchins__needletail-harness") == NEEDLE[0]
    assert RI.decode_clone_slug("o/r") == "o/r"
    assert RI.decode_clone_slug("/tmp/pytest-1/multirepo") == ""
    assert RI.decode_clone_slug("") == ""
    # __ outside a clones/ tree is not an owner encoding.
    assert RI.decode_clone_slug("/tmp/my__repo") == ""
    assert RI.decode_clone_slug(
        "/data/s/clones/o__r") == "o/r"


def test_corrupt_identity_file_refused_never_rebound(tmp_path: Path):
    """A damaged binding refuses delivery AND rebinding (no silent
    downgrade, no infinite recursion)."""
    from codatiel_i2 import service as S
    d = _mk_run(tmp_path, "r", *NEEDLE, bind=False)
    (d / "identity.json").write_text("{truncated", encoding="utf-8")
    with pytest.raises(ValueError, match="present-but-damaged"):
        S.deliver_shadow(d, to=["t@example.com"], sender="s@example.com",
                         transport=lambda t, s, b, h=None: {"ok": True},
                         context=_ctx(*NEEDLE))
    with pytest.raises(ValueError, match="present-but-damaged"):
        S.deliver_shadow(d, to=["t@example.com"], sender="s@example.com",
                         transport=lambda t, s, b, h=None: {"ok": True})
    with pytest.raises(ValueError, match="present-but-unreadable"):
        RI.bind_run(d, repo_slug=NEEDLE[0], pr_number=NEEDLE[1],
                    base_sha="a" * 40, head_sha="b" * 40, run_id="r1")


# -- the crossed-delivery repro ----------------------------------------

def test_crossed_contexts_both_refused_zero_outbound(tmp_path: Path):
    """The exact incident: two runs, swapped caller labels."""
    from codatiel_i2 import service as S
    needle = _mk_run(tmp_path, "needle", *NEEDLE)
    yt = _mk_run(tmp_path, "yt", *YT)
    sent: list = []

    def _send(t, s, b, h=None):
        sent.append((t, s))
        return {"accepted_by_transport": True, "email_id": "x"}

    with pytest.raises(ValueError, match="does not match bound"):
        S.deliver_shadow(needle, to=["dev@example.com"],
                         sender="s@example.com", transport=_send,
                         context=_ctx(*YT))
    with pytest.raises(ValueError, match="does not match bound"):
        S.deliver_shadow(yt, to=["dev@example.com"],
                         sender="s@example.com", transport=_send,
                         context=_ctx(*NEEDLE))
    assert sent == []  # zero outbound bytes on either run
    assert not (needle / "delivery.json").exists()
    assert not (yt / "delivery.json").exists()
    # Refusals leave an auditable trace (no receipt).
    for d in (needle, yt):
        hist = (d / "delivery-history.jsonl").read_text().strip().splitlines()
        assert len(hist) == 1
        entry = json.loads(hist[0])
        assert entry["outcome"] == "refused"
        assert "does not match bound" in entry["error"]


def test_same_slug_wrong_pr_refused(tmp_path: Path):
    """Same repo, wrong PR number: still a misaddressed digest."""
    from codatiel_i2 import service as S
    needle = _mk_run(tmp_path, "needle", *NEEDLE)
    sent: list = []
    with pytest.raises(ValueError, match="does not match bound PR"):
        S.deliver_shadow(needle, to=["dev@example.com"],
                         sender="s@example.com",
                         transport=lambda t, s, b, h=None: sent.append(b),
                         context=_ctx(NEEDLE[0], 11))
    assert sent == []


def test_unbound_run_with_label_refused_without_label_legacy(
        tmp_path: Path):
    """A label with nothing to check against is refused; unlabeled
    legacy renders still work (path subject, no PR links)."""
    from codatiel_i2 import service as S
    d = _mk_run(tmp_path, "legacy", *NEEDLE, bind=False)
    with pytest.raises(ValueError, match="no bound identity"):
        S.deliver_shadow(d, to=["t@example.com"], sender="s@example.com",
                         transport=lambda t, s, b, h=None: {"ok": True},
                         context=_ctx(*NEEDLE))
    ok = S.deliver_shadow(d, to=["t@example.com"], sender="s@example.com",
                          transport=lambda t, s, b, h=None: {"ok": True})
    assert ok["subject"].startswith("[codatiel-shadow]")


def test_correct_labels_send_and_history_reconciles(tmp_path: Path):
    """Two runs, correct labels: two sends, history counts match."""
    from codatiel_i2 import service as S
    needle = _mk_run(tmp_path, "needle", *NEEDLE)
    yt = _mk_run(tmp_path, "yt", *YT)
    sent: list = []

    def _send(t, s, b, h=None):
        sent.append(s)
        return {"accepted_by_transport": True, "email_id": "e-" + s}

    r1 = S.deliver_shadow(needle, to=["dev@example.com"],
                          sender="s@example.com", transport=_send,
                          context=_ctx(*NEEDLE))
    r2 = S.deliver_shadow(yt, to=["dev@example.com"],
                          sender="s@example.com", transport=_send,
                          context=_ctx(*YT))
    assert r1["subject"].startswith("[codatiel] mitchins/needletail-harness#10")
    assert r2["subject"].startswith("[codatiel] mitchins/yt2wiki#35")
    assert len(sent) == 2
    for d in (needle, yt):
        hist = (d / "delivery-history.jsonl").read_text().strip().splitlines()
        assert len(hist) == 1
        assert json.loads(hist[0])["outcome"] == "sent"


def test_refusal_leaves_no_state_resend_after_refusal_ok(
        tmp_path: Path):
    """A refused attempt pollutes nothing; the correct resend works."""
    from codatiel_i2 import service as S
    needle = _mk_run(tmp_path, "needle", *NEEDLE)
    with pytest.raises(ValueError, match="does not match bound"):
        S.deliver_shadow(needle, to=["dev@example.com"],
                         sender="s@example.com",
                         transport=lambda t, s, b, h=None: {"ok": True},
                         context=_ctx(*YT))
    assert not (needle / "delivery.json").exists()
    sent: list = []
    ok = S.deliver_shadow(needle, to=["dev@example.com"],
                          sender="s@example.com",
                          transport=lambda t, s, b,
                          h=None: sent.append(b) or {"ok": True},
                          context=_ctx(*NEEDLE))
    assert sent and "mismatch" not in ok["subject"]


def test_retry_with_wrong_label_after_success_refused(tmp_path: Path):
    """A webhook retry carrying the wrong label is refused, not resent."""
    from codatiel_i2 import service as S
    needle = _mk_run(tmp_path, "needle", *NEEDLE)
    calls: list = []

    def _send(t, s, b, h=None):
        calls.append(b)
        return {"accepted_by_transport": True, "email_id": "e1"}

    S.deliver_shadow(needle, to=["dev@example.com"], sender="s@example.com",
                     transport=_send, context=_ctx(*NEEDLE))
    assert len(calls) == 1
    with pytest.raises(ValueError, match="does not match bound"):
        S.deliver_shadow(needle, to=["dev@example.com"],
                         sender="s@example.com", transport=_send,
                         context=_ctx(*YT), force=True)
    assert len(calls) == 1  # force does not override identity


def test_concurrent_swapped_sends_zero_outbound(tmp_path: Path):
    """Interleaved jobs with swapped labels: both refuse, nothing sent."""
    import threading
    from codatiel_i2 import service as S
    needle = _mk_run(tmp_path, "needle", *NEEDLE)
    yt = _mk_run(tmp_path, "yt", *YT)
    sent: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    errors: list = []

    def _send(t, s, b, h=None):
        with lock:
            sent.append(s)
        return {"accepted_by_transport": True}

    def _worker(run_dir, ctx):
        barrier.wait()
        try:
            S.deliver_shadow(run_dir, to=["dev@example.com"],
                             sender="s@example.com", transport=_send,
                             context=ctx)
        except ValueError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=_worker, args=(needle, _ctx(*YT))),
               threading.Thread(target=_worker, args=(yt, _ctx(*NEEDLE)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 2
    assert sent == []


def test_interleaved_correct_sends_both_delivered(tmp_path: Path):
    """Interleaved jobs with correct labels: both delivered."""
    import threading
    from codatiel_i2 import service as S
    needle = _mk_run(tmp_path, "needle", *NEEDLE)
    yt = _mk_run(tmp_path, "yt", *YT)
    sent: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def _send(t, s, b, h=None):
        with lock:
            sent.append(s)
        return {"accepted_by_transport": True}

    def _worker(run_dir, ctx):
        barrier.wait()
        S.deliver_shadow(run_dir, to=["dev@example.com"],
                         sender="s@example.com", transport=_send, context=ctx)

    threads = [threading.Thread(target=_worker, args=(needle, _ctx(*NEEDLE))),
               threading.Thread(target=_worker, args=(yt, _ctx(*YT)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(sent) == ["[codatiel] mitchins/needletail-harness#10 "
                            "bbbbbbbb complete",
                            "[codatiel] mitchins/yt2wiki#35 bbbbbbbb "
                            "complete"]


# -- model-level binding -----------------------------------------------

def test_model_uses_binding_not_caller_label(tmp_path: Path):
    """The model takes slug/PR/SHAs from the binding; a confirming
    label works, a contradicting one raises before any render."""
    from codatiel_i2 import digest_model as DM
    needle = _mk_run(tmp_path, "needle", *NEEDLE)
    m = DM.build_review_model(needle, context=_ctx(*NEEDLE))
    assert m["repo_slug"] == NEEDLE[0] and m["pr_number"] == 10
    assert m["pr_url"] == f"https://github.com/{NEEDLE[0]}/pull/10"
    with pytest.raises(ValueError, match="does not match bound"):
        DM.build_review_model(needle, context=_ctx(*YT))


# -- operator bind-identity --------------------------------------------

def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODATIEL_ENV", "test")
    monkeypatch.setenv("CODATIEL_TEST_STATE", str(tmp_path / "estate"))
    monkeypatch.setenv("CODATIEL_TEST_REPOS", "o/r")
    monkeypatch.setenv("CODATIEL_TEST_SPEND_CAP_USD", "20")
    monkeypatch.setenv("CODATIEL_TEST_TO", "t@example.com")
    monkeypatch.setenv("CODATIEL_TEST_SENDER", "s@example.com")
    monkeypatch.setenv("CODATIEL_TEST_REPO_PATHS", "o/r=/tmp/nowhere")
    monkeypatch.setenv("REVIEW_PIPELINE", "terra_only")
    monkeypatch.delenv("CODATIEL_WEBHOOK_SECRET", raising=False)


def test_bind_identity_cli_corroborated_and_refusals(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from codatiel_i2.cli import build_parser, cmd_events
    from codatiel_i2 import event_store as store_mod
    _env(monkeypatch, tmp_path)
    run_dir = str(tmp_path / "some-run")
    Path(run_dir).mkdir()
    (Path(run_dir) / "run.json").write_text(json.dumps({
        "run_id": "abc", "repo": "/x/clones/o__r",
        "base_sha": "a" * 40, "head_sha": "b" * 40}))
    store = store_mod.Store(tmp_path / "estate" / "events.sqlite")
    try:
        store.review_record(repo="o/r", pr=7, kind="full",
                            base_sha="a" * 40, head_sha="b" * 40,
                            diff_sha="d" * 40, files=1, pipeline="test",
                            status="complete", terra_findings=0,
                            luna_added=0, cost_usd=0.1,
                            cost_unknown=False, run_dir=run_dir)
        args = build_parser().parse_args(
            ["events", "bind-identity", "--repo", "o/r", "--pr", "7",
             "--run-dir", run_dir])
        out = cmd_events(args)
        assert out["status"] == "ok", out
        assert out["identity"]["pr_number"] == 7
        # Uncorroborated run dir: refused.
        args = build_parser().parse_args(
            ["events", "bind-identity", "--repo", "o/r", "--pr", "8",
             "--run-dir", str(tmp_path / "other")])
        bad = cmd_events(args)
        assert bad["status"] == "failed" and "corroborates" in bad["error"]
        # Re-requesting the identical binding is idempotent.
        args = build_parser().parse_args(
            ["events", "bind-identity", "--repo", "o/r", "--pr", "7",
             "--run-dir", run_dir])
        again = cmd_events(args)
        assert again["status"] == "ok", again
    finally:
        store.close()


def test_bind_identity_ambiguous_run_dir_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from codatiel_i2.cli import build_parser, cmd_events
    from codatiel_i2 import event_store as store_mod
    _env(monkeypatch, tmp_path)
    run_dir = str(tmp_path / "shared-run")
    Path(run_dir).mkdir()
    (Path(run_dir) / "run.json").write_text(json.dumps({
        "run_id": "abc", "repo": "/x/clones/o__r",
        "base_sha": "a" * 40, "head_sha": "b" * 40}))
    store = store_mod.Store(tmp_path / "estate" / "events.sqlite")
    try:
        for pr in (7, 8):
            store.review_record(repo="o/r", pr=pr, kind="full",
                                base_sha="a" * 40, head_sha="b" * 40,
                                diff_sha="d" * 40, files=1, pipeline="test",
                                status="complete", terra_findings=0,
                                luna_added=0, cost_usd=0.1,
                                cost_unknown=False, run_dir=run_dir)
        args = build_parser().parse_args(
            ["events", "bind-identity", "--repo", "o/r", "--pr", "7",
             "--run-dir", run_dir])
        out = cmd_events(args)
        assert out["status"] == "failed" and "ambiguous" in out["error"]
    finally:
        store.close()


# -- stage binding agreement --------------------------------------------

def _repo_with_dup(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "e.py").write_text("def g():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "n.py").write_text("def f():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "dup")
    return {"root": root, "base": base,
            "head": _git(root, "rev-parse", "HEAD")}


def test_stage_agrees_with_binding_and_refuses_mismatch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from codatiel_i2.semdup_dev import stage as ST
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    repo = _repo_with_dup(tmp_path)

    def _assess(*, evidence, head_rev, target_kind, tools, model,
                model_settings, ledger, ctx=None, call=None) -> dict:
        return {"ok": True, "terra": {"overlap": "x", "relationship": "y",
                                      "opportunity": "z",
                                      "shared_component": "w",
                                      "differences": []},
                "transcript": {"tool_calls": []}}

    def _edit(*, package, proposed_comment, tools, model, model_settings,
              ledger, ctx=None, call=None) -> dict:
        return {"ok": True, "comment": "reuse", "differences": ""}

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "r1", "repo": str(repo["root"]),
        "base_sha": repo["base"], "head_sha": repo["head"]}))
    RI.bind_run(run_dir, repo_slug="o/r", pr_number=1,
                base_sha=repo["base"], head_sha=repo["head"], run_id="r1",
                binder="test")
    out = ST.run_stage(repo_path=repo["root"], repo="o/r", pr=1,
                       base_rev=repo["base"], head_rev=repo["head"],
                       run_dir=run_dir, state_dir=tmp_path / "state",
                       allow_embeddings=False, assess_fn=_assess,
                       edit_fn=_edit)
    assert out["status"] == "ok", out
    stamped = json.loads((run_dir / "semdup.json").read_text())
    assert stamped["base_sha"] == repo["base"]
    assert stamped["head_sha"] == repo["head"]

    run2 = tmp_path / "run2"
    run2.mkdir()
    (run2 / "run.json").write_text(json.dumps({
        "run_id": "r2", "repo": str(repo["root"]),
        "base_sha": repo["base"], "head_sha": repo["head"]}))
    RI.bind_run(run2, repo_slug="o/r", pr_number=1,
                base_sha=repo["base"], head_sha=repo["head"], run_id="r2",
                binder="test")
    out2 = ST.run_stage(repo_path=repo["root"], repo="o/r", pr=2,
                        base_rev=repo["base"], head_rev=repo["head"],
                        run_dir=run2, state_dir=tmp_path / "state",
                        allow_embeddings=False, assess_fn=_assess,
                        edit_fn=_edit)
    assert out2["status"] == "unavailable"
    assert "identity mismatch" in out2["reason"]


# -- publication eligibility ---------------------------------------------

def _claim(path="src/a.py", start=10, end=14, title="Leak"):
    return {"claim_id": f"{path}:{start}",
            "changed_anchor": {"path": path, "line_start": start,
                               "line_end": end},
            "raw": {"title": title}, "publish_eligible": True}


def test_publication_superseded_pr_all_email(tmp_path: Path):
    from codatiel_i2.github import publish as pub
    out = pub.publication_eligibility(
        claims=[_claim()], bound_repo=YT[0], bound_pr=35,
        reviewed_head_sha="e" * 40, current_head_sha="e" * 40,
        superseded_by=38)
    assert out["publishable"] == [] and len(out["email_only"]) == 1
    assert "superseded by #38" in out["email_only"][0]["reason"]


def test_publication_head_moved_all_email():
    from codatiel_i2.github import publish as pub
    out = pub.publication_eligibility(
        claims=[_claim()], bound_repo=YT[0], bound_pr=35,
        reviewed_head_sha="a" * 40, current_head_sha="b" * 40)
    assert out["publishable"] == [] and "moved" in out["reason"]


def test_publication_suppresses_fixed_and_reported():
    from codatiel_i2.github import publish as pub
    head = "c" * 40
    out = pub.publication_eligibility(
        claims=[_claim("src/gone.py"), _claim("src/a.py"),
                _claim("src/b.py")],
        bound_repo=YT[0], bound_pr=35, reviewed_head_sha=head,
        current_head_sha=head, changed_paths={"src/a.py", "src/b.py"},
        existing_threads=[{"key": ["src/b.py", 10, 14, "Leak"]}])
    assert [c["changed_anchor"]["path"] for c in out["publishable"]] == [
        "src/a.py"]
    reasons = " ".join(e["reason"] for e in out["email_only"])
    assert "not in the PR's current changed set" in reasons
    assert "already reported" in reasons


def test_publication_ambiguous_routes_email_only():
    from codatiel_i2.github import publish as pub
    out = pub.publication_eligibility(
        claims=[_claim()], bound_repo=YT[0], bound_pr=35,
        reviewed_head_sha="a" * 40, current_head_sha="")
    assert out["publishable"] == [] and "ambiguous" in out["reason"]
    out2 = pub.publication_eligibility(
        claims=[{"claim_id": "x"}], bound_repo=YT[0], bound_pr=35,
        reviewed_head_sha="a" * 40, current_head_sha="a" * 40)
    assert out2["publishable"] == []  # unparseable anchor: email-only
    out3 = pub.publication_eligibility(
        claims=[_claim()], bound_repo=YT[0], bound_pr=35,
        reviewed_head_sha="a" * 40, current_head_sha="a" * 40,
        changed_paths=None)
    assert out3["publishable"] == []  # unknown changed set: email-only
    assert "changed set unknown" in out3["email_only"][0]["reason"]


def test_text_body_subject_must_name_bound_pr(tmp_path: Path):
    """Explicit subjects on the text path are checked too: a subject
    naming another PR is refused; the bound PR sends."""
    from codatiel_i2 import service as S
    d = _mk_run(tmp_path, "r", *NEEDLE)
    sent: list = []
    with pytest.raises(ValueError, match="explicit subject names"):
        S.deliver_shadow(
            d, to=["t@example.com"], sender="s@example.com",
            transport=lambda t, s, b, h=None: sent.append(b) or {"ok": True},
            subject=f"[codatiel-dev-semdup] {YT[0]}#{YT[1]} body",
            text_body="findings")
    assert sent == []
    ok = S.deliver_shadow(
        d, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b, h=None: sent.append(b) or {"ok": True},
        subject=f"[codatiel-dev-semdup] {NEEDLE[0]}#{NEEDLE[1]} body",
        text_body="findings")
    assert sent and "findings" in sent[0]


def test_text_body_subject_checked_against_artifacts(tmp_path: Path):
    """Unbound legacy run: a subject no artifact attests is refused."""
    from codatiel_i2 import service as S
    d = _mk_run(tmp_path, "r", *NEEDLE, bind=False)
    (d / "semdup.json").write_text(json.dumps(
        {"status": "ok", "repo": NEEDLE[0], "pr": NEEDLE[1]}))
    with pytest.raises(ValueError, match="stored artifacts attest"):
        S.deliver_shadow(
            d, to=["t@example.com"], sender="s@example.com",
            transport=lambda t, s, b, h=None: {"ok": True},
            subject=f"[codatiel-dev-semdup] {YT[0]}#{YT[1]} body",
            text_body="findings")


def test_worker_style_run_attests_subject(tmp_path: Path):
    """Semdup-worker report dirs (repo + pr keys) corroborate subjects."""
    from codatiel_i2 import service as S
    d = tmp_path / "worker-run"
    d.mkdir()
    (d / "run.json").write_text(json.dumps({
        "run_id": "semdup-dev-abc", "detector": "semdup-dev-01",
        "repo": NEEDLE[0], "pr": NEEDLE[1],
        "base_sha": "a" * 40, "head_sha": "b" * 40}))
    sent: list = []
    ok = S.deliver_shadow(
        d, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b, h=None: sent.append(b) or {"ok": True},
        subject=f"[codatiel-dev-semdup] {NEEDLE[0]}#{NEEDLE[1]} body",
        text_body="findings")
    assert sent and "findings" in sent[0]
    with pytest.raises(ValueError, match="stored artifacts attest"):
        S.deliver_shadow(
            d, to=["t@example.com"], sender="s@example.com",
            transport=lambda t, s, b, h=None: {"ok": True},
            subject=f"[codatiel-dev-semdup] {YT[0]}#{YT[1]} body",
            text_body="findings")


def test_subject_checked_alongside_rendered_digest(tmp_path: Path):
    """An explicit subject with the default rendered body is checked
    too — not just the text_body path."""
    from codatiel_i2 import service as S
    d = _mk_run(tmp_path, "r", *NEEDLE)
    with pytest.raises(ValueError, match="explicit subject names"):
        S.deliver_shadow(
            d, to=["t@example.com"], sender="s@example.com",
            transport=lambda t, s, b, h=None: {"ok": True},
            subject=f"[codatiel] {YT[0]}#{YT[1]} bbbbbbbb complete",
            context=_ctx(*NEEDLE))
    sent: list = []
    ok = S.deliver_shadow(
        d, to=["t@example.com"], sender="s@example.com",
        transport=lambda t, s, b, h=None: sent.append(s) or {"ok": True},
        subject=f"[codatiel] {NEEDLE[0]}#{NEEDLE[1]} bbbbbbbb complete",
        context=_ctx(*NEEDLE))
    assert sent and NEEDLE[0] in sent[0]


def test_subject_naming_pr_without_any_attestation_refused(tmp_path: Path):
    """Empty attestation + PR-naming subject: refused, not unchecked."""
    from codatiel_i2 import service as S
    d = tmp_path / "bare"
    d.mkdir()
    (d / "run.json").write_text(json.dumps({
        "run_id": "r9", "repo": "/tmp/somewhere",
        "base_sha": "a" * 40, "head_sha": "b" * 40}))
    (d / "shadow_result.json").write_text(json.dumps(
        {"status": "complete", "counts": {}, "advisory": []}))
    (d / "metrics.json").write_text(json.dumps(
        {"status": "complete", "model": "x", "spend": {"usd_estimate": 0}}))
    with pytest.raises(ValueError, match="stored artifacts attest"):
        S.deliver_shadow(
            d, to=["t@example.com"], sender="s@example.com",
            transport=lambda t, s, b, h=None: {"ok": True},
            subject="[codatiel] o/r#7 bbbbbbbb complete",
            text_body="findings")


def test_stage_mismatch_does_not_block_primary_delivery(tmp_path: Path):
    """A refused semdup stamp carries no identity, so the primary
    review still renders and delivers under its binding."""
    from codatiel_i2 import service as S
    from codatiel_i2.semdup_dev import stage as ST
    d = _mk_run(tmp_path, "r", *NEEDLE)
    out = ST.run_stage(repo_path=tmp_path, repo="other/repo", pr=99,
                       base_rev="a" * 40, head_rev="b" * 40,
                       run_dir=d, state_dir=tmp_path / "state",
                       allow_embeddings=False)
    assert out["status"] == "unavailable"
    assert "identity mismatch" in out["reason"]
    stamped = json.loads((d / "semdup.json").read_text())
    assert stamped.get("repo") is None and stamped.get("pr") is None
    sent: list = []
    ok = S.deliver_shadow(d, to=["t@example.com"], sender="s@example.com",
                          transport=lambda t, s, b,
                          h=None: sent.append(b) or {"ok": True},
                          context=_ctx(*NEEDLE))
    assert sent and ok["subject"].startswith(
        "[codatiel] mitchins/needletail-harness#10")


def test_publication_clean_claim_advisory_only_no_posting():
    """Even an eligible claim is advisory: posting stays disabled."""
    from codatiel_i2.github import publish as pub
    head = "d" * 40
    out = pub.publication_eligibility(
        claims=[_claim()], bound_repo=YT[0], bound_pr=35,
        reviewed_head_sha=head, current_head_sha=head,
        changed_paths={"src/a.py"}, existing_threads=[])
    assert len(out["publishable"]) == 1
    assert out["mode"] == "report_only"
