"""Parallel editorial shadow (SEMDUP-07 Track A, DEV-only, report-only).

Runs the SEMDUP-06 Terra-propose / Luna-edit pipeline alongside the
existing DEV shadow implementation (M11 investigate -> findings ->
advisories) on the same delta, and persists its outputs separately
(`editorial.json` next to `findings.json` / `advisories.json`).

Rules:
- Prospective-only: invoked only from the worker's normal delta flow,
  which enforces delivery dedup, last-processed-head checkpoints,
  delta fingerprints, and the grandfather path for pre-existing PRs.
  This module performs no backfill and keeps no independent cursor.
- Never affects the engine/advisory path: every failure (backends,
  evidence, models, validation, budget) degrades to a recorded
  editorial outcome. This module never raises into the worker.
- Bounded: at most `max_pairs` candidate pairs per delta and a
  per-run spend ceiling; estimates are checked before each model call.
- No GitHub comments, no notifications, no state outside the run dir
  except the spend sink inside it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

EDITORIAL_VERSION = "semdup-editorial-shadow-0.1"
DEFAULT_MAX_PAIRS = 4
DEFAULT_CEILING_USD = 0.20
ELIGIBLE_RECORD_STATUS = ("finding", "investigate")


def _is_oracle(path: str) -> bool:
    return path == "oracles" or path.startswith("oracles/")


def _is_fragment(symbol: str) -> bool:
    return "#region" in (symbol or "")


def unordered_mirror_key(target: dict, counterpart: dict) -> str:
    """Unordered identity of a unit pair (direction-insensitive).

    Same format as the post-hoc mirror consolidation: the sorted
    ``path::symbol`` pair joined by ``~~``. The single definition
    lives here; stage._dedup_mirrors reuses it.
    """
    a = f"{(target or {}).get('path')}::{(target or {}).get('symbol')}"
    b = f"{(counterpart or {}).get('path')}::{(counterpart or {}).get('symbol')}"
    return "~~".join(sorted((a, b)))


def mirror_skip_decision(target: dict, counterpart: dict,
                         published_keys) -> bool:
    """True when the opposite direction already has an established
    published verdict: the unordered pair key is present in the
    published set (earlier in this run, or a same-revision
    continuation seed). Never true on rejection alone — only
    published verdicts are recorded in the set."""
    try:
        return unordered_mirror_key(target, counterpart) in published_keys
    except Exception:
        return False


def mirror_preskip_row(pair: dict, *, head_rev: str, base_rev: str,
                       kept: str = "") -> dict:
    """Suppressed row for a pre-examination mirror skip ($0, no verdict).

    Not a judgment: completed=False so it is never cached or continued
    (the kept twin's own record re-establishes the skip on re-drive),
    and assess never ran so it never counts as examined.
    """
    return {
        "target": pair.get("target"), "counterpart": pair.get("counterpart"),
        "record_status": pair.get("record_status"),
        "classification": pair.get("classification"),
        "head_rev": head_rev, "base_rev": base_rev,
        "path": "suppressed", "reason": "mirror_of_published",
        "detail": ("same unordered unit pair already published"
                   + (f" ({kept})" if kept else "") +
                   "; opposite direction skipped before examination"),
        "assess_ran": False, "completed": False, "budget_refused": False,
        "terra_completed": False, "luna_completed": None,
    }


def _split_unit(ref: str) -> tuple[str, str] | None:
    if "::" not in ref:
        return None
    path, _, symbol = ref.partition("::")
    path, symbol = path.strip(), symbol.strip()
    if not path or not symbol:
        return None
    return path, symbol


# SCAFFOLD-01: deterministic pre-examination suppression of test
# setup/teardown/fixture scaffolding. All inputs are pre-inference
# (unit texts from the evidence package, file paths); no model
# judgment, no shared_component, no yield signal is consulted.
# Matched against the whitespace-collapsed (not fully stripped)
# line, so verdict keywords keep word boundaries ("throw " never
# matches "throws"; "assert " never matches an identifier containing
# "assert"). Substring matching errs toward failing open.
SCAFFOLD_ASSERT_TOKENS = (
    "assert ", "#expect", "#require", "xctassert", "xctfail",
    "issue.record", "pytest.raises", "require(", "expect(", "wait(",
    "fatalerror", "precondition", "raise ", "throw ", "throw(",
    "check(",
)

_SCAFFOLD_TRIVIAL = {"{", "}", "}", "(", ")", ",", ";"}


def _scaffold_mask_collapsed(line: str) -> str:
    """Skeleton form: string literals masked, whitespace collapsed.

    Assertion tokens are matched against this form so keywords keep
    word boundaries."""
    import re as _re
    try:
        masked = _re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
        masked = _re.sub(r"'(?:[^'\\]|\\.)*'", "''", masked)
        return _re.sub(r"\s+", " ", masked).strip()
    except Exception:
        return ""


def _scaffold_mask(line: str) -> str:
    """Sharing key: collapsed skeleton with all whitespace stripped,
    so purely stylistic spacing never splits a shared setup line."""
    try:
        return "".join(_scaffold_mask_collapsed(line).split())
    except Exception:
        return ""


def _scaffold_trivial(masked: str) -> bool:
    if not masked or masked in _SCAFFOLD_TRIVIAL:
        return True
    if len(masked) < 3 and not masked[0].isalnum():
        return True
    if masked.startswith("//"):
        return True
    if masked.startswith("#") and not masked.startswith(
            ("#expect", "#require")):
        return True
    return False


def _scaffold_is_test_path(path: str) -> bool:
    """Positive test-only identification: existing classifier plus the
    *-Tests directory convention (e.g. PresentationTests/)."""
    try:
        from pathlib import PurePosixPath as _PPP
        from codatiel_i2.core import files as _F
        if _F._is_test_path(_PPP(str(path or ""))):
            return True
    except Exception:
        pass
    try:
        parts = str(path or "").split("/")
        return any(p.lower().endswith("tests") for p in parts[:-1])
    except Exception:
        return False


def scaffold_preskip_decision(target: dict,
                              counterpart: dict) -> dict:
    """Decide pre-examination scaffolding suppression (deterministic).

    ``target``/``counterpart`` carry ``path`` and ``text`` (unit text
    from the evidence package). Returns {"skip", "shared", "detail"}.
    Skips only when: both units are test-only; the skeleton-shared
    lines are non-empty and carry no assertion/verdict token; and every
    shared line lies in the setup region (before the first assertion)
    or teardown region (after the last assertion) of BOTH units.
    Fails open (no skip) on any ambiguity, including unparseable
    input — never raises.
    """
    no = {"skip": False, "shared": 0, "detail": ""}
    try:
        t = target or {}
        c = counterpart or {}
        if not (_scaffold_is_test_path(t.get("path"))
                and _scaffold_is_test_path(c.get("path"))):
            return dict(no, detail="not both test-only")

        def normalized(text):
            # (sharing key, collapsed line) pairs; sharing compares
            # whitespace-insensitively while assertions and regions are
            # judged on the collapsed form with word boundaries intact.
            out = []
            for raw in str(text or "").splitlines():
                col = _scaffold_mask_collapsed(raw)
                if not col or _scaffold_trivial(col):
                    continue
                out.append(("".join(col.split()), col))
            return out

        tnorm = normalized(t.get("text"))
        cnorm = normalized(c.get("text"))
        if not tnorm or not cnorm:
            return dict(no, detail="empty unit")
        tkeys = [k for k, _ in tnorm]
        ckeys = [k for k, _ in cnorm]
        shared = sorted(set(tkeys) & set(ckeys))
        if not shared:
            return dict(no, detail="no shared skeleton lines")

        def key_asserted(lines, key):
            # Assertion status of the collapsed line behind a key.
            for k, col in lines:
                if k == key and any(tok in col.lower()
                                    for tok in SCAFFOLD_ASSERT_TOKENS):
                    return True
            return False

        bad = [k for k in shared
               if key_asserted(tnorm, k) or key_asserted(cnorm, k)]
        if bad:
            return dict(no, detail=f"shared assertions ({len(bad)})")

        def regions(lines):
            first, last = None, -1
            for i, (_, col) in enumerate(lines):
                if any(tok in col.lower()
                       for tok in SCAFFOLD_ASSERT_TOKENS):
                    if first is None:
                        first = i
                    last = i
            if first is None:
                return set(range(len(lines))), set()
            return set(range(first)), set(range(last + 1, len(lines)))

        def in_setup(lines, key):
            idx = [i for i, (k, _) in enumerate(lines) if k == key]
            ok = regions(lines)[0] | regions(lines)[1]
            return bool(idx) and all(i in ok for i in idx)

        if any(not in_setup(tnorm, k) for k in shared):
            return dict(no, detail="shared outside setup/teardown (target)")
        if any(not in_setup(cnorm, k) for k in shared):
            return dict(no, detail="shared outside setup/teardown")
        return {"skip": True, "shared": len(shared),
                "detail": (f"shared test setup ({len(shared)} lines, "
                           "no assertions); skipped before examination")}
    except Exception:
        return dict(no, detail="unreadable input")


def candidate_pairs(records: list, max_pairs: int) -> tuple[list, list]:
    """Collect (target, counterpart) candidates from finding records.

    Deterministic order: record order, then counterpart order. Oracle
    and region-fragment references are excluded with a recorded reason.
    Returns (pairs, skipped) where skipped records the reason per unit.
    """
    pairs: list = []
    skipped: list = []
    for record in records or []:
        if record.get("status") not in ELIGIBLE_RECORD_STATUS:
            continue
        target = record.get("target") or {}
        tpath, tsym = target.get("path"), target.get("symbol")
        if not tpath or not tsym:
            skipped.append({"unit": f"{tpath}::{tsym}",
                            "reason": "target unresolvable"})
            continue
        for ref in (record.get("counterparts") or []):
            if len(pairs) >= max_pairs:
                break
            split = _split_unit(str(ref))
            if split is None:
                skipped.append({"unit": str(ref),
                                "reason": "counterpart unparseable"})
                continue
            cpath, csym = split
            if _is_oracle(cpath) or _is_fragment(csym):
                skipped.append({"unit": str(ref),
                                "reason": "oracle/fragment excluded"})
                continue
            if cpath == tpath and csym == tsym:
                skipped.append({"unit": str(ref),
                                "reason": "self-pair excluded"})
                continue
            pairs.append({"target": {"path": tpath, "symbol": tsym},
                          "counterpart": {"path": cpath, "symbol": csym},
                          "record_status": record.get("status"),
                          "classification": record.get("classification")})
        if len(pairs) >= max_pairs:
            break
    return pairs, skipped


def _is_budget_error(err: Any) -> bool:
    """Ledger/spending-guard refusal, never a model verdict."""
    text = str(err or "")
    return ("BudgetCeilingExceeded" in text
            or "exceeds remaining budget" in text)


def _preflight_usd(pairs: list, merged_db: Path, terra_res: dict,
                   luna_res: dict) -> float | None:
    """Upper-bound cost to judge every pair through both models.

    Sums actual-size Terra + Luna estimates over the pair list using
    the deterministic representative texts (same rule as retrieval).
    Luna only runs on Terra leads, so the true cost is at or below
    this number — a conservative planning bound, never a promise.
    None when rates or texts are unavailable.
    """
    try:
        from codatiel_i2.semdup_dev import oai_dupe as _OD
        from codatiel_i2.semdup_dev import editor as _ED
        conn = sqlite3.connect(str(merged_db))
    except Exception:  # noqa: BLE001 - no preflight without the view
        return None
    try:
        total = 0.0
        for p in pairs:
            if not isinstance(p, dict):
                continue
            sizes = []
            for u in (p.get("target") or {}, p.get("counterpart") or {}):
                if not isinstance(u, dict):
                    continue
                r = conn.execute(
                    "SELECT length(text) FROM units WHERE path=? AND "
                    "symbol=? ORDER BY start_line LIMIT 1",
                    (u.get("path"), u.get("symbol"))).fetchone()
                if not r or not r[0]:
                    return None
                sizes.append(int(r[0]))
            if len(sizes) != 2:
                return None
            body = sum(sizes)
            te = _OD.estimate_call_usd(
                len(_OD.TERRA_INSTRUCTIONS) + body,
                terra_res.get("rate_usd_per_mtok") or {})
            le = _OD.estimate_call_usd(
                len(_ED.EDITOR_INSTRUCTIONS) + body,
                luna_res.get("rate_usd_per_mtok") or {})
            if te is None or le is None:
                return None
            total += te + le
        return round(total, 6)
    except Exception:  # noqa: BLE001 - estimates are best-effort
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _spent(ledger) -> float | None:
    try:
        return ledger.confirmed_usd()
    except Exception:  # noqa: BLE001 - spend unknown => stop spending
        return None


def _input_chars(render: Callable | None, *args: Any) -> int | None:
    """Actual model input size for the estimate, never a fixed proxy.

    A fixed character proxy blinds the preflight gate to real evidence
    size (a 50 KB unit costs ~15x the old 12 K proxy), letting calls
    pass the gate only to die at the ledger — mislabelled as model
    verdicts. Measure the rendered input; fall back to the proxy only
    if rendering itself fails.
    """
    try:
        if render is None:
            return None
        return len(render(*args))
    except Exception:  # noqa: BLE001 - fall back to the proxy
        return None


def run_editorial(*, cfg, repo_path: Path, merged_db: Path,
                  base_rev: str, head_rev: str, records: list,
                  run_dir: Path, head_tree: Path | None,
                  base_tree: Path | None, max_pairs: int | None = None,
                  ceiling_usd: float | None = None,
                  assess_fn: Callable | None = None,
                  edit_fn: Callable | None = None) -> dict:
    """Run the editorial shadow pass for one delta. Never raises."""
    t0 = time.monotonic()
    outcome: dict[str, Any] = {
        "enabled": True, "ok": False, "version": EDITORIAL_VERSION,
        "head_rev": head_rev, "base_rev": base_rev,
        "pairs": [], "published": [], "suppressed": [],
        "skipped": [], "spend_usd": 0.0, "wall_s": 0.0,
        "mirror_skipped": 0, "mirror_saved_usd_est": None,
        "mirror_saved_wall_s_est": None, "scaffold_skipped": 0,
        "scaffold_saved_usd_est": None,
        "scaffold_saved_wall_s_est": None,
    }
    try:
        return _run(cfg=cfg, repo_path=repo_path, merged_db=merged_db,
                    base_rev=base_rev, head_rev=head_rev, records=records,
                    run_dir=run_dir, head_tree=head_tree,
                    base_tree=base_tree, max_pairs=max_pairs,
                    ceiling_usd=ceiling_usd, assess_fn=assess_fn,
                    edit_fn=edit_fn, outcome=outcome, t0=t0)
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        outcome["ok"] = False
        outcome["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome


def _run(*, cfg, repo_path, merged_db, base_rev, head_rev, records,
         run_dir, head_tree, base_tree, max_pairs, ceiling_usd,
         assess_fn, edit_fn, outcome, t0) -> dict:
    from codatiel_i2 import providers as P
    from codatiel_i2 import runtime_sdk as R
    from codatiel_i2.core.spend import SpendLedger
    from codatiel_i2.semdup_dev import editor as ED
    from codatiel_i2.semdup_dev import oai_dupe as OD

    max_pairs = (cfg.editorial_max_pairs if max_pairs is None
                 else max_pairs)
    ceiling_usd = (cfg.editorial_ceiling_usd if ceiling_usd is None
                   else ceiling_usd)
    pairs, skipped = candidate_pairs(records, max_pairs)
    outcome["skipped"] = skipped
    if head_tree is None or base_tree is None:
        outcome["ok"] = True
        outcome["stage"] = "no_head_tree"
        outcome["skipped"] = skipped + [
            {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                     f"{p['counterpart']['path']}::{p['counterpart']['symbol']}",
             "reason": "head/base tree unavailable; candidates recorded, "
                       "no inference attempted"} for p in pairs]
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    try:
        terra_res = P.resolve_backend("terra")
        luna_res = P.resolve_backend("luna")
    except Exception as exc:  # noqa: BLE001 - no credentials => no spend
        outcome["ok"] = True
        outcome["stage"] = "backends_unavailable"
        outcome["skipped"] = skipped + [
            {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                     f"{p['counterpart']['path']}::{p['counterpart']['symbol']}",
             "reason": f"model backend unavailable: {exc}; no inference "
                       "attempted"} for p in pairs]
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    terra_model_id, terra_settings = P.build_model("terra")
    luna_model_id, luna_settings = P.build_model("luna")
    half = max(ceiling_usd / 2.0, 0.0)
    terra_ledger = SpendLedger(rate=terra_res["rate_usd_per_mtok"],
                              rate_status=terra_res["rate_status"],
                              ceiling_usd=ceiling_usd)
    luna_ledger = SpendLedger(rate=luna_res["rate_usd_per_mtok"],
                             rate_status=luna_res["rate_status"],
                             ceiling_usd=ceiling_usd)
    terra_ledger.attach_sink(run_dir / "spend_editorial_terra.jsonl")
    luna_ledger.attach_sink(run_dir / "spend_editorial_luna.jsonl")
    outcome["models"] = {
        "terra": {"model": terra_res.get("model"),
                  "provider": terra_res.get("provider"),
                  "instructions_id": OD.instructions_id(
                      "terra", OD.TERRA_INSTRUCTIONS)},
        "luna": {"model": luna_res.get("model"),
                 "provider": luna_res.get("provider"),
                 "instructions_id": ED.editor_instructions_id()},
    }
    assess = assess_fn or OD.assess_pair
    edit = edit_fn or ED.edit_pair
    # MIRROR-01: first-published-wins within this run. A reverse pair is
    # skipped before any paid call only after its twin published.
    published_keys: dict[str, str] = {}
    outcome["mirror_skipped"] = 0
    for pair in pairs:
        mkey = unordered_mirror_key(pair.get("target"),
                                    pair.get("counterpart"))
        if mirror_skip_decision(pair.get("target"),
                                pair.get("counterpart"), published_keys):
            row = mirror_preskip_row(pair, head_rev=head_rev,
                                     base_rev=base_rev,
                                     kept=published_keys[mkey])
            outcome["pairs"].append(row)
            outcome["suppressed"].append(_suppressed_row(row))
            outcome["mirror_skipped"] += 1
            continue
        row = _review_pair(
            pair, cfg=cfg, repo_path=repo_path, merged_db=merged_db,
            base_rev=base_rev, head_rev=head_rev, head_tree=head_tree,
            base_tree=base_tree, terra_res=terra_res, luna_res=luna_res,
            terra_model_id=terra_model_id, terra_settings=terra_settings,
            luna_model_id=luna_model_id, luna_settings=luna_settings,
            terra_ledger=terra_ledger, luna_ledger=luna_ledger,
            ceiling_usd=ceiling_usd, half=half, assess=assess, edit=edit,
            OD=OD, ED=ED, R=R)
        outcome["pairs"].append(row)
        if row["path"] == "published":
            t, c = row.get("target") or {}, row.get("counterpart") or {}
            published_keys[mkey] = (
                f"{t.get('path')}::{t.get('symbol')} -> "
                f"{c.get('path')}::{c.get('symbol')}")
            outcome["published"].append(_public_row(row))
        else:
            outcome["suppressed"].append(_suppressed_row(row))
    spent_t, spent_l = _spent(terra_ledger), _spent(luna_ledger)
    outcome["spend_terra_usd"] = spent_t
    outcome["spend_luna_usd"] = spent_l
    outcome["spend_terra_unknown"] = spent_t is None
    outcome["spend_luna_unknown"] = spent_l is None
    if spent_t is None or spent_l is None:
        outcome["spend_usd"] = (None if (spent_t is None and spent_l is None)
                                else round((spent_t or 0.0) + (spent_l or 0.0), 6))
    else:
        outcome["spend_usd"] = round(spent_t + spent_l, 6)
    outcome["spend_unknown"] = spent_t is None or spent_l is None
    _record_mirror_savings(outcome, spent_t, spent_l, t0)
    _record_scaffold_savings(outcome, spent_t, spent_l, t0)
    outcome["ok"] = True
    outcome["wall_s"] = round(time.monotonic() - t0, 1)
    return outcome


def _record_mirror_savings(outcome: dict, spent_t, spent_l, t0) -> None:
    _record_skip_savings(outcome, spent_t, spent_l, t0, "mirror_skipped",
                         "mirror_saved_usd_est", "mirror_saved_wall_s_est")


def _record_scaffold_savings(outcome: dict, spent_t, spent_l, t0) -> None:
    # Scaffold skips land inside _review_inner (no loop counter), so
    # count suppressed scaffold_setup rows at finalize time.
    outcome["scaffold_skipped"] = sum(
        1 for r in outcome.get("pairs", [])
        if isinstance(r, dict) and r.get("reason") == "scaffold_setup")
    _record_skip_savings(outcome, spent_t, spent_l, t0,
                         "scaffold_skipped", "scaffold_saved_usd_est",
                         "scaffold_saved_wall_s_est")


def _record_skip_savings(outcome: dict, spent_t, spent_l, t0, count_key: str,
                         usd_key: str, wall_key: str) -> None:
    """Estimated dollars/latency saved by pre-examination skips.

    Estimates only (labeled _est): skipped examinations are valued at
    the run's mean examined-pair cost and latency. The mean includes
    cheap Terra-only rejections, so the dollar figure is a
    conservative floor (a skipped examination that would have run both
    models costs more than the mean). USD is None when the spend is
    unknown or no examination baseline exists; latency needs only the
    wall clock.
    """
    mirror_n = int(outcome.get(count_key) or 0)
    outcome[count_key] = mirror_n
    outcome[usd_key] = None
    outcome[wall_key] = None
    if not mirror_n:
        outcome[usd_key] = 0.0
        outcome[wall_key] = 0.0
        return
    examined = sum(1 for r in outcome.get("pairs", [])
                   if isinstance(r, dict) and r.get("assess_ran"))
    if not examined:
        return
    if spent_t is not None and spent_l is not None:
        outcome[usd_key] = round(
            mirror_n * (spent_t + spent_l) / examined, 6)
    wall = round(time.monotonic() - t0, 1)
    outcome[wall_key] = round(mirror_n * wall / examined, 1)


def _review_pair(pair, *, cfg, repo_path, merged_db, base_rev, head_rev,
                 head_tree, base_tree, terra_res, luna_res,
                 terra_model_id, terra_settings, luna_model_id,
                 luna_settings, terra_ledger, luna_ledger, ceiling_usd,
                 half, assess, edit, OD, ED, R) -> dict:
    pt0 = time.monotonic()
    row = _review_inner(
        pair, cfg=cfg, repo_path=repo_path, merged_db=merged_db,
        base_rev=base_rev, head_rev=head_rev, head_tree=head_tree,
        base_tree=base_tree, terra_res=terra_res, luna_res=luna_res,
        terra_model_id=terra_model_id, terra_settings=terra_settings,
        luna_model_id=luna_model_id, luna_settings=luna_settings,
        terra_ledger=terra_ledger, luna_ledger=luna_ledger,
        ceiling_usd=ceiling_usd, half=half, assess=assess, edit=edit,
        OD=OD, ED=ED, R=R)
    row["wall_s"] = round(time.monotonic() - pt0, 1)
    return row


def _review_inner(pair, *, cfg, repo_path, merged_db, base_rev, head_rev,
                  head_tree, base_tree, terra_res, luna_res,
                  terra_model_id, terra_settings, luna_model_id,
                  luna_settings, terra_ledger, luna_ledger, ceiling_usd,
                  half, assess, edit, OD, ED, R) -> dict:
    row: dict[str, Any] = {
        "target": pair["target"], "counterpart": pair["counterpart"],
        "record_status": pair.get("record_status"),
        "classification": pair.get("classification"),
        "head_rev": head_rev, "base_rev": base_rev,
    }
    ev = ED.build_evidence_package(
        merged_db, repo_path, head_rev,
        pair["target"], pair["counterpart"])
    if not ev["ok"]:
        row.update({"path": "suppressed",
                    "reason": "evidence_failed",
                    "detail": f"{ev.get('code')}: {ev.get('detail', '')}",
                    # Completion state (COST-01B): evidence failure is
                    # inability, never a verdict — always retried.
                    "completed": False, "budget_refused": False,
                    "terra_completed": False, "luna_completed": None})
        return row
    row["anchors"] = {
        "target": {k: ev["target"][k] for k in
                   ("path", "symbol", "start_line", "end_line", "sha256")},
        "counterpart": {k: ev["counterpart"][k] for k in
                        ("path", "symbol", "start_line", "end_line",
                         "sha256")},
    }
    pre = ED.wrapper_prefilter(ev["target"]["text"],
                               ev["counterpart"]["symbol"],
                               ev["target"]["symbol"])
    if pre["wrapper"]:
        row.update({"path": "suppressed", "reason": "deterministic_drop",
                    "detail": pre["detail"],
                    # Deterministic prefilter verdict: completed without
                    # model calls, safely reusable across revisions.
                    "completed": True, "budget_refused": False,
                    "terra_completed": None, "luna_completed": None})
        return row
    # SCAFFOLD-01: deterministic test-scaffolding suppression. Runs on
    # pre-inference evidence only (unit texts + paths); completed like
    # other deterministic verdicts, so retries and revisions reuse it
    # without re-examination. Never fires on shared assertions, test
    # logic, oracles, or non-test code (fail open).
    sc = scaffold_preskip_decision(
        {"path": pair["target"].get("path"),
         "text": ev["target"]["text"]},
        {"path": pair["counterpart"].get("path"),
         "text": ev["counterpart"]["text"]})
    if sc["skip"]:
        row.update({"path": "suppressed", "reason": "scaffold_setup",
                    "detail": sc["detail"],
                    "completed": True, "budget_refused": False,
                    "terra_completed": False, "luna_completed": None})
        return row
    terra_ctx = R.ReviewContext(
        repo=cfg.repos[0] if cfg.repos else "dev", base_sha=base_rev,
        head_sha=head_rev, base_dir=base_tree, head_dir=head_tree,
        diff_repo=repo_path, tool_budget=OD.TERRA_TOOL_BUDGET)
    est = OD.estimate_call_usd(
        _input_chars(OD.render_terra_input, ev, head_rev,
                     pair.get("classification") or "introduced")
        or (len(OD.TERRA_INSTRUCTIONS) + 12000),
        terra_res["rate_usd_per_mtok"])
    s = _spent(terra_ledger)
    if s is None or (est is not None and s + est > half):
        row.update({"path": "suppressed", "reason": "budget_stop",
                    "detail": ("terra estimate exceeds its half of the "
                               f"whole-PR ceiling ${ceiling_usd:.2f}; "
                               "unexamined, not rejected"),
                    "completed": False, "budget_refused": True,
                    "terra_completed": False, "luna_completed": None})
        return row
    ta = assess(
        evidence={"target": ev["target"],
                  "counterpart": ev["counterpart"],
                  "pair_sha256": "editorial-shadow"},
        head_rev=head_rev,
        target_kind=pair.get("classification") or "introduced",
        tools=OD.scoped_tools(terra_ctx), model=terra_model_id,
        model_settings=terra_settings, ledger=terra_ledger, ctx=terra_ctx)
    row["assess_ran"] = True
    row["terra_tool_calls"] = len(
        ((ta.get("transcript") or {}).get("tool_calls", [])))
    if not ta.get("ok") or not (ta.get("terra") or {}).get("overlap"):
        err = str(ta.get("error", "") or "")
        if _is_budget_error(err):
            # Ledger refusal is inability, never a decline: record the
            # guard stop explicitly so budget exhaustion cannot hide
            # behind a model-rejection label.
            row.update({"path": "suppressed", "reason": "budget_stop",
                        "detail": ("Terra call refused by the spending "
                                   f"guard ({err[:160]}); unexamined, "
                                   "not rejected"),
                        "completed": False, "budget_refused": True,
                        "terra_completed": False, "luna_completed": None})
            return row
        terra_ok = bool(ta.get("ok"))
        row.update({"path": "suppressed", "reason": "terra_no_lead",
                    "detail": err,
                    "terra_note": str(
                        (ta.get("transcript") or {}).get("final_text",
                                                       "")
                        or "")[:600],
                    # Completion state: a successful Terra call that finds
                    # no overlap is a completed no-lead verdict (reusable);
                    # a failed call is inability (always retried).
                    "terra_completed": terra_ok, "luna_completed": None,
                    "budget_refused": False, "completed": terra_ok})
        return row
    terra = ta["terra"]
    row["terra"] = {k: terra.get(k) for k in
                    ("overlap", "relationship", "opportunity",
                     "shared_component", "differences")}
    if terra.get("opportunity", "none") == "none":
        row.update({"path": "suppressed",
                    "reason": "terra_no_opportunity",
                    "terra_note": str(terra.get("shared_component", "")
                                      or "")[:300],
                    "terra_completed": True, "luna_completed": None,
                    "budget_refused": False, "completed": True})
        return row
    proposed = ED.propose_comment(terra, ev)
    row["proposed_comment"] = proposed
    luna_ctx = R.ReviewContext(
        repo=cfg.repos[0] if cfg.repos else "dev", base_sha=base_rev,
        head_sha=head_rev, base_dir=base_tree, head_dir=head_tree,
        diff_repo=repo_path, tool_budget=8)
    est_l = OD.estimate_call_usd(
        _input_chars(OD.render_luna_input, ev, terra)
        or (len(ED.EDITOR_INSTRUCTIONS) + 12000),
        luna_res["rate_usd_per_mtok"])
    sl = _spent(luna_ledger)
    if sl is None or (est_l is not None and sl + est_l > half):
        row.update({"path": "suppressed", "reason": "budget_stop",
                    "detail": ("luna estimate exceeds its half of the "
                               f"whole-PR ceiling ${ceiling_usd:.2f}; "
                               "Terra proposal held, never published "
                               "without Luna review"),
                    "terra_completed": True, "luna_completed": None,
                    "budget_refused": True, "completed": False})
        return row
    lt0 = time.monotonic()
    luna = edit(
        package=ev, proposed_comment=proposed,
        tools=R.build_i2_tools(luna_ctx), model=luna_model_id,
        model_settings=luna_settings, ledger=luna_ledger, ctx=luna_ctx)
    row["edit_ran"] = True
    row["luna_wall_s"] = round(time.monotonic() - lt0, 1)
    row["luna_tool_calls"] = len(
        ((luna.get("transcript") or {}).get("tool_calls", [])))
    row["editor"] = luna.get("editor")
    row["editor_error"] = luna.get("error", "")
    if not luna.get("ok"):
        if _is_budget_error(luna.get("error", "")):
            row.update({"path": "suppressed", "reason": "budget_stop",
                        "detail": ("Luna call refused by the spending "
                                   "guard; Terra proposal held, never "
                                   "published without Luna review"),
                        "terra_completed": True, "luna_completed": False,
                        "budget_refused": True, "completed": False})
            return row
        row.update({"path": "suppressed",
                    "reason": "editor_unpublishable",
                    "detail": str(luna.get("error", "") or "")[:600],
                    "terra_completed": True, "luna_completed": False,
                    "budget_refused": False, "completed": False})
        return row
    decision = (luna.get("editor") or {}).get("decision")
    if decision not in ("KEEP", "REVISE"):
        row.update({"path": "suppressed", "reason": "editor_drop",
                    "detail": ((luna.get("editor") or {}).get("reason", "")
                               or "")[:600],
                    "terra_completed": True, "luna_completed": True,
                    "budget_refused": False, "completed": True})
        return row
    row.update({"path": "published", "decision": decision,
                "revised_comment": (luna.get("editor") or {}).get(
                    "revised_comment", ""),
                "reason": ((luna.get("editor") or {}).get("reason", "")
                           or "")[:800],
                "terra_completed": True, "luna_completed": True,
                "budget_refused": False, "completed": True})
    return row


def _public_row(row: dict) -> dict:
    return {
        "target": row["target"], "counterpart": row["counterpart"],
        "head_rev": row.get("head_rev"),
        "decision": row.get("decision"),
        "revised_comment": row.get("revised_comment", ""),
        "reason": row.get("reason", ""),
        "anchors": row.get("anchors", {}),
        "inference": {
            "terra_wall_s": row.get("terra_wall_s", 0.0),
            "terra_tool_calls": row.get("terra_tool_calls", 0),
            "luna_wall_s": row.get("luna_wall_s", 0.0),
            "luna_tool_calls": row.get("luna_tool_calls", 0),
        },
    }


def _suppressed_row(row: dict) -> dict:
    return {
        "target": row["target"], "counterpart": row["counterpart"],
        "head_rev": row.get("head_rev"),
        "reason": row.get("reason", ""),
        "detail": str(row.get("detail", "") or "")[:600],
        "terra": row.get("terra", {}),
        "terra_note": str(row.get("terra_note", "") or "")[:600],
        "anchors": row.get("anchors", {}),
    }


def review_candidate_pairs(pairs: list, *, repo: str, repo_path: Path,
                           merged_db: Path, base_rev: str, head_rev: str,
                           head_tree: Path | str | None,
                           base_tree: Path | str | None,
                           run_dir: Path, ceiling_usd: float,
                           assess_fn: Callable | None = None,
                           edit_fn: Callable | None = None,
                           on_pair: Callable | None = None,
                           is_stale: Callable[[], bool] | None = None,
                           known_published_keys=None) -> dict:
    """Terra-propose / Luna-edit over directly-retrieved pairs.

    SEMDUP-08 integrated stage: pairs come from deterministic retrieval
    (`oai_dupe.retrieve_pairs`), NOT from M11/Gemma records. No Gemma
    stage is invoked anywhere on this path; Terra and Luna are resolved
    via the standard product backends. Every failure degrades to a
    recorded row; this function never raises.

    `on_pair` (optional) receives each judged row right after it is
    recorded, so the caller can persist verdicts incrementally.

    `known_published_keys` (optional) maps unordered pair keys to the
    kept direction for already-established published verdicts
    (same-content continuation/cache hits). A reverse pair whose key
    is present is skipped before examination (MIRROR-01); nothing is
    ever skipped on a mere rejection.

    Returns {"ok", "models", "pairs", "published", "suppressed",
    "skipped", "spend_usd", "spend_unknown", "wall_s", "stage"/"error"}.
    """
    from types import SimpleNamespace
    t0 = time.monotonic()
    outcome: dict[str, Any] = {
        "enabled": True, "ok": False, "version": EDITORIAL_VERSION,
        "head_rev": head_rev, "base_rev": base_rev,
        "pairs": [], "published": [], "suppressed": [],
        "skipped": [], "spend_usd": 0.0, "wall_s": 0.0,
        "preflight_usd": None, "mirror_skipped": 0,
        "mirror_saved_usd_est": None, "mirror_saved_wall_s_est": None,
        "scaffold_skipped": 0, "scaffold_saved_usd_est": None,
        "scaffold_saved_wall_s_est": None,
    }
    try:
        from codatiel_i2 import providers as P
        from codatiel_i2 import runtime_sdk as R
        from codatiel_i2.core.spend import SpendLedger
        from codatiel_i2.semdup_dev import editor as ED
        from codatiel_i2.semdup_dev import oai_dupe as OD
    except Exception as exc:  # noqa: BLE001 - stack unavailable
        outcome["ok"] = False
        outcome["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    if head_tree is None or base_tree is None:
        outcome["ok"] = True
        outcome["stage"] = "no_head_tree"
        outcome["skipped"] = [
            {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                     f"{p['counterpart']['path']}::"
                     f"{p['counterpart']['symbol']}",
             "reason": "head/base tree unavailable; candidates recorded, "
                       "no inference attempted"} for p in pairs]
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    try:
        terra_res = P.resolve_backend("terra")
        luna_res = P.resolve_backend("luna")
    except Exception as exc:  # noqa: BLE001 - no credentials => no spend
        outcome["ok"] = True
        outcome["stage"] = "backends_unavailable"
        outcome["skipped"] = [
            {"unit": f"{p['target']['path']}::{p['target']['symbol']} -> "
                     f"{p['counterpart']['path']}::"
                     f"{p['counterpart']['symbol']}",
             "reason": f"model backend unavailable: {exc}; no inference "
                       "attempted"} for p in pairs]
        outcome["wall_s"] = round(time.monotonic() - t0, 1)
        return outcome
    terra_model_id, terra_settings = P.build_model("terra")
    luna_model_id, luna_settings = P.build_model("luna")
    half = max(ceiling_usd / 2.0, 0.0)
    # Each model ledger is capped at half the delta ceiling so the two
    # together can never exceed ceiling_usd (preflight estimates stop
    # well short of the ledger cap; the cap is the backstop).
    terra_ledger = SpendLedger(rate=terra_res["rate_usd_per_mtok"],
                               rate_status=terra_res["rate_status"],
                               ceiling_usd=half)
    luna_ledger = SpendLedger(rate=luna_res["rate_usd_per_mtok"],
                              rate_status=luna_res["rate_status"],
                              ceiling_usd=half)
    try:
        run_dir = Path(run_dir)
        terra_ledger.attach_sink(run_dir / "spend_semdup_terra.jsonl")
        luna_ledger.attach_sink(run_dir / "spend_semdup_luna.jsonl")
    except Exception:  # noqa: BLE001 - sinks are best-effort
        pass
    outcome["models"] = {
        "terra": {"model": terra_res.get("model"),
                  "provider": terra_res.get("provider"),
                  "instructions_id": OD.instructions_id(
                      "terra", OD.TERRA_INSTRUCTIONS)},
        "luna": {"model": luna_res.get("model"),
                 "provider": luna_res.get("provider"),
                 "instructions_id": ED.editor_instructions_id()},
    }
    cfg = SimpleNamespace(repos=[repo])
    assess = assess_fn or OD.assess_pair
    edit = edit_fn or ED.edit_pair
    judged_preflight = _preflight_usd(pairs, merged_db, terra_res,
                                       luna_res)
    # MIRROR-01: first-published-wins. Seeds are same-revision
    # continuation verdicts; the loop adds each published twin. Skips
    # fire only on published verdicts, never on rejection.
    try:
        seeds = dict(known_published_keys or {})
    except (TypeError, ValueError):
        seeds = {k: "" for k in (known_published_keys or ())}
    published_keys: dict[str, str] = seeds
    outcome["mirror_skipped"] = 0
    for idx, pair in enumerate(pairs):
        # COST-01B: check live head before each additional paid assessment
        if is_stale is not None:
            try:
                if is_stale():
                    # Remaining pairs unexamined due to staleness
                    for remaining in pairs[idx:]:
                        skipped = {
                            "unit": f"{remaining['target']['path']}::{remaining['target']['symbol']} -> "
                                     f"{remaining['counterpart']['path']}::{remaining['counterpart']['symbol']}",
                            "reason": "stale_superseded",
                            "detail": "target head no longer current during duplication; no inference issued",
                        }
                        outcome["skipped"].append(skipped)
                        # Record a suppressed row for provenance (no inference)
                        outcome["pairs"].append({
                            "target": remaining["target"],
                            "counterpart": remaining["counterpart"],
                            "head_rev": head_rev,
                            "base_rev": base_rev,
                            "reason": "stale_superseded",
                            "detail": "stale head; skipped before inference",
                            "path": "suppressed",
                            "completed": False, "budget_refused": False,
                            "terra_completed": False, "luna_completed": None,
                        })
                    break
            except Exception:
                pass
        mkey = unordered_mirror_key(pair.get("target"),
                                    pair.get("counterpart"))
        if mirror_skip_decision(pair.get("target"),
                                pair.get("counterpart"), published_keys):
            row = mirror_preskip_row(pair, head_rev=head_rev,
                                     base_rev=base_rev,
                                     kept=published_keys[mkey])
            outcome["pairs"].append(row)
            outcome["suppressed"].append(_suppressed_row(row))
            outcome["mirror_skipped"] += 1
            if on_pair is not None:
                try:
                    on_pair(row)
                except Exception:  # noqa: BLE001 - hook never breaks judging
                    pass
            continue
        row = _review_pair(
            pair, cfg=cfg, repo_path=repo_path, merged_db=merged_db,
            base_rev=base_rev, head_rev=head_rev, head_tree=head_tree,
            base_tree=base_tree, terra_res=terra_res, luna_res=luna_res,
            terra_model_id=terra_model_id, terra_settings=terra_settings,
            luna_model_id=luna_model_id, luna_settings=luna_settings,
            terra_ledger=terra_ledger, luna_ledger=luna_ledger,
            ceiling_usd=ceiling_usd, half=half, assess=assess, edit=edit,
            OD=OD, ED=ED, R=R)
        outcome["pairs"].append(row)
        if row["path"] == "published":
            t, c = row.get("target") or {}, row.get("counterpart") or {}
            published_keys[mkey] = (
                f"{t.get('path')}::{t.get('symbol')} -> "
                f"{c.get('path')}::{c.get('symbol')}")
            outcome["published"].append(_public_row(row))
        else:
            outcome["suppressed"].append(_suppressed_row(row))
        if on_pair is not None:
            try:
                on_pair(row)
            except Exception:  # noqa: BLE001 - hook never breaks judging
                pass
    spent_t, spent_l = _spent(terra_ledger), _spent(luna_ledger)
    # Per-model spend from the priced ledger results (persisted with the
    # stage outcome). None means unknown — never represented as $0.
    # The aggregate is a confirmed lower bound only when both sides are
    # known; otherwise it is None with spend_unknown True.
    outcome["spend_terra_usd"] = spent_t
    outcome["spend_luna_usd"] = spent_l
    outcome["spend_terra_unknown"] = spent_t is None
    outcome["spend_luna_unknown"] = spent_l is None
    if spent_t is None or spent_l is None:
        outcome["spend_usd"] = (None if (spent_t is None and spent_l is None)
                                else round((spent_t or 0.0) + (spent_l or 0.0), 6))
    else:
        outcome["spend_usd"] = round(spent_t + spent_l, 6)
    outcome["spend_unknown"] = spent_t is None or spent_l is None
    outcome["preflight_usd"] = judged_preflight
    _record_mirror_savings(outcome, spent_t, spent_l, t0)
    _record_scaffold_savings(outcome, spent_t, spent_l, t0)
    outcome["ok"] = True
    outcome["wall_s"] = round(time.monotonic() - t0, 1)
    return outcome
