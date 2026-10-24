"""M9.2 P2: deterministic trace failure taxonomy (no inference).

Computes "touched the decisive location" from evidence ledgers
(path + line overlap) for every (run, reference item) pair with matching
repo across M7, M8, M9/M9.1 traces. Judgement labels (connected vs not,
wrong mechanism, suppressed) are computed by explicit keyword rules and
recorded with the trace text they rest on — never inferred reasoning.

Outputs: eval/M9_2_TRACE_TAXONOMY.json (one row per pair) and the .md
summary (written by hand from the JSON, not by this script).
"""

import json
from pathlib import Path

REF = [
    {"id": "FW3-median", "repo": "mitchins/frigate-watchdog",
     "locs": [{"path": "src/frigate_watchdog/store.py", "s": 845, "e": 846}],
     "keywords": ["median", "durations", "outage_median", "upper middle"],
     "materiality": "minor"},
    {"id": "FW3-reboots", "repo": "mitchins/frigate-watchdog",
     "locs": [{"path": "src/frigate_watchdog/store.py", "s": 831, "e": 836},
              {"path": "src/frigate_watchdog/service.py", "s": 360, "e": 385}],
     "keywords": ["reboots_sent", "reservation", "reserved", "dispatched"],
     "materiality": "moderate-high"},
    {"id": "FW3-restart", "repo": "mitchins/frigate-watchdog",
     "locs": [{"path": "src/frigate_watchdog/policy.py", "s": 200, "e": 215},
              {"path": "src/frigate_watchdog/policy.py", "s": 520, "e": 540}],
     "keywords": ["restart", "SELF_RECOVERED", "outage_started_utc", "attribution"],
     "materiality": "moderate"},
    {"id": "L08-lms", "repo": "Smart-Compliance-Systems/scs-infra",
     "locs": [{"path": "servers/nonprod/haproxy/scs-edge-01.haproxy.cfg",
               "s": 250, "e": 262}],
     "keywords": ["lms", ":443", ":9443", "vhost"],
     "materiality": "moderate-high"},
    {"id": "L08-epcore", "repo": "Smart-Compliance-Systems/scs-infra",
     "locs": [{"path": "servers/nonprod/haproxy/scs-edge-01.haproxy.cfg",
               "s": 175, "e": 190}],
     "keywords": ["epcore", "9443", "default vhost", "wrong application"],
     "materiality": "moderate-high"},
    {"id": "L08-403", "repo": "Smart-Compliance-Systems/scs-infra",
     "locs": [{"path": "servers/nonprod/haproxy/scs-edge-01.haproxy.cfg",
               "s": 70, "e": 80},
              {"path": "servers/nonprod/haproxy/scs-edge-01.haproxy.cfg",
               "s": 124, "e": 130}],
     "keywords": ["403", "remove_item", "_wpnonce", "cart", "WooCommerce"],
     "materiality": "moderate-high", "note": "partial: exact-path caveat"},
    {"id": "L06-trim", "repo": "My-Day-My-Way/my-day-my-way-ios",
     "locs": [{"path": "MyDayMyWay/MyDayMyWay/Modules/InAppPurchase/Models/Subscription/SubscriptionPlanCatalogueEntry.swift", "s": 28, "e": 36},
              {"path": "MyDayMyWay/MyDayMyWay/Modules/InAppPurchase/Models/Subscription/SubscriptionPlanOption.swift", "s": 55, "e": 68}],
     "keywords": ["trim", "whitespace", "join", "catalogue"],
     "materiality": "low-moderate"},
    {"id": "KL1-isolation", "repo": "mitchins/kinglet-lms",
     "locs": [{"path": "tools/run_deployed_probe.py", "s": 84, "e": 145}],
     "keywords": ["database_id", "isolation", "disposable", "binding"],
     "materiality": "high"},
    {"id": "KL1-readme", "repo": "mitchins/kinglet-lms",
     "locs": [{"path": "README.md", "s": 55, "e": 67}],
     "keywords": ["48/32", "67/73", "stale", "ledger"],
     "materiality": "moderate"},
    {"id": "RB7-preview", "repo": "mitchins/rough-bench",
     "locs": [{"path": "scripts/build_docs_data.py", "s": 330, "e": 345}],
     "keywords": ["preview", "leaderboard", "enforcement", "policy"],
     "materiality": "moderate-high"},
    {"id": "RB7-compat", "repo": "mitchins/rough-bench",
     "locs": [{"path": "roughbench/openai_compat.py", "s": 50, "e": 60}],
     "keywords": ["chat_template_kwargs", "third key", "undocumented"],
     "materiality": "moderate"},
    {"id": "L10-evidenceTier", "repo": "mitchins/needletail-harness",
     "locs": [{"path": "Sources/NeedletailTestSupport/CaseLedger.swift",
               "s": 220, "e": 250}],
     "keywords": ["evidenceTier", "evidence_tier", "decod", "old JSON",
                  "missing key"],
     "materiality": "low"},
    {"id": "L10-tier-trap", "repo": "mitchins/needletail-harness",
     "locs": [{"path": "Sources/NeedletailTestSupport/CaseLedger.swift",
               "s": 294, "e": 302}],
     "keywords": ["integration", "pure", "continue", "satisfies"],
     "materiality": "trap:intended behaviour, correct answer is no finding",
     "trap": True},
]

