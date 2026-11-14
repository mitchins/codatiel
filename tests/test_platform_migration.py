"""Slice 1 migration tests: v1 -> platform import preserves money/state.

Builds a realistic v1 store (enabled, grandfathered + reviewed +
draft + stray in_progress + queued delta + spend + posted/refused
publications), migrates, and asserts every preservation promise.
"""

from __future__ import annotations

import pytest

from codatiel_i2 import event_store as v1_mod
from codatiel_i2.platform import db as db_mod
from codatiel_i2.platform import migrate as migrate_mod
from codatiel_i2.platform import queue as q
from codatiel_i2.platform import schema as schema_mod
from codatiel_i2.platform import settings as settings_mod

OP_DOC = {
    "schema_version": "codatiel_i2.operator.v1",
    "monthly_budget_usd": 25.0,
    "daily_budget_usd": 5.0,
    "per_pr_breaker_usd": 2.0,
    "repos": {"o/r": True},
    "pause_reviews": False,
    "pause_github_comments": False,
    "email_enabled": True,
    "email_on_complete": True,
    "notify_to": ["op@example.com"],
    "notify_sender": "codatiel@example.com",
    "model_profile": "terra_luna_additive",
}


def _v1(path, spend=(6.5, 1.0)):
    store = v1_mod.Store(path)
    store.meta_set("enabled_at", "2026-11-14T00:00:00Z")
    store.meta_set("pipeline", "terra_luna_additive")
    store.spend_set("settled_usd", spend[0])
    store.spend_set("reserved_usd", spend[1])
    store.spend_set("unknown_holds", 0.0)
    store.checkpoint_upsert({"repo": "o/r", "pr": 1, "status": "reviewed",
                             "baseline_head": "b" * 40,
                             "last_reviewed_head": "h" * 40,
                             "last_reviewed_base": "b" * 40,
                             "last_cost_usd": 0.39})
    store.checkpoint_upsert({"repo": "o/r", "pr": 2, "status": "grandfathered",
                             "baseline_head": "g" * 40})
    store.checkpoint_upsert({"repo": "o/r", "pr": 3, "status": "draft",
                             "baseline_head": "d" * 40})
    store.checkpoint_upsert({"repo": "o/r", "pr": 4, "status": "in_progress",
                             "in_progress_head": "x" * 40,
                             "last_reviewed_head": "y" * 40})
    store.checkpoint_upsert({"repo": "o/r", "pr": 5, "status": "reviewed",
                             "last_reviewed_head": "q" * 40,
                             "queued_head": "w" * 40,
                             "queued_base": "q" * 40})
    store.publication_record(repo="o/r", pr=1, head_sha="h" * 40,
                             run_id="run1", comment_id="c100",
                             outcome="posted", reason="ok")
    store.publication_record(repo="o/r", pr=1, head_sha="h" * 40,
                             run_id="run0", comment_id="",
                             outcome="refused", reason="paused")
    store.close()
    return path


def test_migrate_preserves_spend_state_markers_watermark(tmp_path):
    v1_path = _v1(tmp_path / "v1.sqlite")
    conn = db_mod.connect(tmp_path / "p.sqlite")
    summary = migrate_mod.migrate(
        conn, v1_db_path=str(v1_path), operator_doc=dict(OP_DOC),
        budgets_usd={"cycle": 25.0, "day": 5.0, "project": 50.0},
        actor="test", now=2000.0)
    assert summary["settled_micro"] == 6_500_000
    assert summary["reserved_micro"] == 1_000_000
    assert summary["targets"] == 5
    assert summary["stranded"] == ["o/r#4"]
    assert summary["publication_markers"] == 1
    # Spend: settled + outstanding reservation both block headroom
    # on lifetime scopes; the rolling day scope carries only the still-
    # open hold (settled history belongs to past days).
    assert q.exposure(conn, "cycle") == 7_500_000
    assert q.exposure(conn, "project") == 7_500_000
    assert q.exposure(conn, "day") == 1_000_000
    # Reviewed target carries its checkpoint; nothing is live-verified.
    tgt = conn.execute("SELECT * FROM pl_pr_targets WHERE pr=1").fetchone()
    assert tgt["reviewed_head"] == "h" * 40
    assert tgt["live_head"] == "" and tgt["target_generation"] == 1
    # Queued delta preserved as an unverified note, not a live fact.
    ev = conn.execute("SELECT detail FROM pl_events WHERE kind="
                      "'import_target' AND ref='o/r#5'").fetchone()
    assert "w" * 12 in ev[0][:400] or "w" * 40 in ev[0]
    # Posted comment marker preserved for the new publisher.
    mark = conn.execute(
        "SELECT * FROM pl_publications_imported").fetchone()
    assert (mark["head_sha"], mark["comment_id"]) == ("h" * 40, "c100")
    # Watermark + one settings revision + journal.
    assert conn.execute("SELECT watermark FROM pl_recon WHERE kind="
                        "'activation_watermark'").fetchone()[0] == \
        "2026-11-14T00:00:00Z"
    rev = settings_mod.current_revision(conn)
    assert rev["revision"] == 1 and rev["actor"] == "test"
    assert conn.execute(
        "SELECT COUNT(*) FROM pl_events WHERE kind='import_complete'"
    ).fetchone()[0] == 1
    q.assert_invariants(conn)
    conn.close()


