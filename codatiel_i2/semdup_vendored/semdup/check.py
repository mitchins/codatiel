"""Four-way incremental check: base vs head as distinct snapshots.

Classifies candidate families on head as introduced / worsened /
pre-existing / removed; the semantic pipeline (assess -> challenge ->
finding) runs only on introduced + materially worsened. Pre-existing and
removed duplication remain counts/context and are never reissued as
findings. Old and new versions of the same function never become a
supposed pair: units are matched by (path, symbol) identity across
snapshots first, and same-key content changes do not pair with each other.

Move/rename identity is distinct from content identity: same symbol +
same content at a different path (original gone) is a move, not a new
unit. Delegating wrappers are uses, not copies (delegation guard).
Unchanged callers of changed callees are reexamined (invalidate.py):
a pre-existing family touching a reexamined unit is reported as worsened
(dependency-change), never silently kept as pre-existing.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import assess as A
from . import audit as AU
from . import delegation as dg
from . import families as fam
from . import index_cmd as idx
from . import invalidate as inv
from . import ledger as L
from . import report as R


def materialize(rev: str, repo: Path, dest: Path) -> None:
    """Materialize git revision `rev` of `repo` into `dest` via git archive."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(repo), "archive", rev],
                             capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)


def unit_map(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        out = {}
        for r in conn.execute(
                "SELECT path, symbol, kind, start_line, end_line, text, fingerprint,"
                " flags, refs FROM units WHERE kind IN ('function','closure','initializer')"):
            path, symbol, kind, sl, el, text, fp, flags, refs = r
            out[(path, symbol)] = {"path": path, "symbol": symbol, "kind": kind,
                                   "start_line": sl, "end_line": el, "text": text,
                                   "fingerprint": fp,
                                   "flags": json.loads(flags), "refs": json.loads(refs)}
        return out
    finally:
        conn.close()


def classify(base: dict, head: dict) -> dict:
    """Four-way unit classification + move detection + reexamine set."""
    base_keys, head_keys = set(base), set(head)
    added = head_keys - base_keys
    removed = base_keys - head_keys
    changed = {k for k in base_keys & head_keys
               if (base[k]["fingerprint"], base[k].get("text")) !=
               (head[k]["fingerprint"], head[k].get("text"))}
    unchanged = (base_keys & head_keys) - changed
    # Moves: added key whose (symbol, full content) exists under exactly one
    # other base path that no longer carries it. A copy (original persists)
    # is not a move — it stays added and may introduce.
    base_by_content = {}
    for (path, symbol), unit in base.items():
        base_by_content.setdefault((symbol, unit["fingerprint"], unit.get("text")), []).append(path)
    moved = set()
    for k in added:
        key = (k[1], head[k]["fingerprint"], head[k].get("text"))
        old_paths = [p for p in base_by_content.get(key, []) if p != k[0]]
        if len(old_paths) == 1 and (old_paths[0], k[1]) not in head:
            moved.add(k)
    changed_syms = inv.changed_symbols(base, head)
    reex = inv.reexamine(head, changed_syms)
    return {"added": added, "removed": removed, "changed": changed,
            "unchanged": unchanged, "moved": moved, "reexamine": reex}


def _id_map(db_path: Path) -> dict[int, tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {uid: (path, symbol) for uid, path, symbol in conn.execute(
            "SELECT id, path, symbol FROM units WHERE kind IN ('function','closure','initializer')")}
    finally:
        conn.close()


def _pairs_with_channels(conn: sqlite3.Connection, db_path: Path, neighbors_k: int,
                         touched: set | None = None) -> list[tuple[list[int], list[str]]]:
    """Whole-index pairs plus directed touched-unit retrieval, merged by key."""
    pairs = fam.form_families(conn, neighbors_k)
    if touched is not None:
        head_ids = _id_map(db_path)
        touched_ids = [uid for uid, key in head_ids.items() if key in touched]
        for pair, ch in fam.focused_families(conn, touched_ids):
            key = frozenset(pair)
            for i, (existing, ech) in enumerate(pairs):
                if frozenset(existing) == key:
                    pairs[i] = (existing, sorted(set(ech) | set(ch)))
                    break
            else:
                pairs.append((pair, ch))
    return pairs


def _delegation_pair(head: dict, db_path: Path, ka: tuple, kb: tuple) -> bool:
    return dg.is_delegation(head[ka]["text"], kb[1], _lang_of(db_path, ka)) or \
        dg.is_delegation(head[kb]["text"], ka[1], _lang_of(db_path, kb))


def _family_content(units: dict, keys: list) -> str:
    return "|".join(sorted(f"{units[m]['fingerprint']}\n{units[m].get('text', '')}" for m in keys))


def classify_pairs(base_db: Path, head_db: Path, neighbors_k: int = 30) -> dict:
    """Classify head families against base. Returns lists + reason tags.

    Families may have 2+ members (growth with full pairwise support);
    identity is the member-key set. A head family matching a base family
    with an added member is `worsened/expanded`, never pairwise spam.
    """
    base, head = unit_map(base_db), unit_map(head_db)
    units = classify(base, head)
    touched = units["added"] | units["changed"] | units["moved"] | units["reexamine"]

    def prepared(db_path: Path, with_focus: set | None):
        conn = sqlite3.connect(str(db_path))
        try:
            pairs = _pairs_with_channels(conn, db_path, neighbors_k, with_focus)
        finally:
            conn.close()
        idm = _id_map(db_path)
        units_here = base if db_path == base_db else head
        kept = []
        skipped = []
        for (a, b), ch in pairs:
            ka, kb = idm[a], idm[b]
            if ka == kb:
                continue  # degenerate self-pair; never a finding
            if db_path == head_db and _delegation_pair(head, db_path, ka, kb):
                skipped.append({"members": sorted([ka, kb]), "channels": ch})
                continue
            kept.append(([ka, kb], ch))
        return kept, skipped

    head_pairs, delegation_skipped = prepared(head_db, touched)
    base_pairs, _ = prepared(base_db, None)
    head_families = fam.grow(head_pairs)
    base_families = fam.grow(base_pairs)
    base_sets = {frozenset(m) for m, _ in base_families}
    base_content = {}
    for members, _ in base_families:
        try:
            base_content[frozenset(members)] = _family_content(base, list(members))
        except KeyError:
            pass
    introduced, worsened, preexisting = [], [], []
    for members, channels in head_families:
        keys = [tuple(m) for m in members]
        pk = frozenset(keys)
        content = _family_content(head, keys)
        if pk in base_sets:
            if content != base_content.get(pk):
                worsened.append({"members": sorted(keys), "channels": channels,
                                 "reason": "member-content-changed"})
            elif any(k in units["reexamine"] for k in keys):
                worsened.append({"members": sorted(keys), "channels": channels,
                                 "reason": "dependency-change"})
            else:
                preexisting.append({"members": sorted(keys), "reason": "unchanged"})
            continue
        supersets = [b for b in base_sets if b < pk]
        if supersets:
            # Shared members unchanged, new member(s) added: expanded family.
            shared = max(supersets, key=len)
            if all(m in base and head[m].get("text") == base[m].get("text") for m in shared):
                worsened.append({"members": sorted(keys), "channels": channels,
                                 "reason": "expanded"})
                continue
        if any(k in units["moved"] for k in keys) and content in set(base_content.values()):
            preexisting.append({"members": sorted(keys), "reason": "member-moved"})
        elif any(k in touched for k in keys):
            introduced.append({"members": sorted(keys), "channels": channels,
                               "reason": "new-or-changed-member"})
        else:
            preexisting.append({"members": sorted(keys), "reason": "stable-unreported"})
    removed = [sorted(k) for k in base_sets
               if not any(k <= set(map(tuple, h)) for h, _ in head_families)
               and any(m not in head for m in k)]
    return {"introduced": introduced, "worsened": worsened,
            "preexisting": preexisting,
            "removed": removed, "delegation_skipped": delegation_skipped,
            "units": {k: sorted(v) for k, v in units.items()}}


def _lang_of(db_path: Path, key: tuple[str, str]) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT language FROM files WHERE path=?", (key[0],)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def run_check(repo: Path, base_rev: str, head_rev: str, out_path: Path, ledger_path: Path,
              *, judge_url: str, judge_model: str, judge_key: str | None,
              judge_expected: str | None, challenge_url: str | None = None,
              challenge_model: str | None = None, challenge_key: str | None = None,
              challenge_expected: str | None = None, max_families: int = 20,
              max_challenge_calls: int = 20, neighbors_k: int = 30,
              timeout_s: float = 180.0, attempts: int = 2) -> dict:
    """Semantic incremental check: classify, then assess introduced/worsened.

    Only introduced + materially worsened families run the head-snapshot
    evidence -> assessment -> challenge -> finding pipeline. Pre-existing
    and removed stay counts/context in the sidecar, never findings.
    Unexamined introduced/worsened (budget/transport exhaustion) make the
    run partial with deferred accounting — never a clean result.
    """
    import tempfile as _tf

    started = time.monotonic()
    with _tf.TemporaryDirectory(prefix="semdup-check-base-") as _b, \
            _tf.TemporaryDirectory(prefix="semdup-check-head-") as _h:
        base_dir, head_dir = Path(_b) / "tree", Path(_h) / "tree"
        materialize(base_rev, repo, base_dir)
        materialize(head_rev, repo, head_dir)
        base_db, head_db = Path(_b) / "b.sqlite", Path(_h) / "h.sqlite"
        base_summary = idx.build(base_dir, base_db)
        head_summary = idx.build(head_dir, head_db)
        classification = classify_pairs(base_db, head_db, neighbors_k)
        assessable = ([{**f, "kind": "introduced"} for f in classification["introduced"]] +
                      [{**f, "kind": "worsened"} for f in classification["worsened"]])
        conn = sqlite3.connect(str(head_db))
        conn.row_factory = sqlite3.Row
        ledger = L.Ledger(ledger_path)
        assessor_text, assessor_hash = AU._load_prompt("assessor_v1.md")
        challenger_text, challenger_hash = AU._load_prompt("challenger_v2.md")
        same_endpoint = not challenge_url
        ctx = AU.FamilyCtx(
            conn=conn,
            judge_run=AU._transport(judge_url, judge_model, judge_key, judge_expected,
                                    timeout_s, attempts),
            challenge_run=AU._transport(challenge_url or judge_url, challenge_model or judge_model,
                                        challenge_key if challenge_url else judge_key,
                                        challenge_expected if challenge_url else judge_expected,
                                        timeout_s, attempts),
            ledger=ledger, assessor_text=assessor_text, assessor_hash=assessor_hash,
            challenger_text=challenger_text, challenger_hash=challenger_hash,
            judge_url=judge_url, judge_model=judge_model, judge_expected=judge_expected,
            c_url=challenge_url or judge_url, c_model=challenge_model or judge_model,
            c_expected=challenge_expected if challenge_url else judge_expected,
            same_endpoint=same_endpoint, max_challenge_calls=max_challenge_calls,
            timeout_s=timeout_s, attempts=attempts)
        key_to_uid = {}
        for uid, path, symbol in conn.execute(
                "SELECT id, path, symbol FROM units WHERE kind IN ('function','closure','initializer')"):
            key_to_uid[(path, symbol)] = uid
        candidate_groups, findings, examined, rank, skipped = [], [], 0, 0, 0
        finding_kinds = []
        for family in assessable[:max_families]:
            rank += 1
            try:
                uids = [key_to_uid[tuple(m)] for m in family["members"]]
            except KeyError:
                # Defensive only: classification and ids come from the same
                # snapshot, so members are present. Never fabricate spans:
                # count deferred with an error entry instead.
                skipped += 1
                ctx.errors.append(f"g{rank}: member left the snapshot; deferred")
                continue
            group, finding = AU.process_family(ctx, rank, uids, family["channels"], head_dir)
            candidate_groups.append(group)
            if finding is not None:
                findings.append(finding)
                finding_kinds.append({"finding_id": finding["id"], "check_kind": family["kind"],
                                      "reason": family.get("reason")})
                examined += 1
        conn.close()
        emitted = len(candidate_groups)
        deferred = max(0, len(assessable) - max_families) + (emitted - examined) + skipped
        # check_kind travels in the sidecar (frozen v1 carries no such field).
        coverage = {"files_eligible": head_summary["files_eligible"],
                    "files_parsed": head_summary["files_parsed"],
                    "files_failed": head_summary["files_failed"],
                    "units_indexed": head_summary["units_indexed"],
                    "units_searched": head_summary["units_searched"],
                    "candidate_groups_emitted": emitted,
                    "candidate_groups_examined": examined, "groups_deferred": deferred}
        status = "complete"
        if ctx.errors or head_summary["files_failed"] or deferred:
            status = "partial" if examined else "failed"
        config_sha = AU._sha(json.dumps(
            {"mode": "check", "base": base_rev, "head": head_rev,
             "judge": [judge_url, judge_model]}, sort_keys=True))
        result = {"schema_version": 1, "run_id": f"check-{uuid.uuid4().hex[:8]}",
                  "detector_version": AU.DETECTOR_VERSION, "config_sha256": config_sha,
                  "snapshot_sha256": head_summary["snapshot"], "status": status,
                  "errors": ctx.errors, "coverage": coverage, "usage": ledger.rollup(),
                  "candidate_groups": candidate_groups, "findings": findings}
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        tmp.rename(out_path)
        sidecar = {"base_snapshot": base_summary["snapshot"], "head_snapshot": head_summary["snapshot"],
                   "base_rev": base_rev, "head_rev": head_rev,
                   "preexisting": classification["preexisting"], "removed": classification["removed"],
                   "delegation_skipped": classification["delegation_skipped"],
                   "units": classification["units"],
                   "finding_kinds": finding_kinds,
                   "challenge_coverage": f"{ctx.challenges_spent}/{ctx.actionable_seen}"}
        out_path.with_suffix(".sidecar.json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n",
                                                         encoding="utf-8")
        report_text = R.render(f"check {base_rev[:8]}..{head_rev[:8]}", findings, coverage,
                               result["usage"],
                               [f"introduced={len(classification['introduced'])} "
                                f"worsened={len(classification['worsened'])} "
                                f"preexisting={len(classification['preexisting'])} "
                                f"removed={len(classification['removed'])}",
                                f"ledger: {ledger_path} ({len(ledger.records)} calls)",
                                f"wall: {time.monotonic() - started:.1f}s"])
        return {"result": result, "report": report_text, "sidecar": sidecar,
                "ledger_records": len(ledger.records)}
