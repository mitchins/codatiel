"""Slice 1 recovery tests: real subprocess crashes, thread races.

No mocks for the crash paths: subprocesses die via os._exit / SIGKILL
around durable commits and the reopened database must show all-or-
nothing. Concurrency uses real threads with separate SQLite
connections and a start barrier.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from codatiel_i2.platform import artifacts as art_mod
from codatiel_i2.platform import db as db_mod
from codatiel_i2.platform import queue as q
from codatiel_i2.platform import schema as schema_mod

REPO = "o/r"
REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _open(path, busy=30.0):
    conn = db_mod.connect(path, busy_timeout_s=busy)
    schema_mod.ensure(conn)
    return conn


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _run_script(path: Path, body: str, timeout_s: int = 120) -> int:
    script = path / "crash_case.py"
    script.write_text(body, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)],
                          capture_output=True, text=True, timeout=timeout_s,
                          cwd=REPO_ROOT, env=_env())
    return proc.returncode


def test_crash_before_and_after_inbox_commit(tmp_path):
    db_path = tmp_path / "q.sqlite"
    conn = _open(db_path)
    conn.close()
    # Crash before any commit: reopen finds no receipt, re-accept works.
    rc = _run_script(tmp_path, f"""
import os
from codatiel_i2.platform import db as db_mod, queue as q, schema as s
conn = db_mod.connect({str(db_path)!r}); s.ensure(conn)
conn.execute("SELECT 1").fetchone()
os._exit(81)
""")
    assert rc == 81
    conn = _open(db_path)
    assert conn.execute("SELECT COUNT(*) FROM pl_inbox").fetchone()[0] == 0
    assert q.accept(conn, app="gh", guid="d1", body=b"{}",
                    repo=REPO, repo_id=1, pr=1, head="h" * 40,
                    now=1000.0) == "accepted"
    conn.close()
    # Crash after commit: reopen finds the durable receipt; re-accept
    # is a duplicate, never new work.
    rc = _run_script(tmp_path, f"""
import os
from codatiel_i2.platform import db as db_mod, queue as q, schema as s
conn = db_mod.connect({str(db_path)!r}); s.ensure(conn)
assert q.accept(conn, app="gh", guid="d1", body=b"{{}}", repo={REPO!r},
                repo_id=1, pr=1, head={"h" * 40!r}, now=1000.0) == "duplicate"
conn.close()
os._exit(82)
""")
    assert rc == 82
    conn = _open(db_path)
    assert conn.execute("SELECT COUNT(*) FROM pl_inbox").fetchone()[0] == 1
    assert q.accept(conn, app="gh", guid="d1", body=b"{}", repo=REPO,
                    repo_id=1, pr=1, head="h" * 40,
                    now=1001.0) == "duplicate"
    q.assert_invariants(conn)
    conn.close()


def test_crash_during_completion_is_all_or_nothing(tmp_path):
    db_path = tmp_path / "q.sqlite"
    marker = tmp_path / "in-complete"
    conn = _open(db_path)
    q.set_budget(conn, "cycle", 10.0, now=1000.0)
    q.signal(conn, repo=REPO, pr=1, head="a" * 40, now=1000.0)
    cl = q.reconcile_claim(conn, repo=REPO, pr=1, owner="r", now=1000.0)
    assert q.reconcile_commit(conn, repo=REPO, pr=1, owner="r",
                              fence=cl["fence"],
                              generation=cl["generation"],
                              dirty=cl["dirty"], head="a" * 40, now=1000.0)
    q.accept(conn, app="gh", guid="g1", body=b"1", repo=REPO,
             repo_id=1, pr=1, head="a" * 40, now=1000.0)
    assert q.route(conn, app="gh", guid="g1", now=1000.0) == "routed"
    claim = q.claim(conn, owner="w1", reserve_usd=1.0, now=1000.0)
    assert q.dispatch_intent(conn, claim, stage="terra", call_id="c1",
                             now=1000.0)
    conn.close()
    script = tmp_path / "die_in_complete.py"
    script.write_text(f"""
