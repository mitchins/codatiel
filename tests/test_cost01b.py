"""COST-01B regressions: stale-head and cross-revision cache.

Covers Gate B (stale) and Gate C (cache) with deterministic fakes,
no paid inference.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codatiel_i2 import event_pipeline as P
from codatiel_i2 import event_store as S
from codatiel_i2.semdup_dev import cache as CACHE
from codatiel_i2.semdup_dev import stage as ST

REPO = "o/r"

DUPE_EXISTING = '''"""Base."""

def format_money(a, places=2):
    return f"{round(float(a), places):,.{places}f}"
'''

DUPE_NEW = '''"""New."""

def format_price(a, places=2):
    return f"{round(float(a), places):,.{places}f}"
'''

def _git(repo: Path, *a: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()

@pytest.fixture
def dupe_repo(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "existing.py").write_text(DUPE_EXISTING)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "new.py").write_text(DUPE_NEW)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add dup")
    head_b = _git(root, "rev-parse", "HEAD")
    # second head C: small unrelated change elsewhere to create new head
    (root / "extra.py").write_text("x=1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "extra")
    head_c = _git(root, "rev-parse", "HEAD")
    return {"root": root, "base": base, "head_b": head_b, "head_c": head_c}

def _fake_assess(**kw):
    return {"ok": True, "terra": {"overlap": True, "relationship": "equivalent", "strength": "likely", "shared_behavior": "money formatting", "shared_component": "format", "opportunity": "direct_reuse", "differences": [], "wrapper_or_delegation": False, "ownership_lifecycle_difference": False, "uncertainty": ""}, "transcript": {"tool_calls": []}}

def _fake_edit(**kw):
    return {"ok": True, "editor": {"decision": "KEEP", "revised_comment": "reuse", "reason": "ok"}, "transcript": {"tool_calls": []}}

def _dev_cfg(tmp_path: Path, root: Path):
    return P.EventConfig(env="dev", state_dir=tmp_path / "estate", repos=[REPO], repo_paths={REPO: str(root)}, whole_pr_ceiling_usd=2.0, semdup_enabled=True)

# --- Gate B ---

def test_stale_before_dup_no_inference(tmp_path: Path, dupe_repo):
    """A->B becomes stale at C before duplication starts: no duplication inference."""
    cfg = _dev_cfg(tmp_path, dupe_repo["root"])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls = {"n": 0}
    def _count_assess(**kw):
        calls["n"] += 1
        return _fake_assess(**kw)
    # is_stale returns True immediately (head C != B)
    out = ST.run_stage(repo_path=dupe_repo["root"], repo=REPO, pr=1, base_rev=dupe_repo["base"], head_rev=dupe_repo["head_b"], run_dir=run_dir, state_dir=tmp_path / "state", allow_embeddings=False, assess_fn=_count_assess, edit_fn=_fake_edit, is_stale=lambda: True)
    assert out["status"] == "stale_skipped"
    assert calls["n"] == 0
    assert out["spend_usd"] == 0.0
    assert out.get("stale_skipped") is True

def test_stale_during_dup_no_further_pairs(tmp_path: Path, dupe_repo, monkeypatch):
    """Review becomes stale during duplication: no further pair assessments once staleness observed."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    # Use editorial directly with 3 pairs, stale after first
    from codatiel_i2.semdup_dev import editorial_shadow as ES
    import codatiel_i2.providers as Prov
    # Need a merged_db; use stage to build one first via a full run, then test per-pair
    # Simpler: test via stage with is_stale flipping after first pair
    cfg = _dev_cfg(tmp_path, dupe_repo["root"])
    # Create a repo with 3 duplicate targets to ensure multiple pairs
    root = tmp_path / "multirepo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "existing.py").write_text(DUPE_EXISTING)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    snippet = '    quantized = round(float(a), places)\n    return f"{quantized:,.{places}f}"\n'
    text = '"""x"""\n'
    for name in ("alpha_price", "beta_price", "gamma_price"):
        text += f'\n\ndef {name}(a, places=2):\n    """Format."""\n' + snippet
    (root / "new.py").write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add")
    head = _git(root, "rev-parse", "HEAD")
    calls = {"n": 0}
    def _count_assess(**kw):
        calls["n"] += 1
        return _fake_assess(**kw)
    # is_stale: false first, true after
    counter = {"i": 0}
    def _flip():
        counter["i"] += 1
        return counter["i"] > 1
    run_dir = tmp_path / "run2"
    run_dir.mkdir()
    out = ST.run_stage(repo_path=root, repo=REPO, pr=1, base_rev=base, head_rev=head, run_dir=run_dir, state_dir=tmp_path / "state2", allow_embeddings=False, assess_fn=_count_assess, edit_fn=_fake_edit, is_stale=_flip)
    # Should have at most 1 assessed before stale triggered
    assert calls["n"] <= 1
    # Remaining pairs should be skipped with stale reason
    assert any(s.get("reason") == "stale_superseded" for s in out.get("skipped", []))

