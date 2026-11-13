"""PROD-01 Track A, task A1: editorial cleanup of the digest model.

No inference. Exercises the stored-artifact shapes (partial semdup
with approved reuse, complete semdup with reuse) plus the real DEV
run dirs for cart-ray #80 and needletail-harness #10 when present
(skipped otherwise):

- partial examinations that approved reuse suggestions must never
  claim "nothing found";
- overviews distinguish ordinary correctness findings from
  reuse/refactoring opportunities;
- overlapping reuse suggestions collapse into distinct opportunities
  with every location preserved;
- confidence/coverage labels stay accurate;
- HTML/Markdown/text renderers stay aligned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codatiel_i2 import digest_model as DM

A40 = "a" * 40
B40 = "b" * 40


def _run(tmp_path: Path, *, advisory, sem=None, metrics=None,
         evidence=(), bind_pr: int | None = 7) -> Path:
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)
    (d / "run.json").write_text(json.dumps({
        "run_id": "r1", "repo": "o/r",
        "base_sha": A40, "head_sha": B40,
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
        from codatiel_i2 import review_identity as _RI
        _RI.bind_run(d, repo_slug="o/r", pr_number=bind_pr,
                     base_sha=A40, head_sha=B40, run_id="r1",
                     binder="test")
    return d


def _adv(n="agent-000", path="src/w.py", start=10, end=14,
         title="Token leak on retry"):
    return {
        "finding_id": n, "detector": "contracts",
        "disposition": "advisory_grounded",
        "substantive_assessment": "UNASSESSED",
        "publish_eligible": False,
        "bad_outcome": "The widget leaks the token on retry.",
        "evidence_ids": ["ev1"],
        "changed_anchor": {"path": path, "line_start": start,
                           "line_end": end},
        "raw": {"title": title, "claim": "same",
                "path": path, "line_start": start, "line_end": end,
                "detector": "contracts", "revision": "head",
                "evidence_ids": ["ev1"]},
    }


def _pub(target="a.py::f", counterpart="b.py::g", decision="REVISE",
         comment="share the helper"):
    t_path, _, t_sym = target.partition("::")
    c_path, _, c_sym = counterpart.partition("::")
    return {
        "target": {"path": t_path, "symbol": t_sym},
        "counterpart": {"path": c_path, "symbol": c_sym},
        "head_rev": "h", "decision": decision,
        "revised_comment": comment, "reason": "overlap",
        "anchors": {
            "target": {"path": t_path, "start_line": 1, "end_line": 5},
            "counterpart": {"path": c_path, "start_line": 1,
                            "end_line": 5}}}


def _needletail_like_sem():
    """The needletail #10 shape: coverage partial, no skipped rows, a
    stored budget-guard reason, and approved reuse suggestions."""
    return {"status": "ok", "coverage": "partial", "spend_usd": 0.05,
            "reason": "examined 149 targets, 292 pairs; 13 approved; "
                      "190 pair(s) unexamined by the budget guard",
            "targets": [], "pairs_examined": 292,
            "skipped": [],
            "published": [_pub(f"a{i}.py::f{i}", "b.py::g",
                               comment=f"share helper {i}")
                          for i in range(3)],
            "suppressed": []}


def test_partial_with_reuse_never_claims_nothing_found(tmp_path: Path):
    d = _run(tmp_path, advisory=[], sem=_needletail_like_sem())
    m = DM.build_review_model(d, context="dev:o/r#7")
    state = m["semdup"]["state"]
    assert "Partial" in state
    assert "3 reuse suggestions approved below" in state
    assert "Nothing found" not in state
    assert "unexamined code may still contain" in state
    # The stored budget-guard reason is surfaced, not swallowed.
    assert "budget guard" in state
    # The overview agrees: no correctness-silence claim, reuse named.
    assert "no correctness findings" in m["overview"]
    assert "13 approved" not in m["overview"]
    assert "3 reuse" in m["overview"]
    assert "Nothing found" not in m["overview"]


def test_partial_without_reuse_keeps_nothing_found_caveat(tmp_path: Path):
    sem = {"status": "ok", "coverage": "partial", "spend_usd": 0.01,
           "reason": "examined 2 targets, 4 pairs; 0 approved",
           "targets": [], "pairs_examined": 4,
           "skipped": [{"unit": "a.py::f", "reason": "budget"}],
           "published": [], "suppressed": []}
    d = _run(tmp_path, advisory=[], sem=sem)
    m = DM.build_review_model(d, context="dev:o/r#7")
    assert "Nothing found in the examined code" in m["semdup"]["state"]
    assert "is not proof" in m["semdup"]["state"]


def test_overview_separates_correctness_from_reuse(tmp_path: Path):
    sem = {"status": "ok", "coverage": "full", "spend_usd": 0.05,
           "reason": "examined 2 targets, 2 pairs; 1 approved",
           "targets": [], "pairs_examined": 1,
           "skipped": [], "published": [_pub()], "suppressed": []}
    d = _run(tmp_path, advisory=[_adv()], sem=sem,
             evidence=[{"evidence_id": "ev1", "path": "src/w.py",
                        "line_start": 10, "line_end": 14,
                        "revision": "head", "revision_sha": B40,
                        "excerpt": "token = cache.get(key)"}])
    m = DM.build_review_model(d, context="dev:o/r#7")
    assert "1 correctness candidate" in m["overview"]
    assert "reuse/refactoring suggestion" in m["overview"]
    assert "not correctness defects" in m["overview"]
    assert m["n_findings"] == 1 and m["n_reuse"] == 1


def test_overlapping_suggestions_collapse_preserving_locations(
        tmp_path: Path):
    # Two pairs sharing unit b.py::g are one opportunity; disjoint pair
    # is another. Every pair still renders with its own comment.
    sem = {"status": "ok", "coverage": "full", "spend_usd": 0.05,
           "reason": "examined 3 targets, 3 pairs; 3 approved",
           "targets": [], "pairs_examined": 3, "skipped": [],
           "published": [
               _pub("a.py::f", "shared.py::g", comment="first"),
               _pub("z.py::h", "shared.py::g", comment="second"),
               _pub("q.py::i", "w.py::j", comment="third")],
           "suppressed": []}
    d = _run(tmp_path, advisory=[], sem=sem)
    m = DM.build_review_model(d, context="dev:o/r#7")
    assert m["n_reuse"] == 3
    assert m["n_opportunities"] == 2
    text = DM.render_text(m)
    assert "distinct opportunities" in m["overview"]
    assert "2 distinct opportunities" in m["overview"]
    assert text.count("may reuse") == 3
    for needle in ("first", "second", "third"):
        assert needle in text
    html = DM.render_html(m)
    assert html.count("may reuse") == 3
    assert "Shared reuse opportunity (2 supporting pairs)" in text
    md = DM.render_markdown(m)
    assert md.count("may reuse") == 3


def test_renderers_stay_aligned(tmp_path: Path):
    sem = _needletail_like_sem()
    d = _run(tmp_path, advisory=[_adv()], sem=sem)
    m = DM.build_review_model(d, context="dev:o/r#7")
    text, html, md = (DM.render_text(m), DM.render_html(m),
                      DM.render_markdown(m))
    for needle in ("Token leak on retry", "Needs human review",
                   "Partial", "Diagnostics reference", "r1",
                   "reuse suggestions approved below"):
        assert needle in text, needle
        assert needle in html or "gal" in needle, needle
        assert needle in md, needle
    # Suppressed/skipped glosses agree across renderers.
    sem2 = dict(sem)
    sem2["suppressed"] = [{"target": {"path": "a.py", "symbol": "f"},
                           "counterpart": {"path": "b.py", "symbol": "g"},
                           "reason": "mirror_of_published",
                           "detail": ""}]
    sem2["skipped"] = [{"unit": "c.py::h", "reason": "budget"}]
    m2 = DM.build_review_model(_run(tmp_path, advisory=[], sem=sem2),
                               context="dev:o/r#7")
    t2, h2, md2 = (DM.render_text(m2), DM.render_html(m2),
                   DM.render_markdown(m2))
    assert ("the same pair published in the other direction" in t2
            and "the same pair published in the other direction" in md2)
    assert "budget" in t2 and "budget" in md2


def test_coverage_labels_stay_accurate(tmp_path: Path):
    full = {"status": "ok", "coverage": "full", "spend_usd": 0.01,
            "reason": "examined 1 targets, 1 pairs; 0 approved",
            "targets": [{"path": "a.py", "symbol": "f"}],
            "pairs_examined": 1, "skipped": [],
            "published": [], "suppressed": []}
    m = DM.build_review_model(_run(tmp_path, advisory=[], sem=full),
                              context="dev:o/r#7")
    assert m["semdup"]["coverage_word"] == "complete"
    part = dict(full, coverage="partial")
    m = DM.build_review_model(_run(tmp_path, advisory=[], sem=part),
                              context="dev:o/r#7")
    assert m["semdup"]["coverage_word"] == "partial"
    bad = dict(full, status="unavailable", coverage="unknown")
    m = DM.build_review_model(_run(tmp_path, advisory=[], sem=bad),
                              context="dev:o/r#7")
    assert m["semdup"]["coverage_word"] == "not completed"


# -- stored DEV artifacts (cart-ray #80, needletail #10) -------------------

STORED = {
    "cart-ray-80": "/data/coding/codatiel-i2/semdup-08/devstate/runs/"
                   "a78e4d313213e619/d42f59ea5ca987fe",
    "needletail-10": "/data/coding/codatiel-i2/semdup-08/devstate/runs/"
                     "2100fb3c4297bed4/da7191b81d2f7e33",
}


@pytest.mark.parametrize("name,run_dir", list(STORED.items()))
def test_stored_artifacts_editorial(name: str, run_dir: str):
    run = Path(run_dir)
    if not (run / "run.json").exists() or not (run / "semdup.json").exists():
        pytest.skip(f"stored run {name} not present in this checkout")
    m = DM.build_review_model(run)
    assert m["n_reuse"] >= 2, name
    pubs = m["semdup"]["published"]
    assert len(pubs) == m["n_reuse"]
    # No contradiction: reuse approved => no "nothing found" anywhere.
    for blob in (m["overview"], m["semdup"]["state"]):
        assert "Nothing found" not in blob, name
    assert "reuse" in m["overview"], name
    # Every location preserved across all three renderers.
    text, html, md = (DM.render_text(m), DM.render_html(m),
                      DM.render_markdown(m))
    import html as _html_mod
    for p in pubs:
        assert p["target_symbol"] in text, (name, p)
        # HTML escapes markup characters; the symbol must appear escaped.
        assert _html_mod.escape(p["target_symbol"]) in html, (name, p)
        assert p["target_symbol"] in md, (name, p)
    assert text.count("may reuse") == len(pubs) == html.count("may reuse")
    assert md.count("may reuse") == len(pubs)
    if name == "needletail-10":
        assert m["semdup"]["coverage_word"] == "partial"
        # Overlapping pairs collapse, locations preserved.
        assert m["n_opportunities"] <= m["n_reuse"]
        assert "distinct opportunities" in m["overview"]
