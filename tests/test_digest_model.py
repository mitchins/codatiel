"""DIGEST-01: delivery-independent digest model + renderers.

No inference. Exercises: finding preservation (title/claim/evidence
from validated artifacts, never invented), HTML escaping + link
integrity, plain-text parity, Markdown rendering, zero/partial/malformed
cases, duplicate-delivery prevention lives in test_semdup_stage.
"""

import json
from pathlib import Path

from codatiel_i2 import digest_model as DM


def _run(tmp_path: Path, *, advisory, sem=None, metrics=None,
         evidence=(), bind_pr: int | None = 7) -> Path:
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)
    (d / "run.json").write_text(json.dumps({
        "run_id": "r1", "repo": "o/r",
        "base_sha": "a" * 40, "head_sha": "b" * 40,
        "config": {"model": "terra:t"}}))
    (d / "shadow_result.json").write_text(json.dumps({
        "status": "complete", "counts": {"advisory_grounded": len(advisory)},
        "advisory": advisory}))
    (d / "metrics.json").write_text(json.dumps(metrics or {
        "status": "complete", "model": "terra:t",
        "spend": {"usd_estimate": 0.01}}))
    if evidence:
        (d / "evidence.jsonl").write_text(
            "\n".join(json.dumps(e) for e in evidence) + "\n")
    if sem is not None:
        (d / "semdup.json").write_text(json.dumps(sem))
    if bind_pr is not None:
        # IDENTITY-01: context-labelled renders require a bound run.
        from codatiel_i2 import review_identity as _RI
        _RI.bind_run(d, repo_slug="o/r", pr_number=bind_pr,
                     base_sha="a" * 40, head_sha="b" * 40, run_id="r1",
                     binder="test")
    return d


def _adv(n="agent-000", **kw):
    base = {
        "finding_id": n, "detector": "contracts",
        "disposition": "advisory_grounded",
        "substantive_assessment": "UNASSESSED",
        "publish_eligible": False,
        "bad_outcome": "The widget leaks the token on retry.",
        "evidence_ids": ["ev1"],
        "changed_anchor": {"path": "src/w.py", "line_start": 10,
                           "line_end": 14},
        "raw": {"title": "Token leak on retry", "claim": "same",
                "path": "src/w.py", "line_start": 10, "line_end": 14,
                "detector": "contracts", "revision": "head",
                "evidence_ids": ["ev1"]},
    }
    base.update(kw)
    return base


def _ev(eid="ev1", **kw):
    base = {"evidence_id": eid, "path": "src/w.py", "line_start": 10,
            "line_end": 14, "revision": "head",
            "revision_sha": "b" * 40,
            "excerpt": "token = cache.get(key)  # retry reuses it"}
    base.update(kw)
    return base


def test_finding_preserves_validated_content(tmp_path: Path):
    d = _run(tmp_path, advisory=[_adv()], evidence=[_ev()])
    m = DM.build_review_model(d, context="dev:o/r#7")
    assert m["repo_label"] == "o/r" and m["pr_number"] == 7
    assert m["pr_url"] == "https://github.com/o/r/pull/7"
    f = m["findings"][0]
    assert f["title"] == "Token leak on retry"
    assert "leaks the token" in f["problem"]
    assert f["url"] == ("https://github.com/o/r/blob/" + "b" * 40 +
                        "/src/w.py#L10-L14")
    assert "Do not treat this as a confirmed defect" in f["verification"]
    assert f["evidence"][0]["excerpt"].startswith("token = cache")


def test_html_escapes_untrusted_content(tmp_path: Path):
    evil = _adv(raw={"title": "<script>alert(1)</script>",
                     "claim": "x & y <z>",
                     "path": "src/w.py", "line_start": 1, "line_end": 1,
                     "detector": "contracts", "revision": "head",
                     "evidence_ids": []},
                bad_outcome="<b>bold</b> & \"quoted\"")
    d = _run(tmp_path, advisory=[evil])
    m = DM.build_review_model(d, context="dev:o/r#7")
    hype = DM.render_html(m)
    assert "<script>" not in hype and "&lt;script&gt;" in hype
    assert "<b>bold</b>" not in hype
    text = DM.render_text(m)
    assert "<script>alert(1)</script>" in text  # plain text needs no escape