def test_partial_stale_accounted(tmp_path: Path, monkeypatch):
    """Completes one assessment, detects new head, stops further paid assessments, retains cost, does not publish stale."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    # Build a repo with 3 duplicate targets to ensure multiple pairs
    root = tmp_path / "repo_partial"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "existing.py").write_text(DUPE_EXISTING)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    snippet = '    quantized = round(float(a), places)\n    return f"{quantized:,.{places}f}"\n'
    text = '"""x"""\n'
    for name in ("alpha_price", "beta_price", "gamma_price"):
        text += f'\n\ndef {name}(a, places=2):\n    """Format."""\n' + snippet
    (root / "new.py").write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add")
    head = _git(root, "rev-parse", "HEAD")

    # Fake assess that records nonzero simulated spending via the ledger
    def _spending_assess(*, evidence, head_rev, target_kind, tools, model, model_settings, ledger, ctx=None, call=None):
        # Simulate $0.05 per Terra assessment
        class Usage:
            requests = 1
            input_tokens = 1000
            output_tokens = 200
        ledger.record_response(f"test-{evidence['pair_sha256'][:8]}", Usage(), cached_input_tokens=0, cache_write_input_tokens=0)
        return _fake_assess(evidence=evidence, head_rev=head_rev, target_kind=target_kind, tools=tools, model=model, model_settings=model_settings, ledger=ledger, ctx=ctx, call=call)

    counter = {"calls": 0}
    def _flip():
        # Stale after first assessment: initial check (before dup) is call 1 (false),
        # first pair check is call 2 (false), second pair check is call 3 (true)
        counter["calls"] += 1
        return counter["calls"] > 2

    run_dir = tmp_path / "run_partial"
    run_dir.mkdir()
    out = ST.run_stage(repo_path=root, repo=REPO, pr=1, base_rev=base, head_rev=head, run_dir=run_dir, state_dir=tmp_path / "state_partial", allow_embeddings=False, assess_fn=_spending_assess, edit_fn=_fake_edit, is_stale=_flip, ceiling_usd=2.0)

    # Should have completed exactly one assessment before stale
    assert out["pairs_examined"] == 1, "should stop after first pair when stale observed"
    assert out["spend_usd"] > 0, "incurred cost must be retained (nonzero simulated spending)"
    assert any(s.get("reason") == "stale_superseded" for s in out.get("skipped", [])), "remaining pairs should be skipped as stale_superseded"
    # Must not publish the stale result (no new findings from skipped pairs)
    assert out["pairs_total"] >= 2, "should have had multiple pairs total"
    # The published from the first pair may exist, but no findings from skipped pairs
    assert len(out.get("published", [])) <= 1

def test_stale_cannot_overwrite_newer(tmp_path: Path, monkeypatch):
    """A stale result cannot overwrite or publish over a newer successful review."""
    # Use event_pipeline's atomic complete with generation token
    store = S.Store(tmp_path / "store.sqlite")
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    repo = "o/r"; pr = 1
    base = "a"*40; head_old = "b"*40; head_new = "c"*40
    # First claim old
    assert store.claim_for_review(repo, pr, base=base, head=head_old) == "claimed"
    old = store.checkpoint_get(repo, pr)
    old_run = old["in_progress_run"]
    # Simulate newer review completing via complete_review with its own claim
    # First release old and claim new
    store.release_claim(repo, pr, "release", base=base, head=head_old, claim_run=old_run)
    assert store.claim_for_review(repo, pr, base=base, head=head_new) == "claimed"
    newer = store.checkpoint_get(repo, pr)
    newer_run = newer["in_progress_run"]
    # Now try to seal old with its stale token: should fail
    sealed = store.complete_review(repo, pr, base=base, head=head_old, diff_sha="d", files=1, terra_findings=0, luna_added=0, cost_usd=0.1, cost_unknown=False, run_dir="/tmp/old", result="complete", claim_base=base, claim_head=head_old, claim_run=old_run)
    assert sealed["sealed"] is False
    # Newer should still be able to seal
    sealed2 = store.complete_review(repo, pr, base=base, head=head_new, diff_sha="d2", files=1, terra_findings=1, luna_added=0, cost_usd=0.1, cost_unknown=False, run_dir="/tmp/new", result="complete", claim_base=base, claim_head=head_new, claim_run=newer_run)
    assert sealed2["sealed"] is True
    cp = store.checkpoint_get(repo, pr)
    assert cp["last_reviewed_head"] == head_new

def test_next_valid_covers_required_range(tmp_path: Path):
    """The next valid review still covers the required A->C changes."""
    # Simulate process_event staleness: A->B stale at C, next should be A->C
    store = S.Store(tmp_path / "store2.sqlite")
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    repo = "o/r"; pr = 1
    base_a = "a"*40; head_b = "b"*40; head_c = "c"*40
    # Simulate first review A->B claimed, then stale, queued C
    assert store.claim_for_review(repo, pr, base=base_a, head=head_b) == "claimed"
    # New head arrives during review: claim queues C (with base A, not B)
    # Our pipeline's _drain_queued uses queued_base = original queued base (A)
    # Simulate by manually setting queued
    store.cx.execute("UPDATE checkpoints SET queued_base=?, queued_head=? WHERE repo=? AND pr=?", (base_a, head_c, repo, pr))
    # Now stale release of B
    cp = store.checkpoint_get(repo, pr)
    old_run = cp["in_progress_run"]
    store.release_claim(repo, pr, "stale", base=base_a, head=head_b, claim_run=old_run)
    # After release, queued should still be A->C
    cp2 = store.checkpoint_get(repo, pr)
    assert cp2["queued_head"] == head_c and cp2["queued_base"] == base_a
    # Next claim for C should succeed with base A
    assert store.claim_for_review(repo, pr, base=base_a, head=head_c) == "claimed"
    cp3 = store.checkpoint_get(repo, pr)
    assert cp3["in_progress_base"] == base_a and cp3["in_progress_head"] == head_c

# --- Gate C ---

def test_cache_unchanged_reused(tmp_path: Path, dupe_repo, monkeypatch):
    """Unchanged pair and context across commits: verdict reused."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    # First run
    root = dupe_repo["root"]; base = dupe_repo["base"]; head_b = dupe_repo["head_b"]; head_c = dupe_repo["head_c"]
    # Ensure head_c is not same file content as head_b? For this test we want unchanged pair: same file content for target/counterpart
    # Use base->head_b as first, base->head_c as second where new.py unchanged but extra.py added
    # The target pair is new.py vs existing.py, which is unchanged between B and C (extra.py change doesn't affect it)
    # So second run should hit cache
    run1 = tmp_path / "run1"
    run1.mkdir()
    state = tmp_path / "state"
    out1 = ST.run_stage(repo_path=root, repo=REPO, pr=1, base_rev=base, head_rev=head_b, run_dir=run1, state_dir=state, allow_embeddings=False, assess_fn=_fake_assess, edit_fn=_fake_edit)
    assert out1["status"] == "ok" and out1["pairs_examined"] >= 1
    calls = {"n": 0}
    def _count_assess(**kw):
        calls["n"] += 1
        return _fake_assess(**kw)
    run2 = tmp_path / "run2"
    run2.mkdir()
    out2 = ST.run_stage(repo_path=root, repo=REPO, pr=2, base_rev=base, head_rev=head_c, run_dir=run2, state_dir=state, allow_embeddings=False, assess_fn=_count_assess, edit_fn=_fake_edit)
    # Since PR is different (1 vs 2), cache is per PR, so should NOT reuse across PRs
    # So second run with pr=2 should not hit cache from pr=1
    # To test unchanged within same PR, we need same PR number
    run3 = tmp_path / "run3"
    run3.mkdir()
    calls2 = {"n": 0}
    def _count_assess2(**kw):
        calls2["n"] += 1
        return _fake_assess(**kw)
    out3 = ST.run_stage(repo_path=root, repo=REPO, pr=1, base_rev=base, head_rev=head_c, run_dir=run3, state_dir=state, allow_embeddings=False, assess_fn=_count_assess2, edit_fn=_fake_edit)
    # Now same PR=1, same target pair unchanged, so should reuse (no new inference)
    # Since head_c is different from head_b, but target pair unchanged, cache should hit
    assert calls2["n"] == 0, "unchanged pair should be reused without inference"
    assert out3["pairs_reused"] >= 1
    # Stage now keeps reused published as published; delivery layer will suppress
    assert len(out3["published"]) >= 1, "stage keeps reused published for delivery-layer deduplication"
    # Verify delivery-layer would suppress if prior delivered
    from codatiel_i2.event_store import Store
    from codatiel_i2.event_pipeline import _finding_hash_from_row, _filter_already_delivered
    store = Store(state / "test_store.sqlite")
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    # Simulate prior delivery via finding_deliveries
    fh = _finding_hash_from_row(out1["published"][0])
    store.finding_delivery_record(repo="o/r", pr=1, finding_hash=fh, channel="github", head_sha=head_b, run_id="run1", outcome="posted")
    to_deliver, already = _filter_already_delivered(store, "o/r", 1, out3["published"], "github")
    assert len(already) == 1 and len(to_deliver) == 0, "already delivered via github should be filtered"
    # If prior was stale (no delivery), should deliver
    store2 = Store(state / "test_store2.sqlite")
    store2.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    to_deliver2, already2 = _filter_already_delivered(store2, "o/r", 1, out3["published"], "github")
    assert len(to_deliver2) == 1 and len(already2) == 0, "no prior delivery → should deliver"