INCOMPLETE = {"incomplete_budget_exhausted", "incomplete_no_tool_use",
              "incomplete", "timeout"}

TRAP_DEFECT_WORDS = ["satisfy", "satisfies", "permit", "allow", "break",
                     "fail", "bypass", "violat", "incorrect", "wrong", "bug",
                     "defect", "should not", "regression", "concern"]


def load_runs():
    runs = []
    # M7 traces (no run.json; case encoded in dir name)
    m7 = Path("/data/coding/codatiel/codatiel-i2/eval/M7_TRACES")
    if m7.exists():
        for d in sorted(m7.iterdir()):
            if d.is_dir():
                runs.append({"trace_id": f"M7/{d.name}", "dir": d,
                             "repo": None, "status": None, "variant": None,
                             "model": None})
    # M8 traces
    m8 = Path("/data/coding/codatiel/codatiel-i2/eval/M8_REAL_REVIEWS/traces")
    if m8.exists():
        for d in sorted(m8.iterdir()):
            if d.is_dir():
                runs.append({"trace_id": f"M8/{d.name}", "dir": d,
                             "repo": None, "status": None, "variant": None,
                             "model": None})
    # M9/M9.1 traces (luna + gemma + haiku)
    for tag, root in (("M9-luna", "/tmp/m9_test_luna/runs"),
                      ("M9-gemma", "/tmp/m9_gemma/runs"),
                      ("M9-haiku", "/tmp/m9_haiku/runs")):
        r = Path(root)
        if r.exists():
            for rd in sorted(r.rglob("run.json")):
                runs.append({"trace_id": f"{tag}/{rd.parent.parent.name}/{rd.parent.name}",
                             "dir": rd.parent, "repo": None, "status": None,
                             "variant": None, "model": tag})
    # Fill repo/status/variant/model from run.json or metrics.json
    for run in runs:
        d = run["dir"]
        rj = d / "run.json"
        mj = d / "metrics.json"
        if rj.exists():
            try:
                r = json.loads(rj.read_text())
                run["repo"] = r.get("repo")
                run["base_sha"] = (r.get("base_sha") or "")[:8]
                run["head_sha"] = (r.get("head_sha") or "")[:8]
                cfg = r.get("config", {})
                run["variant"] = cfg.get("variant")
                run["model"] = run["model"] or cfg.get("model")
                run["shas"] = ((r.get("base_sha") or "")[:12],
                               (r.get("head_sha") or "")[:12])
            except Exception:
                pass
        if mj.exists():
            try:
                m = json.loads(mj.read_text())
                run["status"] = m.get("status")
                run["tool_calls"] = m.get("tool_calls")
                run["variant"] = run["variant"] or m.get("variant")
            except Exception:
                pass
        if run["repo"] is None:
            run["repo"] = _repo_from_trace_id(run["trace_id"])
        else:
            run["repo"] = _normalize_repo(run["repo"])
    return runs


def _normalize_repo(repo):
    if not repo:
        return None
    low = repo.lower()
    if "needletail" in low:
        return "mitchins/needletail-harness"
    if "frigate" in low:
        return "mitchins/frigate-watchdog"
    if "scanweave" in low:
        return "mitchins/scanweave"
    if "rough" in low:
        return "mitchins/rough-bench"
    if "kinglet" in low:
        return "mitchins/kinglet-lms"
    if "wall" in low:
        return "mitchins/wall-tidbits"
    if "scs" in low or "compliance" in low:
        return "Smart-Compliance-Systems/scs-infra"
    if "justcards" in low:
        return "mitchins/justcards-app"
    if "selective" in low:
        return "mitchins/selectivetutor-website"
    if "my-day" in low or "myday" in low:
        return "My-Day-My-Way/my-day-my-way-ios"
    return repo


