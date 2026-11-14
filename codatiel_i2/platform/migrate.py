"""Cutover import: v1 event store -> platform tables (additive, once).

An explicit, operator-run step of a future cutover — not an automatic
migration. Only run against a quiesced worker with a verified backup
of the original database in hand; the platform never modifies the v1
tables and never overwrites the source file.

Preserves spend (settled + outstanding reservations as conservative
opening balances that keep blocking headroom, plus a bounded carry
per unreconciled unknown hold into the rolling day scope), reviewed/
queued PR state (unverified targets the slow sweep re-verifies;
queued deltas become pending synthetic receipts that route through
the normal gates once live confirms them), posted GitHub comment
markers (newest per head, so the new publisher reconciles instead of
double-posting), and the activation watermark. v1 tables are never
altered. History (deliveries, reviews rows) stays readable in place;
the platform journal records exactly what was imported.
"""

from __future__ import annotations

import sqlite3
import time

from codatiel_i2.platform import money as money_mod
from codatiel_i2.platform import queue as queue_mod
from codatiel_i2.platform import schema as schema_mod
from codatiel_i2.platform import settings as settings_mod


def migrate(db: sqlite3.Connection, *, v1_db_path: str,
            operator_doc: dict | None,
            budgets_usd: dict[str, float],
            unknown_hold_usd: float = 1.0,
            actor: str = "migration",
            now: float | None = None) -> dict:
    """Run the one-time import. Returns a summary; raises when unsafe.

    Refuses when platform tables already hold imported state (never
    double-import) or when the v1 store was never enabled.
    unknown_hold_usd bounds each unreconciled v1 unknown hold carried
    into the rolling day scope (default: one full review reservation).
    """
    from codatiel_i2 import event_store as v1_mod
    from codatiel_i2 import operator_config as op_mod
    ts = time.time() if now is None else now
    schema_mod.ensure(db)
    # Read v1 outside the write transaction (short reads, no network).
    v1 = v1_mod.Store(v1_db_path)
    try:
        enabled_at = v1.meta_get("enabled_at")
        spend = v1.spend_totals()
        checkpoints = v1.list_checkpoints()
        publications = v1.publications_list()
    finally:
        v1.close()
    if not enabled_at:
        raise RuntimeError("v1 store never enabled; nothing to "
                           "import (enable the platform directly)")
    # One atomic import transaction: raw statements only, no nested
    # tx() calls inside.
    db.execute("BEGIN IMMEDIATE")
    try:
        already = db.execute(
            "SELECT COUNT(*) FROM pl_config_revisions").fetchone()[0]
        if already:
            raise RuntimeError("platform settings already imported; "
                               "refusing second import")
        db.execute("INSERT INTO pl_recon(kind,watermark,updated_ts) "
                   "VALUES('activation_watermark',?,?)",
                   (enabled_at, ts))
        for name, cap_usd in budgets_usd.items():
            db.execute("INSERT INTO pl_budgets(name,cap_micro) VALUES(?,?)",
                       (name, money_mod.to_micro(cap_usd)))
        # Spend: settled exactly; outstanding reservations convert to a
        # conservative opening hold (still blocks headroom, audit says
        # why). The rolling day scope gets the *reserved* component plus
        # a bounded carry per unreconciled unknown hold: settled spend
        # belongs to past days, but still-open holds (whose money v1
        # folded into settled) must keep reducing headroom across the
        # cutover boundary until reconciled.
        settled = money_mod.to_micro(float(spend.get("settled_usd") or 0.0))
        reserved = money_mod.to_micro(float(spend.get("reserved_usd") or 0.0))
        unknown = int(spend.get("unknown_holds") or 0)
        hold_each = money_mod.to_micro(unknown_hold_usd)
        day_opening = reserved + unknown * hold_each
        for scope in budgets_usd:
            opening = day_opening if scope in ("day", "daily") else (
                settled + reserved)
            db.execute("INSERT INTO pl_openings(scope,micro,note) "
                       "VALUES(?,?,?)",
                       (scope, opening,
                        f"v1 import: settled {settled} + outstanding "
                        f"reservation {reserved} micro-USD, "
                        f"{unknown} unknown holds"))
        doc = dict(operator_doc or {})
        errors = op_mod.validate_doc(doc)
        if errors:
            raise ValueError(f"operator.json invalid: {errors[:3]}")
        settings_mod._append(db, doc, actor, "import_operator_json",
                             {"schema": op_mod.SCHEMA_VERSION}, ts)
        imported_targets = 0
        stranded = []
        queued_deltas: list[tuple[str, int, str, str]] = []
        for cp in checkpoints:
            status = str(cp.get("status") or "")
            note_bits = [f"v1 status {status!r}"]
            if cp.get("note"):
                note_bits.append(str(cp["note"])[:200])
            quarantined = 0
            quarantine_note = ""
            lifecycle = "draft" if status == "draft" else "open"
            if status in ("in_progress", "interrupted"):
                # Crashed or interrupted in-flight work is quarantined:
                # no automatic paid re-review — the sweep may verify
                # live and routing may park a pending job, but no claim
                # admits until an explicit, audited release.
                quarantined = 1
                quarantine_note = ("cutover with no owner: crashed or "
                                   "interrupted in-flight review; explicit "
                                   "release required, never auto-respend")
                note_bits.append(quarantine_note)
                stranded.append(f"{cp['repo']}#{cp['pr']}")
            reviewed = cp.get("last_reviewed_head") or ""
            base = (cp.get("last_reviewed_base")
                    or cp.get("baseline_head") or "")
            if not reviewed and status == "grandfathered":
                # Grandfathered PRs were deliberately never reviewed;
                # anchor the comparison at the enablement HEAD (v1
                # stores it as baseline_head) so the first
                # post-cutover receipt at that head is
                # already-reviewed instead of a paid empty self-review.
                # The journal records that this anchor is
                # grandfathered history, not a completed review.
                reviewed = cp.get("baseline_head") or ""
                if reviewed:
                    note_bits.append("grandfathered anchor: no historical "
                                     "review, enablement-head comparison "
                                     "preserved")
            db.execute(
                "INSERT INTO pl_pr_targets(repo,pr,lifecycle,"
                "target_generation,dirty_rev,reviewed_head,reviewed_base,"
                "last_signal_head,quarantined,quarantine_note,updated_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(repo,pr) DO UPDATE SET "
                "quarantined=excluded.quarantined,"
                "quarantine_note=excluded.quarantine_note,"
                "updated_ts=excluded.updated_ts "
                "WHERE excluded.quarantined=1 "
                "AND pl_pr_targets.quarantined=0",
                (cp["repo"], cp["pr"], lifecycle,
                 1 if status in ("reviewed", "grandfathered") else 0,
                 1, reviewed, base,
                 cp.get("last_reviewed_head")
                 or cp.get("in_progress_head")
                 or cp.get("baseline_head") or "",
                 quarantined, quarantine_note, ts))
            queue_mod.event(db, "import_target",
                            f"{cp['repo']}#{cp['pr']}", now=ts,
                            note="; ".join(note_bits),
                            queued_head=cp.get("queued_head") or "")
            imported_targets += 1
            queued = cp.get("queued_head") or ""
            if queued and queued != (cp.get("last_reviewed_head") or ""):
                queued_deltas.append((cp["repo"], cp["pr"], queued,
                                      lifecycle))
        imported_markers = 0
        for pub in reversed(publications):
            # Newest first: ON CONFLICT keeps the newest posted marker
            # per head, so the new publisher reconciles against the
            # comment that is actually live (never a deleted older one).
            if pub.get("outcome") != "posted" or not pub.get("comment_id"):
                continue
            db.execute("INSERT INTO pl_publications_imported(repo,pr,"
                       "head_sha,run_id,comment_id,outcome,reason,"
                       "created_ts) VALUES(?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(repo,pr,head_sha) DO NOTHING",
                       (pub["repo"], pub["pr"], pub["head_sha"],
                        pub.get("run_id") or "", pub["comment_id"],
                        pub["outcome"], pub.get("reason") or "",
                        pub.get("created_ts") or 0))
            imported_markers += 1
        summary = {"enabled_at": enabled_at,
                   "settled_micro": settled, "reserved_micro": reserved,
                   "unknown_holds": unknown,
                   "unknown_hold_usd": unknown_hold_usd,
                   "day_opening_micro": day_opening,
                   "synthetic_queued_receipts": len(queued_deltas),
                   "targets": imported_targets, "stranded": stranded,
                   "publication_markers": imported_markers,
                   "budgets": sorted(budgets_usd)}
        queue_mod.event(db, "import_complete", "v1", now=ts, **summary)
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise
    # A queued delta that never got its run becomes a pending
    # synthetic receipt, announced but unverified: the slow sweep
    # verifies live, the worker re-routes, and the delta reviews
    # through the normal gates — never a bulk backfill, never silent.
    # The receipt carries the checkpoint's effective lifecycle: draft
    # deltas wait for readiness, quarantined ones wait for release.
    # Post-commit and individually transactional: a crash here loses
    # nothing the sweep cannot re-derive (live != reviewed_head).
    for repo, pr, queued, qlifecycle in queued_deltas:
        queue_mod.accept(db, app="migration",
                         guid=f"migrate:{repo}:{pr}:{queued}",
                         body=f"migrate:{repo}:{pr}:{queued}".encode(),
                         repo=repo, repo_id=None, pr=pr, head=queued,
                         lifecycle=qlifecycle, now=ts)
        queue_mod.signal(db, repo=repo, pr=pr, head=queued,
                         lifecycle=qlifecycle, now=ts)
    return summary