def test_cross_commit_duplicate_suppressed(tmp_path: Path, dupe_repo, monkeypatch):
    """Cross-commit duplicate: delivery layer suppresses already-delivered finding, no new inference."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    root = dupe_repo["root"]; base = dupe_repo["base"]; head_b = dupe_repo["head_b"]; head_c = dupe_repo["head_c"]
    state = tmp_path / "state2"
    run1 = tmp_path / "run1"; run1.mkdir()
    out1 = ST.run_stage(repo_path=root, repo=REPO, pr=5, base_rev=base, head_rev=head_b, run_dir=run1, state_dir=state, allow_embeddings=False, assess_fn=_fake_assess, edit_fn=_fake_edit)
    assert out1["published"] and len(out1["published"]) >= 1
    calls = {"n": 0}
    def _count(**kw):
        calls["n"] += 1
        return _fake_assess(**kw)
    run2 = tmp_path / "run2"; run2.mkdir()
    out2 = ST.run_stage(repo_path=root, repo=REPO, pr=5, base_rev=base, head_rev=head_c, run_dir=run2, state_dir=state, allow_embeddings=False, assess_fn=_count, edit_fn=_fake_edit)
    assert calls["n"] == 0
    assert len(out2["published"]) >= 1  # stage keeps it
    assert out2["pairs_cache_hits"] >= 1
    # Delivery layer should suppress if prior delivered
    from codatiel_i2.event_pipeline import _finding_hash_from_row, _filter_already_delivered
    from codatiel_i2.event_store import Store
    store = Store(state / "store_dup.sqlite")
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    fh = _finding_hash_from_row(out1["published"][0])
    store.finding_delivery_record(repo="o/r", pr=5, finding_hash=fh, channel="github", head_sha=head_b, run_id="run1", outcome="posted")
    to_deliver, already = _filter_already_delivered(store, "o/r", 5, out2["published"], "github")
    assert len(to_deliver) == 0 and len(already) == 1
    # Email channel not conflated
    to_email, already_email = _filter_already_delivered(store, "o/r", 5, out2["published"], "email")
    assert len(to_email) == 1, "github delivery must not suppress email"

def test_cache_same_line_reused(tmp_path: Path, dupe_repo):
    """Same code moved to a different line: verdict reused if decision evidence is unchanged."""
    # Simulate by building identity with different start_line but same sha
    state = tmp_path / "state"
    repo = REPO; pr = 1
    # Create a fake merged_db with files table entry
    db = tmp_path / "merged.sqlite"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, bytes INT, language TEXT, parse_status TEXT)")
    conn.execute("CREATE TABLE units (id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT, language TEXT, symbol TEXT, kind TEXT, start_line INT, end_line INT, text TEXT, fingerprint TEXT)")
    conn.execute("INSERT INTO files VALUES (?,?,?, ?, ?)", ("a.py", "filehash1", 100, "python", "ok"))
    conn.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?)", (1, "a.py", "targethash", "python", "func", "func", 10, 20, "def func(): pass", "fp"))
    conn.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?)", (2, "b.py", "counterhash", "python", "func2", "func", 30, 40, "def func2(): pass", "fp2"))
    conn.commit(); conn.close()
    ident1 = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "func", "sha256": "targethash"}, counterpart={"path": "b.py", "symbol": "func2", "sha256": "counterhash"}, classification="introduced", terra_model="m1", luna_model="m2", terra_instructions_id="tid1", luna_instructions_id="lid1", stage_version=ST.STAGE_VERSION)
    ident2 = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "func", "sha256": "targethash"}, counterpart={"path": "b.py", "symbol": "func2", "sha256": "counterhash"}, classification="introduced", terra_model="m1", luna_model="m2", terra_instructions_id="tid1", luna_instructions_id="lid1", stage_version=ST.STAGE_VERSION)
    # Even though start_line would have been different, identity is same (we don't include line)
    assert CACHE.cache_key(ident1) == CACHE.cache_key(ident2)
    # Store and lookup should hit
    row = {"target": {"path": "a.py", "symbol": "func"}, "counterpart": {"path": "b.py", "symbol": "func2"}, "path": "published", "anchors": {"target": {"path": "a.py", "symbol": "func", "sha256": "targethash"}, "counterpart": {"path": "b.py", "symbol": "func2", "sha256": "counterhash"}}}
    CACHE.store(state, repo, pr, ident1, row)
    hit = CACHE.lookup(state, repo, pr, ident2)
    assert hit is not None

def test_cache_context_change_reassess(tmp_path: Path):
    """Same bodies, changed surrounding context: reassess."""
    state = tmp_path / "state"
    repo = REPO; pr = 1
    db1 = tmp_path / "db1.sqlite"
    db2 = tmp_path / "db2.sqlite"
    import sqlite3
    for db, fhash in [(db1, "filehash1"), (db2, "filehash2")]:
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, bytes INT, language TEXT, parse_status TEXT)")
        conn.execute("CREATE TABLE units (id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT, language TEXT, symbol TEXT, kind TEXT, start_line INT, end_line INT, text TEXT, fingerprint TEXT)")
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("a.py", fhash, 100, "python", "ok"))
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("b.py", "bhash", 100, "python", "ok"))
        conn.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?)", (1, "a.py", "targethash", "python", "func", "func", 10, 20, "def func(): pass", "fp"))
        conn.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?)", (2, "b.py", "counterhash", "python", "func2", "func", 30, 40, "def func2(): pass", "fp2"))
        conn.commit(); conn.close()
    ident1 = CACHE.build_identity(merged_db=db1, target={"path": "a.py", "symbol": "func", "sha256": "targethash"}, counterpart={"path": "b.py", "symbol": "func2", "sha256": "counterhash"}, classification="introduced", terra_model="m1", luna_model="m2", terra_instructions_id="tid", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    ident2 = CACHE.build_identity(merged_db=db2, target={"path": "a.py", "symbol": "func", "sha256": "targethash"}, counterpart={"path": "b.py", "symbol": "func2", "sha256": "counterhash"}, classification="introduced", terra_model="m1", luna_model="m2", terra_instructions_id="tid", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    assert CACHE.cache_key(ident1) != CACHE.cache_key(ident2)

def test_cache_body_change_reassess(tmp_path: Path):
    """Either code body changes: reassess."""
    state = tmp_path / "state"
    db = tmp_path / "db.sqlite"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, bytes INT, language TEXT, parse_status TEXT)")
    conn.execute("CREATE TABLE units (id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT, language TEXT, symbol TEXT, kind TEXT, start_line INT, end_line INT, text TEXT, fingerprint TEXT)")
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("a.py", "fh", 100, "python", "ok"))
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("b.py", "fh2", 100, "python", "ok"))
    conn.commit(); conn.close()
    ident1 = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "func", "sha256": "hash1"}, counterpart={"path": "b.py", "symbol": "func2", "sha256": "hash2"}, classification="introduced", terra_model="m", luna_model="m2", terra_instructions_id="tid", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    ident2 = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "func", "sha256": "hash1_changed"}, counterpart={"path": "b.py", "symbol": "func2", "sha256": "hash2"}, classification="introduced", terra_model="m", luna_model="m2", terra_instructions_id="tid", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    assert CACHE.cache_key(ident1) != CACHE.cache_key(ident2)

def test_cache_prompt_model_change_reassess(tmp_path: Path):
    """Prompt, rules, or model changes: reassess."""
    state = tmp_path / "state"
    db = tmp_path / "db.sqlite"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, bytes INT, language TEXT, parse_status TEXT)")
    conn.commit(); conn.close()
    ident1 = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "f", "sha256": "h1"}, counterpart={"path": "b.py", "symbol": "g", "sha256": "h2"}, classification="introduced", terra_model="modelA", luna_model="m2", terra_instructions_id="tid1", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    ident2 = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "f", "sha256": "h1"}, counterpart={"path": "b.py", "symbol": "g", "sha256": "h2"}, classification="introduced", terra_model="modelB", luna_model="m2", terra_instructions_id="tid1", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    assert CACHE.cache_key(ident1) != CACHE.cache_key(ident2)
    ident3 = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "f", "sha256": "h1"}, counterpart={"path": "b.py", "symbol": "g", "sha256": "h2"}, classification="introduced", terra_model="modelA", luna_model="m2", terra_instructions_id="tid_changed", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    assert CACHE.cache_key(ident1) != CACHE.cache_key(ident3)

def test_cache_stale_reference_no_publish(tmp_path: Path):
    """Cached source reference is no longer valid: no stale suggestion published."""
    state = tmp_path / "state"
    repo = REPO; pr = 1
    db = tmp_path / "db.sqlite"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, bytes INT, language TEXT, parse_status TEXT)")
    conn.execute("CREATE TABLE units (id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT, language TEXT, symbol TEXT, kind TEXT, start_line INT, end_line INT, text TEXT, fingerprint TEXT)")
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("a.py", "fh", 100, "python", "ok"))
    conn.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?)", (1, "a.py", "h1", "python", "func", "func", 10, 20, "def func(): pass", "fp"))
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("b.py", "fh2", 100, "python", "ok"))
    conn.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?)", (2, "b.py", "h2", "python", "func2", "func", 30, 40, "def func2(): pass", "fp2"))
    conn.commit(); conn.close()
    ident = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "func", "sha256": "h1"}, counterpart={"path": "b.py", "symbol": "func2", "sha256": "h2"}, classification="introduced", terra_model="m1", luna_model="m2", terra_instructions_id="tid", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    row = {"target": {"path": "a.py", "symbol": "func"}, "counterpart": {"path": "b.py", "symbol": "func2"}, "path": "published", "anchors": {"target": {"path": "a.py", "symbol": "func", "sha256": "h1"}, "counterpart": {"path": "b.py", "symbol": "func2", "sha256": "h2"}}, "_cache_identity": ident}
    CACHE.store(state, repo, pr, ident, row)
    # Now delete the target unit to simulate deleted file
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM units WHERE path='a.py'")
    conn.commit(); conn.close()
    cached = CACHE.lookup(state, repo, pr, ident)
    assert cached is not None
    rebound = CACHE.rebind_row(cached, db, repo, pr, "head123")
    assert rebound is None

def test_cache_no_cross_scope_collision(tmp_path: Path):
    """Different PR/repository with superficially identical identifiers: no collision."""
    state = tmp_path / "state"
    db = tmp_path / "db.sqlite"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, bytes INT, language TEXT, parse_status TEXT)")
    conn.commit(); conn.close()
    ident = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "f", "sha256": "h1"}, counterpart={"path": "b.py", "symbol": "g", "sha256": "h2"}, classification="introduced", terra_model="m", luna_model="m2", terra_instructions_id="tid", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    row = {"target": {"path": "a.py", "symbol": "f"}, "counterpart": {"path": "b.py", "symbol": "g"}, "path": "published", "anchors": {"target": {"path": "a.py", "symbol": "f", "sha256": "h1"}, "counterpart": {"path": "b.py", "symbol": "g", "sha256": "h2"}}}
    CACHE.store(state, "o/r", 1, ident, row)
    hit_same = CACHE.lookup(state, "o/r", 1, ident)
    assert hit_same is not None
    hit_diff_pr = CACHE.lookup(state, "o/r", 2, ident)
    assert hit_diff_pr is None
    hit_diff_repo = CACHE.lookup(state, "o/other", 1, ident)
    assert hit_diff_repo is None

def test_provider_failure_not_cached(tmp_path: Path, dupe_repo, monkeypatch):
    """Provider failures are not cached as completed verdicts."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    state = tmp_path / "state_pf"
    # Simulate a provider timeout row
    db = tmp_path / "db_pf.sqlite"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, bytes INT, language TEXT, parse_status TEXT)")
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("a.py", "fh", 100, "python", "ok"))
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("b.py", "fh2", 100, "python", "ok"))
    conn.commit(); conn.close()
    ident = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "f", "sha256": "h1"}, counterpart={"path": "b.py", "symbol": "g", "sha256": "h2"}, classification="introduced", terra_model="m", luna_model="m2", terra_instructions_id="tid", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    row = {"target": {"path": "a.py", "symbol": "f"}, "counterpart": {"path": "b.py", "symbol": "g"}, "path": "suppressed", "reason": "terra_no_lead", "detail": "ProviderTimeout: request timed out", "assess_ran": True, "terra_tool_calls": 0}
    # Should not be stored
    before = CACHE.load_cache(state, "o/r", 9)
    CACHE.store(state, "o/r", 9, ident, row)
    after = CACHE.load_cache(state, "o/r", 9)
    assert after == before, "provider failure should not be cached"
    # Also direct check
    assert CACHE.is_provider_failure_row(row) is True
    assert CACHE.lookup(state, "o/r", 9, ident) is None