def _repo_from_trace_id(tid):
    low = tid.lower()
    if "fw3" in low or "frigate" in low:
        return "mitchins/frigate-watchdog"
    if "f-01" in low or "f_01" in low:
        return "F-01-control"
    if "l10" in low or "needletail" in low and ("o-0" in low or "l10" in low):
        return "mitchins/needletail-harness"
    if "l01" in low:
        return "mitchins/needletail-harness"
    if "sw7" in low:
        return "mitchins/scanweave"
    if "sw6" in low:
        return "mitchins/scanweave"
    if "rb7" in low:
        return "mitchins/rough-bench"
    if "kl1" in low:
        return "mitchins/kinglet-lms"
    if "wt4" in low:
        return "mitchins/wall-tidbits"
    return None


def evidence_touches(d, loc):
    """Deterministic overlap: same path, overlapping lines (any revision)."""
    try:
        lines = (d / "evidence.jsonl").read_text().splitlines()
    except FileNotFoundError:
        return []
    hits = []
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("path") != loc["path"]:
            continue
        s, en = e.get("line_start"), e.get("line_end")
        if s is None or en is None:
            continue
        if s <= loc["e"] and en >= loc["s"]:
            hits.append({"evidence_id": e.get("evidence_id"),
                         "revision": e.get("revision"),
                         "revision_sha": (e.get("revision_sha") or "")[:8],
                         "span": [s, en]})
    return hits


def model_findings(d):
    p = d / "model_findings_raw.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def finding_overlaps(f, locs):
    """Path AND line overlap with a decisive span."""
    hits = []
    for loc in locs:
        if f.get("path") != loc["path"]:
            continue
        s = f.get("line_start", f.get("line", 0)) or 0
        e = f.get("line_end", f.get("line", 0)) or 0
        try:
            s, e = int(s), int(e)
        except (TypeError, ValueError):
            continue
        if s <= loc["e"] and e >= loc["s"]:
            hits.append(loc)
    return hits


def final_summary_text(d):
    """Only the model's own conclusion: silence summary or finding texts.

    Detector dashboards are excluded — they list noise the model never
    endorsed, which would fake 'connection'.
    """
    bits = []
    try:
        sh = json.loads((d / "shadow_result.json").read_text())
        sil = sh.get("silence") or {}
        if sil.get("summary"):
            bits.append(str(sil["summary"]))
    except (FileNotFoundError, ValueError):
        pass
    for f in model_findings(d):
        bits.append(str(f.get("title", "")) + " " + str(f.get("claim", "")))
    return "\n".join(bits)


# Titles adjudicated REAL_LOW_VALUE / intended-behaviour in M8/M9.1.
KNOWN_LOW_TITLES = [
    "phased recovery", "authentication failures", "group-wide inhibition",
    "introduction of evidence tiering", "resolve_entities",
    "checkpoint strings", "checkpoint metadata",
    "documentation generation script",
]


def classify_false_finding(f, items):
    # Trap-site overlap asserting a defect -> intended behaviour misread.
    text = (str(f.get("title", "")) + " "
            + str(f.get("claim", ""))).lower()
    for item in items:
        if not item.get("trap"):
            continue
        if finding_overlaps(f, item["locs"]) and any(
                w in text for w in TRAP_DEFECT_WORDS):
            return ("INTENDED_BEHAVIOUR_MISREAD",
                    "finding at the intentional-behaviour site asserts a "
                    "defect")
    if any(k in text for k in KNOWN_LOW_TITLES):
        return ("OVERSTATED_MATERIALITY",
                "adjudicated REAL_LOW_VALUE: accurate restatement, no defect")
    ev = f.get("evidence_ids") or []
    if not ev:
        return ("UNSUPPORTED_CLAIM", "no tool evidence cited")
    return ("UNSUPPORTED_CLAIM",
            "default: overlaps no decisive span; needs adjudication")