def test_migrate_refuses_double_neverenabled_and_bad_doc(tmp_path):
    v1_path = _v1(tmp_path / "v1.sqlite")
    conn = db_mod.connect(tmp_path / "p.sqlite")
    migrate_mod.migrate(conn, v1_db_path=str(v1_path),
                        operator_doc=dict(OP_DOC),
                        budgets_usd={"cycle": 25.0}, now=2000.0)
    with pytest.raises(RuntimeError):
        migrate_mod.migrate(conn, v1_db_path=str(v1_path),
                            operator_doc=dict(OP_DOC),
                            budgets_usd={"cycle": 25.0}, now=2001.0)
    empty = v1_mod.Store(tmp_path / "empty.sqlite")
    empty.close()
    conn2 = db_mod.connect(tmp_path / "p2.sqlite")
    with pytest.raises(RuntimeError):
        migrate_mod.migrate(conn2, v1_db_path=str(tmp_path / "empty.sqlite"),
                            operator_doc=dict(OP_DOC),
                            budgets_usd={"cycle": 25.0}, now=2000.0)
    conn3 = db_mod.connect(tmp_path / "p3.sqlite")
    bad = dict(OP_DOC, pause_reviews="eventually")
    with pytest.raises(ValueError):
        migrate_mod.migrate(conn3, v1_db_path=str(v1_path),
                            operator_doc=bad,
                            budgets_usd={"cycle": 25.0}, now=2000.0)
    # Failed import left no partial revision behind.
    assert settings_mod.current_revision(conn3) is None
    conn.close(); conn2.close(); conn3.close()


def test_settings_import_once_update_and_resolve(tmp_path):
    conn = db_mod.connect(tmp_path / "p.sqlite")
    schema_mod.ensure(conn)
    rev1 = settings_mod.import_operator_json(conn, dict(OP_DOC),
                                             actor="op", now=2000.0)
    assert rev1 == 1
    with pytest.raises(RuntimeError):
        settings_mod.import_operator_json(conn, dict(OP_DOC), now=2001.0)
    with pytest.raises(ValueError):
        settings_mod.update(conn, {"nope": 1})
    with pytest.raises(ValueError):
        settings_mod.update(conn, {"monthly_budget_usd": -5})
    rev2 = settings_mod.update(conn, {"pause_reviews": True}, actor="op",
                               now=2002.0)
    assert rev2 == 2
    cur = settings_mod.current_revision(conn)
    assert cur["doc"]["pause_reviews"] is True
    audit = conn.execute("SELECT action,actor FROM pl_config_audit "
                         "ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in audit] == [("import_operator_json", "op"),
                                            ("update", "op")]
    pol = settings_mod.resolve_policy(
        cur["doc"], env_monthly_usd=25.0, env_per_pr_usd=2.0,
        env_repos=["o/r", "o/other"])
    # Stale file values narrow, never raise; repos intersect env.
    assert pol["monthly_effective_usd"] == 25.0
    assert pol["per_pr_effective_usd"] == 2.0
    assert pol["repos"] == ["o/r"]
    assert pol["pause_reviews"] is True
    assert pol["pause_publication"] is False  # legacy comment key carried
    conn.close()