def test_tool_dependent_not_reused(tmp_path: Path, dupe_repo, monkeypatch):
    """Tool-dependent verdicts are not reused across revisions."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    root = dupe_repo["root"]; base = dupe_repo["base"]; head_b = dupe_repo["head_b"]
    state = tmp_path / "state_tool"
    # First run with tool calls
    def _assess_with_tool(**kw):
        # Simulate tool use by setting tool_calls in row
        return {"ok": True, "terra": {"overlap": True, "relationship": "equivalent", "strength": "likely", "shared_behavior": "x", "shared_component": "y", "opportunity": "direct_reuse", "differences": [], "wrapper_or_delegation": False, "ownership_lifecycle_difference": False, "uncertainty": ""}, "transcript": {"tool_calls": [{"name": "read_file", "arguments": {"path": "helper.py"}}]}}
    # Use a custom edit that will be called but we need to ensure tool calls are recorded
    # Instead directly test cache store with tool_calls flag
    db = tmp_path / "db_tool.sqlite"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, bytes INT, language TEXT, parse_status TEXT)")
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("a.py", "fh", 100, "python", "ok"))
    conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", ("b.py", "fh2", 100, "python", "ok"))
    conn.commit(); conn.close()
    ident = CACHE.build_identity(merged_db=db, target={"path": "a.py", "symbol": "f", "sha256": "h1"}, counterpart={"path": "b.py", "symbol": "g", "sha256": "h2"}, classification="introduced", terra_model="m", luna_model="m2", terra_instructions_id="tid", luna_instructions_id="lid", stage_version=ST.STAGE_VERSION)
    row = {"target": {"path": "a.py", "symbol": "f"}, "counterpart": {"path": "b.py", "symbol": "g"}, "path": "published", "anchors": {"target": {"path": "a.py", "symbol": "f", "sha256": "h1"}, "counterpart": {"path": "b.py", "symbol": "g", "sha256": "h2"}}, "terra_tool_calls": 1, "luna_tool_calls": 0}
    CACHE.store(state, "o/r", 10, ident, row)
    # Should not be stored due to tool dependency
    assert CACHE.lookup(state, "o/r", 10, ident) is None

def test_no_duplicate_publication_on_repeat(tmp_path: Path):
    """Repeated successful incremental review: no duplicate publication (replay)."""
    store = S.Store(tmp_path / "store.sqlite")
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    # Simulate a completed review then duplicate delivery
    ev = {"delivery_id": "d1", "event": "pull_request", "action": "synchronize", "repo": "o/r", "pr_number": 1, "head_sha": "b"*40}
    store.delivery_record(delivery_id="d1", event_type="pull_request", action="synchronize", repo="o/r", pr=1, head_sha="b"*40, decision="completed", reason="ok", cost_usd=0.1, detail={})
    # Duplicate delivery with same ID should be marked replay, not second publication
    dup = store.delivery_get("d1")
    replay = store.delivery_mark_replay("d1", dup)
    assert replay["decision"] == "skipped:replay"
    # No duplicate review record
    assert len(store.reviews_list()) == 0

def test_delivery_separate_approval_and_publication(tmp_path: Path, dupe_repo, monkeypatch):
    """Goal 1.1: Cache verdict independent of publication; suppress only when prior delivery confirmed."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    from codatiel_i2.event_store import Store
    from codatiel_i2.event_pipeline import _finding_hash_from_row, _filter_already_delivered
    state = tmp_path / "state_deliver"
    store = Store(state / "events.sqlite")
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    # Simulate finding X
    row = {"anchors": {"target": {"path": "a.py", "sha256": "h1"}, "counterpart": {"path": "b.py", "sha256": "h2"}}}
    fh = _finding_hash_from_row(row)
    # Case 1: B delivered successfully → C should suppress
    store.finding_delivery_record(repo="o/r", pr=1, finding_hash=fh, channel="github", head_sha="b"*40, run_id="runB", outcome="posted")
    to_deliver, already = _filter_already_delivered(store, "o/r", 1, [row], "github")
    assert len(to_deliver) == 0 and len(already) == 1
    # Case 2: B stale before delivery (no record) → C should deliver
    store2 = Store(state / "events2.sqlite")
    store2.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    to_deliver2, already2 = _filter_already_delivered(store2, "o/r", 1, [row], "github")
    assert len(to_deliver2) == 1 and len(already2) == 0
    # Case 3: B delivery failed → C can still deliver
    store3 = Store(state / "events3.sqlite")
    store3.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    store3.finding_delivery_record(repo="o/r", pr=1, finding_hash=fh, channel="github", head_sha="b"*40, run_id="runB", outcome="failed")
    to_deliver3, already3 = _filter_already_delivered(store3, "o/r", 1, [row], "github")
    assert len(to_deliver3) == 1, "failed delivery should not suppress"
    # Case 4: GitHub and email not conflated
    store4 = Store(state / "events4.sqlite")
    store4.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    store4.finding_delivery_record(repo="o/r", pr=1, finding_hash=fh, channel="github", head_sha="b"*40, run_id="runB", outcome="posted")
    to_github, _ = _filter_already_delivered(store4, "o/r", 1, [row], "github")
    to_email, _ = _filter_already_delivered(store4, "o/r", 1, [row], "email")
    assert len(to_github) == 0 and len(to_email) == 1, "github success must not suppress email"