def classify(run, item):
    d = run["dir"]
    touches = []
    for loc in item["locs"]:
        touches += evidence_touches(d, loc)
    overlapping = []
    for f in model_findings(d):
        if finding_overlaps(f, item["locs"]):
            overlapping.append(f)
    status = run.get("status") or ""
    row = {"trace": run["trace_id"], "repo": run.get("repo"),
           "model": run.get("model"), "variant": run.get("variant"),
           "status": status, "tool_calls": run.get("tool_calls"),
           "run_shas": list(run.get("shas") or [None, None]),
           "item": item["id"], "touched": bool(touches),
           "touch_evidence": touches[:4],
           "n_finding_overlap": len(overlapping)}
    if item.get("trap"):
        if overlapping:
            texts = " ".join(
                str(f.get("title", "")) + " " + str(f.get("claim", ""))
                for f in overlapping).lower()
            if any(w in texts for w in TRAP_DEFECT_WORDS):
                row["class"] = "INTENDED_BEHAVIOUR_MISREAD"
                row["judgement"] = ("finding at the intentional-behaviour "
                                    "site asserts a defect; adjudicated "
                                    "false positive")
            else:
                row["class"] = "EXAMINED_NOT_CONNECTED"
                row["judgement"] = ("judgement: summary overlaps the trap "
                                    "site without asserting a defect")
                row["unmatched_overlap_titles"] = [
                    f.get("title") for f in overlapping]
        else:
            row["class"] = "CORRECT_NON_FINDING"
            row["judgement"] = "no finding at trap site (correct)"
        return row
    # FOUND requires path+line overlap AND mechanism language (keyword in
    # title+claim). Overlap alone is EXAMINED_NOT_CONNECTED.
    confirmed = []
    for f in overlapping:
        text = (str(f.get("title", "")) + " " + str(f.get("claim", ""))).lower()
        kw = [k for k in item["keywords"] if k.lower() in text]
        if kw:
            confirmed.append((f, kw))
    if confirmed:
        row["class"] = "FOUND"
        row["finding_titles"] = [f.get("title") for f, _ in confirmed]
        row["matched_keywords"] = sorted({k for _, ks in confirmed for k in ks})
        row["judgement"] = ("finding overlaps decisive span with mechanism "
                            "language; mechanism confirmed by M8/M9.1 "
                            "adjudication for this item")
        return row
    if overlapping:
        row["unmatched_overlap_titles"] = [f.get("title") for f in overlapping]
    if status in INCOMPLETE and not touches:
        row["class"] = "BUDGET_EXHAUSTED_BEFORE"
        row["judgement"] = "deterministic: incomplete status, no touch"
        return row
    if touches or overlapping:
        # Judgement: does the model's own conclusion mention the consequence?
        text = final_summary_text(d).lower()
        hits = [k for k in item["keywords"] if k.lower() in text]
        same_file_filed = any(
            f.get("path") in [loc["path"] for loc in item["locs"]]
            for f in model_findings(d))
        if hits and not overlapping and not same_file_filed:
            row["class"] = "CONNECTED_SUPPRESSED"
            row["judgement"] = ("judgement: decisive location read and "
                                f"consequence language present {hits} in the "
                                "model's own conclusion, but no finding filed")
            row["keyword_hits"] = hits
        else:
            row["class"] = "EXAMINED_NOT_CONNECTED"
            row["judgement"] = ("judgement: decisive location read (or "
                                "overlapped without mechanism language)"
                                + ("; connected instead to a different defect "
                                   "in the same file" if same_file_filed else "")
                                + ", no consequence language in the model's "
                                "conclusion")
        return row
    row["class"] = "NEVER_EXAMINED"
    row["judgement"] = "deterministic: no evidence touches decisive span"
    return row


def main():
    runs = load_runs()
    rows = []
    for run in runs:
        for item in REF:
            if run.get("repo") != item["repo"]:
                continue
            rows.append(classify(run, item))
    # SHA-consistency audit for M9 runs
    for run in runs:
        if not run["trace_id"].startswith("M9-"):
            continue
        d = run["dir"]
        rj = d / "run.json"
        if not rj.exists():
            continue
        r = json.loads(rj.read_text())
        ev_shas = set()
        try:
            for line in (d / "evidence.jsonl").read_text().splitlines():
                if line.strip():
                    e = json.loads(line)
                    if e.get("revision_sha"):
                        ev_shas.add(e["revision_sha"][:8])
        except FileNotFoundError:
            pass
        if ev_shas and (r.get("base_sha", "")[:8] not in ev_shas
                        and r.get("head_sha", "")[:8] not in ev_shas):
            for row in rows:
                if row["trace"] == run["trace_id"]:
                    row["sha_note"] = "evidence SHAs do not include run SHAs"
    # False/refuted findings: every model finding not FOUND for any item.
    found_ids = set()
    for row in rows:
        if row["class"] == "FOUND":
            for t in row.get("finding_titles", []):
                found_ids.add((row["trace"], t))
    false_rows = []
    for run in runs:
        d = run["dir"]
        for f in model_findings(d):
            if (run["trace_id"], f.get("title")) in found_ids:
                continue
            cls, why = classify_false_finding(f, REF)
            false_rows.append({"trace": run["trace_id"],
                               "repo": run.get("repo"),
                               "model": run.get("model"),
                               "variant": run.get("variant"),
                               "title": f.get("title"),
                               "path": f.get("path"),
                               "class": cls, "judgement": why})
    Path("/tmp/codatiel-public/eval/M9_2_TRACE_TAXONOMY.json").write_text(
        json.dumps({"schema_version": "m9.2.taxonomy.v1",
                    "reference_items": REF, "rows": rows,
                    "false_findings": false_rows}, indent=1))
    print(f"{len(rows)} pairs, {len(false_rows)} non-reference findings")


if __name__ == "__main__":
    main()