def test_ensure_fails_closed_on_unconstrainable_legacy_db(tmp_path):
    import sqlite3

    from codatiel_i2.platform import schema as schema_mod
    # Simulate a mid-slice-1 experimental database: complete tables
    # but the three-column job uniqueness (no rerun_generation) and
    # none of the later target columns.
    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE pl_pr_targets(repo TEXT NOT NULL, pr INTEGER NOT NULL,"
        " repo_id INTEGER, app TEXT NOT NULL DEFAULT '',"
        " lifecycle TEXT NOT NULL DEFAULT 'open',"
        " lifecycle_version INTEGER NOT NULL DEFAULT 0,"
        " target_generation INTEGER NOT NULL DEFAULT 0,"
        " dirty_rev INTEGER NOT NULL DEFAULT 0,"
        " live_head TEXT NOT NULL DEFAULT '',"
        " live_observed REAL NOT NULL DEFAULT 0,"
        " reviewed_head TEXT NOT NULL DEFAULT '',"
        " reviewed_base TEXT NOT NULL DEFAULT '',"
        " recon_owner TEXT NOT NULL DEFAULT '',"
        " recon_fence INTEGER NOT NULL DEFAULT 0,"
        " recon_expires REAL NOT NULL DEFAULT 0,"
        " updated_ts REAL NOT NULL DEFAULT 0,"
        " PRIMARY KEY(repo,pr))")
    conn.execute(
        "CREATE TABLE pl_jobs(id INTEGER PRIMARY KEY, repo TEXT NOT NULL,"
        " pr INTEGER NOT NULL, target_generation INTEGER NOT NULL,"
        " mode TEXT NOT NULL DEFAULT 'incremental',"
        " full_base TEXT NOT NULL DEFAULT '',"
        " full_head TEXT NOT NULL DEFAULT '',"
        " diff_hash TEXT NOT NULL DEFAULT '',"
        " fingerprint TEXT NOT NULL DEFAULT '',"
        " state TEXT NOT NULL DEFAULT 'pending',"
        " owner TEXT NOT NULL DEFAULT '', fence INTEGER NOT NULL DEFAULT 0,"
        " lease_expires REAL NOT NULL DEFAULT 0, attempt INTEGER,"
        " created_ts REAL NOT NULL DEFAULT 0,"
        " UNIQUE(repo,pr,target_generation))")
    conn.commit()
    conn.close()
    from codatiel_i2.platform import db as db_mod
    conn = db_mod.connect(db_path)
    conn.execute(
        "INSERT INTO pl_pr_targets(repo,pr,lifecycle,target_generation,"
        "dirty_rev,reviewed_head) VALUES('o/r',1,'open',1,1,'h')")
    conn.execute(
        "INSERT INTO pl_jobs(repo,pr,target_generation,mode,full_head,"
        "state) VALUES('o/r',1,1,'incremental','h','pending')")
    conn.commit()
    try:
        # Columns upgrade in place, but the missing four-column job
        # uniqueness cannot be added: fail closed with a
        # NON-destructive diagnostic — preserve, back up, separate
        # destination; never delete history.
        with pytest.raises(RuntimeError, match="preserve the original"):
            schema_mod.ensure(conn)
        try:
            schema_mod.ensure(conn)
        except RuntimeError as exc:
            message = str(exc)
        assert "backup" in message.lower()
        assert "separate" in message.lower()
        assert "delete" not in message.lower()
        # Legacy records survive the refused upgrade untouched.
        assert conn.execute(
            "SELECT reviewed_head FROM pl_pr_targets").fetchone()[0] == "h"
        assert conn.execute(
            "SELECT mode FROM pl_jobs").fetchone()[0] == "incremental"
    finally:
        conn.close()


def test_ensure_version_mismatch_fails_closed_nondestructively(tmp_path):
    import sqlite3

    from codatiel_i2.platform import db as db_mod
    from codatiel_i2.platform import schema as schema_mod
    db_path = tmp_path / "p.sqlite"
    conn = db_mod.connect(db_path)
    schema_mod.ensure(conn)
    conn.execute("INSERT INTO pl_pr_targets(repo,pr,lifecycle) "
                 "VALUES('o/r',1,'open')")
    conn.execute("UPDATE meta SET value='codatiel_i2.platform.vX' "
                 "WHERE key='platform_schema_version'")
    conn.commit()
    conn.close()
    conn = db_mod.connect(db_path)
    try:
        with pytest.raises(RuntimeError, match="preserve the original"):
            schema_mod.ensure(conn)
        # The versioned data is untouched by the refused open.
        assert conn.execute("SELECT COUNT(*) FROM pl_pr_targets"
                            ).fetchone()[0] == 1
    finally:
        conn.close()