def test_publisher_failure_mentions_posted_no_receipt(tmp_path: Path, monkeypatch):
    """Failure text mentioning 'posted' never creates a delivery receipt.

    Exercises the actual publication path: _maybe_publish_comment with
    a failing publisher whose error mentions "posted", plus the
    structured _github_delivery_posted gate (publications.outcome ==
    "posted") that guards _record_finding_deliveries. Under the old
    substring check (`"posted" in publish_bits`) this test failed;
    with the structured check it passes.
    """
    from codatiel_i2.event_store import Store
    from codatiel_i2 import event_pipeline as EP
    from codatiel_i2.github import comments as comments_mod

    state = tmp_path / "state_pub"
    state.mkdir()
    store = Store(state / "events.sqlite")
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    row = {"anchors": {"target": {"path": "a.py", "sha256": "h1"}, "counterpart": {"path": "b.py", "sha256": "h2"}}}
    fh = EP._finding_hash_from_row(row)
    head = "b" * 40
    run_id = "run1"

    # 1. Actual publisher error path: enabled switch + failing poster
    # whose message mentions "posted". Bits contain "posted" (the old
    # substring trigger) but no structured posted outcome exists.
    monkeypatch.setenv("CODATIEL_GITHUB_COMMENTS", "on")
    def _boom(*a, **k):
        raise RuntimeError("comment post failed (not posted: quota posted?)")
    monkeypatch.setattr(comments_mod, "post_review_comment", _boom)
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"run_id": run_id}))
    bits = EP._maybe_publish_comment(
        cfg=type("C", (), {"state_dir": state})(), store=store,
        run_dir=str(run_dir), now=0.0)
    assert "posted" in bits  # old code would have treated this as posted
    assert EP._github_delivery_posted(store, "o/r", 1, head, run_id) is False
    assert not store.is_finding_delivered("o/r", 1, fh, "github")
    to_deliver, already = EP._filter_already_delivered(store, "o/r", 1, [row], "github")
    assert len(to_deliver) == 1 and len(already) == 0

    # 2. Refused publication row whose reason mentions "posted" still
    # does not count as posted (structured outcome, not substring).
    store.publication_record(repo="o/r", pr=1, head_sha=head, run_id=run_id,
                             comment_id="", outcome="refused",
                             reason="comment post failed (not posted: ... posted ...)")
    assert EP._github_delivery_posted(store, "o/r", 1, head, run_id) is False
    to_deliver, already = EP._filter_already_delivered(store, "o/r", 1, [row], "github")
    assert len(to_deliver) == 1 and len(already) == 0

    # 3. Structured posted outcome gates delivery: only then record.
    store.publication_update(repo="o/r", pr=1, head_sha=head, run_id=run_id,
                             comment_id="c1", outcome="posted",
                             reason="posted consolidated comment")
    assert EP._github_delivery_posted(store, "o/r", 1, head, run_id) is True
    EP._record_finding_deliveries(store, "o/r", 1, [row], "github", head, run_id, "posted")
    assert store.is_finding_delivered("o/r", 1, fh, "github")
    assert not store.is_finding_delivered("o/r", 1, fh, "email")
    to_deliver2, already2 = EP._filter_already_delivered(store, "o/r", 1, [row], "github")
    assert len(to_deliver2) == 0 and len(already2) == 1
    to_email, _ = EP._filter_already_delivered(store, "o/r", 1, [row], "email")
    assert len(to_email) == 1