def test_no_link_without_slug_or_sha(tmp_path: Path):
    d = _run(tmp_path, advisory=[_adv()], bind_pr=None)
    m = DM.build_review_model(d)  # no context: local path, no slug
    assert m["findings"][0]["url"] is None
    assert m["pr_url"] is None
    assert "github.com" not in DM.render_text(m)
    assert "github.com" not in DM.render_html(m)


def test_missing_evidence_labelled_not_invented(tmp_path: Path):
    d = _run(tmp_path, advisory=[_adv()])  # ev1 not in evidence.jsonl
    m = DM.build_review_model(d, context="dev:o/r#7")
    e = m["findings"][0]["evidence"][0]
    assert e == {"id": "ev1", "missing": True}
    assert "missing from the run evidence" in DM.render_text(m)


def test_untitled_finding_gets_explicit_placeholder(tmp_path: Path):
    adv = _adv(raw={"claim": "vague", "path": "x.py", "line_start": 1,
                    "line_end": 1, "evidence_ids": []})
    d = _run(tmp_path, advisory=[adv])
    m = DM.build_review_model(d, context="dev:o/r#7")
    assert m["findings"][0]["title"].startswith("Untitled")


def test_zero_findings_and_silence(tmp_path: Path):
    d = _run(tmp_path, advisory=[])
    m = DM.build_review_model(d, context="dev:o/r#7")
    assert m["n_findings"] == 0
    text = DM.render_text(m)
    assert "none surfaced" in text.lower()
    assert "no material findings" in m["overview"].lower() or \
        "silence" in m["overview"].lower()


def test_partial_coverage_never_claims_full(tmp_path: Path):
    sem = {"status": "ok", "coverage": "partial", "spend_usd": 0.01,
           "reason": "examined 2 targets, 4 pairs; 0 approved; 6 skipped",
           "targets": [], "pairs_examined": 4,
           "skipped": [{"unit": "a.py::f", "reason": "budget"}],
           "published": [], "suppressed": []}
    d = _run(tmp_path, advisory=[], sem=sem)
    m = DM.build_review_model(d, context="dev:o/r#7")
    text = DM.render_text(m)
    assert "Partial" in text and "not proof" in text
    assert "Complete:" not in text
    assert "NOT examined" in text or "Not examined" in text


def test_unavailable_semdup_explicit(tmp_path: Path):
    sem = {"status": "unavailable", "coverage": "unknown",
           "reason": "base index unavailable", "targets": [],
           "skipped": [], "published": [], "suppressed": [],
           "spend_usd": 0.0}
    d = _run(tmp_path, advisory=[], sem=sem)
    text = DM.render_text(DM.build_review_model(d, context="dev:o/r#7"))
    assert "Did not complete (unavailable)" in text
    assert "No reuse claim is made" in text


def test_text_html_parity(tmp_path: Path):
    sem = {"status": "ok", "coverage": "partial", "spend_usd": 0.01,
           "reason": "examined 1 targets, 1 pairs; 0 approved; 1 skipped",
           "targets": [], "pairs_examined": 1,
           "skipped": [{"unit": "a.py::f", "reason": "budget"}],
           "published": [], "suppressed": []}
    d = _run(tmp_path, advisory=[_adv()], sem=sem, evidence=[_ev()])
    m = DM.build_review_model(d, context="dev:o/r#7")
    text, hype, md = (DM.render_text(m), DM.render_html(m),
                      DM.render_markdown(m))
    for needle in ("Token leak on retry", "src/w.py:10-14",
                   "Needs human review", "ev1",
                   "Partial", "Diagnostics reference", "r1"):
        assert needle in text, needle
        assert needle in hype or needle.replace(":", " gal") in hype, needle
    assert "<html" in hype and "</html>" in hype
    assert "## Findings" in md and "### 1. Token leak on retry" in md
    assert "https://github.com/o/r/pull/7" in md