def test_ensure_incompatible_preserves_legacy_v1_tables(tmp_path):
    """Both incompatible-schema paths must leave co-located v1
    deliveries/checkpoints/reviews/publications/spend rows intact.
    The database file holds platform tables alongside the historical
    v1 tables; a rejected upgrade must not delete or overwrite them."""
    import sqlite3

    from codatiel_i2 import event_store as v1_mod
    from codatiel_i2.platform import db as db_mod
    from codatiel_i2.platform import schema as schema_mod

    # --- path A: missing four-column uniqueness ---
    db_path_a = tmp_path / "a.sqlite"
    # Create a v1 store first (writes deliveries etc.)
    v1 = v1_mod.Store(db_path_a)
    v1.meta_set("enabled_at", "2026-11-14T00:00:00Z")
    v1.delivery_record(
        delivery_id="d1", event_type="pull_request", action="opened",
        repo="o/r", pr=1, head_sha="a" * 40, decision="completed",
        reason="ok", cost_usd=0.99, detail={}, now=1000.0)
    v1.checkpoint_upsert({"repo": "o/r", "pr": 1, "status": "reviewed",
                          "last_reviewed_head": "a" * 40})
    v1.review_record(
        repo="o/r", pr=1, kind="full", base_sha="b" * 40, head_sha="a" * 40,
        diff_sha="d", files=1, pipeline="terra_luna_additive",
        status="complete", terra_findings=1, luna_added=0,
        cost_usd=0.11, cost_unknown=False, run_dir="/r", now=1000.0)
    v1.publication_record(repo="o/r", pr=1, head_sha="a" * 40,
                          run_id="run1", comment_id="c1",
                          outcome="posted", reason="ok")
    v1.spend_set("settled_usd", 1.23)
    v1.close()
    # Now corrupt platform side: legacy three-column unique
    conn = sqlite3.connect(db_path_a)
    conn.row_factory = sqlite3.Row
    # Add legacy platform tables with wrong uniqueness (if not already)
    # The v1 Store already created meta/deliveries/checkpoints etc.;
    # we add the legacy platform tables on top.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pl_pr_targets(repo TEXT NOT NULL, pr INTEGER NOT NULL,"
        " lifecycle TEXT NOT NULL DEFAULT 'open', PRIMARY KEY(repo,pr))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pl_jobs(id INTEGER PRIMARY KEY, repo TEXT NOT NULL,"
        " pr INTEGER NOT NULL, target_generation INTEGER NOT NULL,"
        " UNIQUE(repo,pr,target_generation))")
    conn.commit()
    conn.close()
    conn = db_mod.connect(db_path_a)
    try:
        with pytest.raises(RuntimeError, match="preserve the original") as exc:
            schema_mod.ensure(conn)
        msg = str(exc.value).lower()
        assert "backup" in msg and ".backup main" in str(exc.value)
        assert "separate" in msg and "delete" not in msg
        # Legacy v1 rows survive untouched
        assert conn.execute("SELECT decision FROM deliveries WHERE delivery_id='d1'"
                            ).fetchone()[0] == "completed"
        assert conn.execute("SELECT status FROM checkpoints WHERE pr=1"
                            ).fetchone()[0] == "reviewed"
        assert conn.execute("SELECT cost_usd FROM reviews WHERE pr=1"
                            ).fetchone()[0] == 0.11
        assert conn.execute("SELECT outcome FROM publications WHERE run_id='run1'"
                            ).fetchone()[0] == "posted"
        assert conn.execute("SELECT value FROM spend WHERE key='settled_usd'"
                            ).fetchone()[0] == 1.23
    finally:
        conn.close()

    # --- path B: version mismatch with co-located v1 history ---
    db_path_b = tmp_path / "b.sqlite"
    v1b = v1_mod.Store(db_path_b)
    v1b.meta_set("enabled_at", "2026-11-14T00:00:00Z")
    v1b.delivery_record(
        delivery_id="d2", event_type="pull_request", action="opened",
        repo="o/r", pr=2, head_sha="b" * 40, decision="completed",
        reason="ok", cost_usd=0.77, detail={}, now=1000.0)
    v1b.checkpoint_upsert({"repo": "o/r", "pr": 2, "status": "reviewed",
                           "last_reviewed_head": "b" * 40})
    v1b.close()
    conn2 = db_mod.connect(db_path_b)
    # First ensure creates correct platform tables, then corrupt version
    schema_mod.ensure(conn2)
    conn2.execute("UPDATE meta SET value='codatiel_i2.platform.vX' "
                  "WHERE key='platform_schema_version'")
    conn2.commit()
    conn2.close()
    conn2 = db_mod.connect(db_path_b)
    try:
        with pytest.raises(RuntimeError, match="preserve the original"):
            schema_mod.ensure(conn2)
        assert conn2.execute("SELECT decision FROM deliveries WHERE delivery_id='d2'"
                             ).fetchone()[0] == "completed"
        assert conn2.execute("SELECT status FROM checkpoints WHERE pr=2"
                             ).fetchone()[0] == "reviewed"
    finally:
        conn2.close()