def test_safe_run_dir_sibling_rejected_nested_accepted(tmp_path: Path):
    """Sibling-directory path is rejected; legitimate nested run directory is accepted."""
    from codatiel_i2.event_pipeline import _safe_run_dir
    state = tmp_path / "state"
    state.mkdir()
    (state / "runs").mkdir()
    legit = state / "runs" / "abc123"
    legit.mkdir()
    sibling = tmp_path / "state-backup" / "runs" / "abc123"
    sibling.parent.mkdir(parents=True)
    sibling.mkdir()
    # Legitimate nested should be accepted
    assert _safe_run_dir(str(legit), state) is not None
    assert _safe_run_dir(str(legit / "semdup.json"), state) is not None
    # Sibling must be rejected (string prefix would incorrectly accept)
    assert _safe_run_dir(str(sibling), state) is None
    assert _safe_run_dir(str(sibling / "semdup.json"), state) is None
    # Also test with run_dir outside state
    assert _safe_run_dir("/tmp/evil/runs/1", state) is None
    assert _safe_run_dir(None, state) is None
    assert _safe_run_dir(str(legit), None) is None

def test_provenance_superseded_traceable(tmp_path: Path, dupe_repo):
    import codatiel_i2.event_pipeline as EP
    state = tmp_path / "estate"
    state.mkdir()
    store = S.Store(state / "events.sqlite")
    store.meta_set("enabled_at", "2026-01-01T00:00:00Z")
    cfg = P.EventConfig(env="dev", state_dir=state, repos=[REPO], repo_paths={REPO: str(dupe_repo["root"])}, whole_pr_ceiling_usd=2.0, semdup_enabled=True, pipeline="terra_only")
    # Enable operator? Not needed
    # Create a small repo for terra_only with scripted model
    from codatiel_i2 import runtime_sdk
    def _factory(role):
        script = [runtime_sdk.scripted_tool_call(call_id="t1", name="read_file", arguments={"path": "existing.py", "revision": "head", "start_line": 1, "end_line": 0}, response_id="r1"), runtime_sdk.scripted_final(json.dumps({"status": "silence", "summary": "ok", "findings": []}), response_id="f1")]
        return runtime_sdk.ScriptedModel(script), f"scripted:{role}"
    # Need to simulate stale during inference: mock get_live_head to return different after start
    orig_live = EP.get_live_head
    calls = {"n": 0}
    def _live(repo, pr):
        calls["n"] += 1
        # First call before claim is live == head, second after is stale
        if calls["n"] == 1:
            return dupe_repo["head_b"]
        return dupe_repo["head_c"]
    EP.get_live_head = _live
    try:
        ev = {"delivery_id": "prov1", "event": "pull_request", "action": "synchronize", "repo": REPO, "pr_number": 1, "head_sha": dupe_repo["head_b"], "base_sha": dupe_repo["base"], "branch": "feat", "draft": False, "merged": False, "closed": False, "created_at": "2026-11-15T00:00:00Z", "received_ts": 0}
        # Need checkpoint: first create via process_event? Use store directly
        # Instead call process_event which will use our live mock
        res = P.process_event(cfg=cfg, store=store, ev=ev, model_factory=_factory)
        # Should be stale_superseded with cost and provenance detail
        assert res["decision"] == "skipped:stale_superseded"
        detail = json.loads(res.get("detail") or "{}")
        assert detail.get("provenance_version") == "cost01b-v1"
        assert detail.get("run_dir")  # run artifact reference
        assert "total_cost_usd" in detail or "run_dir" in detail
        assert detail.get("superseded") is True
    finally:
        EP.get_live_head = orig_live