import time
from pathlib import Path
from codatiel_i2.platform import db as db_mod, queue as q
orig = q.event
def slow(*a, **k):
    if k.get("kind") == "complete" or (a and a[1] == "complete"):
        Path({str(marker)!r}).touch()
        time.sleep(30)
    return orig(*a, **k)
q.event = slow
conn = db_mod.connect({str(db_path)!r})
claim = q.Claim({claim.job}, {claim.attempt}, "w1", {claim.fence})
q.complete(conn, claim, actual_usd=0.5, run_path="/r/1",
           manifest_sha="m", digest="d",
           effects=[{{"channel": "comment"}}], now=1000.0)
""", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)],
                            cwd=REPO_ROOT,
                            env=_env())
    try:
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), "worker never entered completion"
        time.sleep(0.3)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    conn = _open(db_path)
    # Killed mid-transaction: no partial result, no effects, no
    # checkpoint advance, no settlement; the job is still running with
    # its lease (a later recover, not the crash, decides its fate).
    assert conn.execute("SELECT COUNT(*) FROM pl_results").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pl_effects").fetchone()[0] == 0
    assert conn.execute("SELECT reviewed_head FROM pl_pr_targets").fetchone()[0] == ""
    assert q.exposure(conn, "cycle") == 1_000_000  # reservation, unsettled
    assert conn.execute("SELECT state FROM pl_jobs").fetchone()[0] == "running"
    assert conn.execute(
        "SELECT COUNT(*) FROM pl_events WHERE kind='complete'").fetchone()[0] == 0
    q.assert_invariants(conn)
    conn.close()


def test_concurrent_duplicate_receipts_one_inbox(tmp_path):
    db_path = tmp_path / "q.sqlite"
    conn = _open(db_path)
    conn.close()
    barrier = threading.Barrier(8)
    errors: list = []

    def worker(_):
        local_errors: list = []
        try:
            c = _open(db_path, busy=10.0)
            barrier.wait(timeout=30)
            for _ in range(25):
                c_out = q.accept(c, app="gh", guid="same", body=b"{}",
                                 repo=REPO, repo_id=1, pr=1,
                                 head="h" * 40)
                if c_out not in ("accepted", "duplicate"):
                    local_errors.append(
                        AssertionError(f"bad outcome {c_out!r}"))
            c.close()
        except Exception as exc:  # noqa: BLE001 - thread error collection
            local_errors.append(exc)
        errors.extend(local_errors)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors
    conn = _open(db_path)
    assert conn.execute("SELECT COUNT(*) FROM pl_inbox").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM pl_receipt_attempts").fetchone()[0] == 200
    q.assert_invariants(conn)
    conn.close()


def test_concurrent_budget_race_exactly_capped(tmp_path):
    db_path = tmp_path / "q.sqlite"
    conn = _open(db_path)
    q.set_budget(conn, "cycle", 35.0, now=1000.0)
    for pr in range(16):
        head = f"{pr:040d}"
        q.signal(conn, repo=REPO, pr=pr, head=head, now=1000.0)
        cl = q.reconcile_claim(conn, repo=REPO, pr=pr, owner="r",
                               now=1000.0)
        assert q.reconcile_commit(conn, repo=REPO, pr=pr, owner="r",
                                  fence=cl["fence"],
                                  generation=cl["generation"],
                                  dirty=cl["dirty"], head=head, now=1000.0)
        q.accept(conn, app="gh", guid=f"g{pr}", body=b"x", repo=REPO,
                 repo_id=1, pr=pr, head=head, now=1000.0)
        assert q.route(conn, app="gh", guid=f"g{pr}", now=1000.0) == "routed"
    conn.close()
    barrier = threading.Barrier(16)
    admitted: list = []
    errors: list = []

    def worker(i):
        try:
            c = _open(db_path, busy=10.0)
            barrier.wait(timeout=30)
            claim = q.claim(c, owner=f"w{i}", reserve_usd=7.0,
                            scopes=("cycle",), now=1000.0)
            if claim is not None:
                admitted.append(claim.job)
            c.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors
    # Exactly five 7-unit reservations fit the 35-unit cap.
    assert len(admitted) == 5
    conn = _open(db_path)
    assert q.exposure(conn, "cycle") == 35_000_000
    q.assert_invariants(conn)
    conn.close()


def test_artifact_stage_commit_validate_and_quarantine(tmp_path):
    staging = tmp_path / "staging"
    runs = tmp_path / "runs"
    staging.mkdir()
    files = {"review.json": b'{"ok": true}', "evidence.txt": b"abc"}
    st = art_mod.stage(staging, "run1", files)
    assert art_mod.validate(st)
    dest = art_mod.commit(st, runs)
    assert art_mod.validate(dest)
    sha = art_mod.manifest_sha(dest)
    assert isinstance(sha, str) and len(sha) == 64
    # Tamper: validation fails, never trusted.
    (dest / "evidence.txt").write_bytes(b"tampered")
    assert not art_mod.validate(dest)
    # Incomplete staging (crashed mid-write, no manifest): quarantine.
    partial = staging / "run2.staging"
    partial.mkdir()
    (partial / "review.json").write_bytes(b"{}")
    out = art_mod.recover_runs(staging, runs)
    assert "run2.staging" in out["quarantine"]
    assert str(dest) in out["quarantine"]  # tampered run flagged too
    assert out["complete_staging"] == []
    # A clean complete staging is reported as evidence, not trusted.
    st3 = art_mod.stage(staging, "run3", files)
    out = art_mod.recover_runs(staging, runs)
    assert st3.name in out["complete_staging"]


def test_artifact_malformed_manifests_quarantined_selectively(tmp_path):
    import hashlib
    import json

    staging = tmp_path / "staging"
    runs = tmp_path / "runs"
    staging.mkdir()
    good_files = {"review.json": b'{"ok": true}'}
    good = art_mod.stage(staging, "good", good_files)
    good_dest = art_mod.commit(good, runs)
    assert art_mod.validate(good_dest)

    def write_run(name, manifest_text, extra=()):
        d = runs / name
        d.mkdir()
        (d / art_mod.MANIFEST_NAME).write_text(manifest_text,
                                               encoding="utf-8")
        for fname, content in extra:
            (d / fname).write_bytes(content)
        return d

    digest = hashlib.sha256(good_files["review.json"]).hexdigest()
    bad_dirs = {
        "null": write_run("r-null", "null"),
        "list": write_run("r-list", '["files"]'),
        "string": write_run("r-string", '"manifest"'),
        "no_files": write_run("r-nofiles", '{"run_id": "x"}'),
        "files_list": write_run("r-fileslist", '{"files": ["a"]}'),
        "files_null": write_run("r-filesnull", '{"files": null}'),
        "hash_number": write_run(
            "r-hashnum", json.dumps({"files": {"review.json": 42}}),
            [("review.json", good_files["review.json"])]),
        "hash_null": write_run(
            "r-hashnull", json.dumps({"files": {"review.json": None}}),
            [("review.json", good_files["review.json"])]),
        "not_json": write_run("r-nojson", "{oops"),
    }
    for name, path in bad_dirs.items():
        assert not art_mod.validate(path), name
    # manifest_sha on a malformed run refuses loudly (no silent hash).
    with pytest.raises(RuntimeError):
        art_mod.manifest_sha(runs / "r-nojson")
    # Recovery classifies each directory independently: one bad apple
    # never aborts the rest, and the good run is untouched.
    out = art_mod.recover_runs(staging, runs)
    assert out["complete_staging"] == []
    assert str(good_dest) not in out["quarantine"]
    assert sorted(out["quarantine"]) == sorted(
        str(runs / n) for n in bad_dirs.values())