def test_malformed_advisory_entries_skipped(tmp_path: Path):
    d = _run(tmp_path, advisory=["junk", None, 42, _adv()])
    m = DM.build_review_model(d, context="dev:o/r#7")
    assert m["n_findings"] == 1  # only the dict survives


def test_overview_groups_many_published_by_area(tmp_path: Path):
    pubs = [{
        "target": {"path": f"src/{chr(97 + i)}.py", "symbol": f"f{i}"},
        "counterpart": {"path": "src/old.py", "symbol": "g"},
        "decision": "KEEP", "revised_comment": "reuse", "reason": "",
    } for i in range(5)]
    pubs[3]["target"]["path"] = "src/a.py"
    pubs[4]["target"]["path"] = "src/a.py"
    sem = {"status": "ok", "coverage": "full", "spend_usd": 0.05,
           "reason": "examined 5 targets, 5 pairs; 5 approved",
           "targets": [], "pairs_examined": 5,
           "skipped": [], "published": pubs, "suppressed": []}
    d = _run(tmp_path, advisory=[], sem=sem)
    m = DM.build_review_model(d, context="dev:o/r#7")
    assert "span 3 files" in m["overview"]
    assert "a.py (3)" in m["overview"]


def test_opportunity_groups_connected_pairs(tmp_path: Path):
    pubs = [
        {"target_ref": "a.py:1-5", "target_symbol": "f",
         "counterpart_ref": "b.py:1-5", "counterpart_symbol": "g",
         "decision": "KEEP", "comment": "c1", "differences": ""},
        {"target_ref": "b.py:1-5", "target_symbol": "g",
         "counterpart_ref": "c.py:1-5", "counterpart_symbol": "h",
         "decision": "KEEP", "comment": "c2", "differences": ""},
        {"target_ref": "z.py:1-5", "target_symbol": "q",
         "counterpart_ref": "y.py:1-5", "counterpart_symbol": "w",
         "decision": "KEEP", "comment": "c3", "differences": ""},
    ]
    groups = DM._group_opportunities(pubs)
    assert len(groups) == 2
    assert sorted(groups[0]["pair_idxs"]) == [0, 1]
    assert groups[1]["pair_idxs"] == [2]
    assert "a.py::f" in groups[0]["units"]


def test_grouped_email_lists_every_pair_once(tmp_path: Path):
    pubs = [
        {"target": {"path": "a.py", "symbol": "f"},
         "counterpart": {"path": "b.py", "symbol": "g"},
         "head_rev": "h", "decision": "KEEP",
         "revised_comment": "cc1", "reason": "",
         "anchors": {"target": {"path": "a.py", "start_line": 1,
                                "end_line": 5},
                     "counterpart": {"path": "b.py", "start_line": 1,
                                     "end_line": 5}}},
        {"target": {"path": "b.py", "symbol": "g"},
         "counterpart": {"path": "c.py", "symbol": "h"},
         "head_rev": "h", "decision": "REVISE",
         "revised_comment": "cc2", "reason": "d2",
         "anchors": {"target": {"path": "b.py", "start_line": 1,
                                "end_line": 5},
                     "counterpart": {"path": "c.py", "start_line": 1,
                                     "end_line": 5}}},
    ]
    sem = {"status": "ok", "coverage": "full", "spend_usd": 0.01,
           "reason": "examined 2 targets, 2 pairs; 2 approved",
           "targets": [], "pairs_examined": 2,
           "skipped": [], "published": pubs, "suppressed": []}
    d = _run(tmp_path, advisory=[], sem=sem)
    m = DM.build_review_model(d, context="dev:o/r#7")
    text = DM.render_text(m)
    assert "Shared reuse opportunity (2 supporting pairs)" in text
    assert text.count("may reuse") == 2
    assert "cc1" in text and "cc2" in text and "d2" in text
    html = DM.render_html(m)
    assert "Shared reuse opportunity" in html
    assert html.count("may reuse") == 2
