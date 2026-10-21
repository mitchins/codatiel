"""Artifact-backed trust validation: fabricated findings cannot pass."""

from __future__ import annotations

from pathlib import Path

from codatiel_i2.eval.scorer import ci_gates
from codatiel_i2.github.publish import publish_gate
from codatiel_i2.tools import read_file as rf
from codatiel_i2.verify.trust import validate_verified_finding


def _real_evidence(two_commit_repo, tmp_path):
    from codatiel_i2.core import workspace as ws_mod

    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    work = ws_mod.prepare_local_workspace(two_commit_repo["repo"], base, head, tmp_path / "work")
    assert work.status == "ready"
    out = rf.read_file(Path(work.head_dir), "src/app.py", base_sha=base, head_sha=head,
                       revision="head", start_line=1, end_line=2)
    return work, out["evidence"]


def _head_counterpart(head_sha: str) -> list[dict]:
    return [{"path": "docs/configuration.md", "line_start": 1, "line_end": 3,
             "revision": "head", "revision_sha": head_sha}]


def _finding(ev_id, **overrides):
    base = {
        "finding_id": "f1",
        "candidate_id": "c1",
        "detector": "docs_code",
        "subclass": "stale_default",
        "changed_anchor": {"path": "src/app.py", "line_start": 1, "line_end": 2},
        "counterpart_anchors": [{"path": "docs/configuration.md", "line_start": 1, "line_end": 3,
                                 "revision": "head", "revision_sha": "HEAD"}
                                ],
        "invariant": "documented timeout == runtime default",
        "base_state": "code=30 docs=30",
        "head_state": "code=60 docs=30",
        "trigger": "operator sets timeout per docs",
        "bad_outcome": "service waits 60s instead of documented 30s",
        "evidence_ids": [ev_id],
        "proof": {"level": "verified", "kind": "deterministic_contradiction",
                  "detail": "head value 60 != documented 30",
                  "artifact_evidence_ids": [ev_id]},
        "verification": {"status": "survives"},
        "confidence": 0.97,
        "publish_eligible": True,
    }
    base.update(overrides)
    return base


def _trust_kwargs(work, two_commit_repo):
    return {
        "verification_by_candidate": {"c1": {"status": "survives"}},
        "base_checkout": work.base_dir,
        "head_checkout": work.head_dir,
        "changed_paths": {"src/app.py", "src/new_feature.py", "docs/configuration.md"},
        "expected_base_sha": two_commit_repo["base_sha"],
        "expected_head_sha": two_commit_repo["head_sha"],
    }


def test_honest_finding_passes_provenance_but_not_proof(two_commit_repo, tmp_path):
    """All provenance checks pass; only the (empty) proof-kind allowlist blocks.

    This is the closed gate working as designed: real artifacts, no
    self-certifying semantics. Detector proof validators re-enable kinds.
    """
    work, ev = _real_evidence(two_commit_repo, tmp_path)
    cp = dict(_finding(ev["evidence_id"])["counterpart_anchors"][0])
    cp["revision_sha"] = two_commit_repo["head_sha"]
    finding = _finding(ev["evidence_id"], counterpart_anchors=[cp])
    errors = validate_verified_finding(
        finding, evidence_by_id={ev["evidence_id"]: ev}, **_trust_kwargs(work, two_commit_repo))
    assert len(errors) == 1, errors
    assert "no registered mechanical validator" in errors[0]
    # CI gates with full trust context: provenance clean, proof blocked.
    trust_context = {"evidence_by_id": {ev["evidence_id"]: ev}, **_trust_kwargs(work, two_commit_repo)}
    gates = ci_gates([finding], evidence_ids_known={ev["evidence_id"]}, trust_context=trust_context)
    assert gates["pass"] is False
    assert gates["trust_errors"] != {}


def test_false_semantic_contradiction_cannot_self_certify(two_commit_repo, tmp_path):
    """Real artifacts, lied states (docs=999 nowhere in evidence): must fail."""
    work, ev = _real_evidence(two_commit_repo, tmp_path)
    cp = dict(_finding(ev["evidence_id"])["counterpart_anchors"][0])
    cp["revision_sha"] = two_commit_repo["head_sha"]
    finding = _finding(ev["evidence_id"], counterpart_anchors=[cp],
                       base_state="code=30 docs=999",
                       head_state="code=60 docs=999")
    errors = validate_verified_finding(
        finding, evidence_by_id={ev["evidence_id"]: ev}, **_trust_kwargs(work, two_commit_repo))
    assert errors != []
    assert any("no registered mechanical validator" in e for e in errors)


def test_fake_evidence_id_rejected(two_commit_repo, tmp_path):
    work, _ = _real_evidence(two_commit_repo, tmp_path)
    finding = _finding("ev_fake", proof={"level": "verified", "kind": "deterministic_contradiction",
                                         "detail": "x", "artifact_evidence_ids": ["ev_fake"]})
    errors = validate_verified_finding(
        finding, evidence_by_id={},
        **_trust_kwargs(work, two_commit_repo))
    assert any("ev_fake" in e for e in errors)


def test_nonexistent_anchor_rejected(two_commit_repo, tmp_path):
    work, ev = _real_evidence(two_commit_repo, tmp_path)
    finding = _finding(ev["evidence_id"],
                       changed_anchor={"path": "does/not/exist.py", "line_start": 999, "line_end": 1000})
    errors = validate_verified_finding(
        finding, evidence_by_id={ev["evidence_id"]: ev},
        **_trust_kwargs(work, two_commit_repo))
    assert errors != []


def test_wrong_excerpt_rejected(two_commit_repo, tmp_path):
    work, ev = _real_evidence(two_commit_repo, tmp_path)
    tampered = dict(ev, excerpt="def timeout():\n    return 999")
    finding = _finding(tampered["evidence_id"],
                       proof={"level": "verified", "kind": "deterministic_contradiction",
                              "detail": "x", "artifact_evidence_ids": [tampered["evidence_id"]]})
    errors = validate_verified_finding(
        finding, evidence_by_id={tampered["evidence_id"]: tampered},
        **_trust_kwargs(work, two_commit_repo))
    assert any("does not match" in e or "mismatch" in e or "forged" in e for e in errors)


def test_base_head_mismatch_rejected(two_commit_repo, tmp_path):
    """Head bytes (return 60) relabelled as base evidence must fail replay."""
    work, _ = _real_evidence(two_commit_repo, tmp_path)
    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    head_out = rf.read_file(Path(work.head_dir), "src/app.py", base_sha=base, head_sha=head,
                            revision="head", start_line=1, end_line=2)
    assert "return 60" in head_out["evidence"]["excerpt"]
    relabelled = dict(head_out["evidence"], revision="base", revision_sha=base)
    finding = _finding(relabelled["evidence_id"],
                       proof={"level": "verified", "kind": "deterministic_contradiction",
                              "detail": "x", "artifact_evidence_ids": [relabelled["evidence_id"]]})
    errors = validate_verified_finding(
        finding, evidence_by_id={relabelled["evidence_id"]: relabelled},
        **_trust_kwargs(work, two_commit_repo))
    assert any("does not match" in e for e in errors)


def test_fabricated_proof_and_verification_rejected(two_commit_repo, tmp_path):
    work, ev = _real_evidence(two_commit_repo, tmp_path)
    ledger = {ev["evidence_id"]: ev}
    eid = ev["evidence_id"]
    # model-only proof kind
    model_only = _finding(eid, proof={"level": "verified", "kind": "model_agreement",
                                      "detail": "trust me", "artifact_evidence_ids": [eid]})
    assert validate_verified_finding(
        model_only, evidence_by_id=ledger,
        **_trust_kwargs(work, two_commit_repo)) != []
    # blank proof detail
    blank = _finding(eid, proof={"level": "verified", "kind": "deterministic_contradiction",
                                 "detail": "   ", "artifact_evidence_ids": [eid]})
    assert validate_verified_finding(
        blank, evidence_by_id=ledger,
        **_trust_kwargs(work, two_commit_repo)) != []
    # bare assertion without artifacts
    bare = _finding(eid, proof={"level": "verified", "kind": "deterministic_contradiction",
                                "detail": "values differ"})
    assert validate_verified_finding(
        bare, evidence_by_id=ledger,
        **_trust_kwargs(work, two_commit_repo)) != []
    # refuted verification record
    refuted = _finding(eid)
    kwargs = _trust_kwargs(work, two_commit_repo)
    kwargs["verification_by_candidate"] = {"c1": {"status": "refuted"}}
    assert validate_verified_finding(refuted, evidence_by_id=ledger, **kwargs) != []
    # missing verification ledger fails closed
    kwargs2 = _trust_kwargs(work, two_commit_repo)
    kwargs2["verification_by_candidate"] = None
    assert validate_verified_finding(refuted, evidence_by_id=ledger, **kwargs2) != []


def test_publish_gate_rejects_fake_pointers(tmp_path):
    fake = _finding("ev_fake",
                    changed_anchor={"path": "does/not/exist.py", "line_start": 999, "line_end": 1000},
                    proof={"level": "verified", "kind": "deterministic_contradiction",
                           "detail": "x", "artifact_evidence_ids": ["ev_fake"]})
    gate = publish_gate([fake], precision_gate_passed=True,
                        evidence_by_id={}, verification_by_candidate={},
                        base_checkout=tmp_path, head_checkout=tmp_path,
                        changed_paths={"does/not/exist.py"},
                        expected_base_sha="a" * 40, expected_head_sha="b" * 40)
    assert gate["publishable"] == []
    assert gate["validation"] == "artifact_backed"
    # no trust context at all: fail closed, syntactic rows kept separate
    gate2 = publish_gate([fake], precision_gate_passed=True)
    assert gate2["publishable"] == []
    assert gate2["validation"] == "incomplete_context"


def test_forged_diff_hunk_is_rejected(two_commit_repo, tmp_path):
    """Self-signed diff text on a genuinely changed file must fail replay."""
    from codatiel_i2.core.evidence import make_evidence

    work, ev = _real_evidence(two_commit_repo, tmp_path)
    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    forged = make_evidence(kind="diff_hunk", tool="diff_window", base_sha=base, head_sha=head,
                           revision="diff", path="src/app.py", line_start=1, line_end=1,
                           excerpt="@@ -1 +1 @@\n-VALUE=30\n+VALUE=999999\n",
                           tool_args={"context_lines": 40}).to_dict()
    finding = _finding(ev["evidence_id"],
                       changed_anchor={"path": "src/app.py", "line_start": 1, "line_end": 1},
                       evidence_ids=[forged["evidence_id"]],
                       proof={"level": "verified", "kind": "deterministic_contradiction",
                              "detail": "diff shows the change",
                              "artifact_evidence_ids": [forged["evidence_id"]]})
    errors = validate_verified_finding(
        finding, evidence_by_id={ev["evidence_id"]: ev, forged["evidence_id"]: forged},
        **_trust_kwargs(work, two_commit_repo))
    assert any("does not match the recomputed" in e for e in errors), errors


def test_honest_diff_hunk_replays(two_commit_repo, tmp_path):
    from codatiel_i2.tools.diff_window import diff_window

    work, ev = _real_evidence(two_commit_repo, tmp_path)
    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    dw = diff_window(two_commit_repo["repo"], base, head, "src/app.py")
    assert "return 60" in dw["diff"]
    finding = _finding(ev["evidence_id"],
                       counterpart_anchors=_head_counterpart(head),
                       evidence_ids=[dw["evidence"]["evidence_id"]],
                       proof={"level": "verified", "kind": "deterministic_contradiction",
                              "detail": "recomputed diff shows 30->60",
                              "artifact_evidence_ids": [dw["evidence"]["evidence_id"]]})
    errors = validate_verified_finding(
        finding, evidence_by_id={ev["evidence_id"]: ev,
                                 dw["evidence"]["evidence_id"]: dw["evidence"]},
        **_trust_kwargs(work, two_commit_repo))
    # Diff bytes replay cleanly; only the empty proof-kind allowlist blocks.
    assert len(errors) == 1, errors
    assert "no registered mechanical validator" in errors[0]


def test_unbacked_witness_cannot_be_verified(two_commit_repo, tmp_path):
    """A self-consistent witness envelope with no executor is not proof."""
    from codatiel_i2.core.evidence import make_evidence

    work, ev = _real_evidence(two_commit_repo, tmp_path)
    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    fake_witness = make_evidence(kind="witness", tool="witness_runner", base_sha=base, head_sha=head,
                                 revision="generated", revision_sha="run-123",
                                 path="witness/stdout.log", line_start=1, line_end=1,
                                 excerpt="ALL TESTS PASS: trust me").to_dict()
    finding = _finding(ev["evidence_id"],
                       proof={"level": "verified", "kind": "executable_witness",
                              "detail": "witness passed",
                              "artifact_evidence_ids": [fake_witness["evidence_id"]]})
    errors = validate_verified_finding(
        finding, evidence_by_id={ev["evidence_id"]: ev,
                                 fake_witness["evidence_id"]: fake_witness},
        **_trust_kwargs(work, two_commit_repo))
    assert errors != [], "unbacked executable_witness must not verify"
    assert any("executable_witness" in e for e in errors)


def test_external_document_without_artifact_replay_cannot_verify(two_commit_repo, tmp_path):
    """Fabricated vendor evidence (self-consistent digest/ID) cannot verify."""
    from codatiel_i2.core.evidence import make_evidence

    work, ev = _real_evidence(two_commit_repo, tmp_path)
    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    fake_external = make_evidence(kind="external_document", tool="lookup_docs",
                                  base_sha=base, head_sha=head, revision="external",
                                  revision_sha="sha256-of-nothing-real",
                                  path="vendor-cache://no/such/file",
                                  line_start=1, line_end=1,
                                  excerpt="Vendor guarantees exactly 999 results").to_dict()
    finding = _finding(ev["evidence_id"],
                       proof={"level": "verified", "kind": "deterministic_contradiction",
                              "detail": "vendor guarantees 999",
                              "artifact_evidence_ids": [fake_external["evidence_id"]]})
    errors = validate_verified_finding(
        finding, evidence_by_id={ev["evidence_id"]: ev,
                                 fake_external["evidence_id"]: fake_external},
        **_trust_kwargs(work, two_commit_repo))
    assert errors != [], "non-repository artifact must not back verified proof"
    assert any("not repository-bound" in e or "no registered mechanical validator" in e
               for e in errors)


def test_diff_evidence_malformed_args_fail_closed(two_commit_repo, tmp_path):
    """Non-dict tool_args and out-of-hunk anchors are errors, never crashes."""
    work, ev = _real_evidence(two_commit_repo, tmp_path)
    from codatiel_i2.core.evidence import replay_source_evidence, validate_envelope

    bad_args = dict(ev, kind="diff_hunk", revision="diff", tool_args="evil",
                    excerpt="@@ -1 +1 @@\n-a\n+b\n")
    assert validate_envelope(bad_args) != []
    assert replay_source_evidence(bad_args, base_checkout=work.base_dir,
                                  head_checkout=work.head_dir,
                                  changed_paths={"src/app.py"}) != []
    # honest bytes re-minted with trailing padding are rejected (exact compare)
    from codatiel_i2.tools.diff_window import diff_window

    base, head = two_commit_repo["base_sha"], two_commit_repo["head_sha"]
    dw = diff_window(two_commit_repo["repo"], base, head, "src/app.py")
    padded = dict(dw["evidence"], excerpt=dw["evidence"]["excerpt"] + "\n\n\n")
    assert replay_source_evidence(padded, base_checkout=work.base_dir,
                                  head_checkout=work.head_dir,
                                  changed_paths={"src/app.py"}) != []
    # honest diff with an out-of-hunk anchor is rejected
    moved = dict(dw["evidence"], line_start=9999, line_end=9999)
    assert replay_source_evidence(moved, base_checkout=work.base_dir,
                                  head_checkout=work.head_dir,
                                  changed_paths={"src/app.py"}) != []


def test_diff_argv_is_hermetic():
    from codatiel_i2.core.evidence import diff_git_argv

    argv = diff_git_argv("a" * 40, "b" * 40, "f.py", 3)
    assert "--no-ext-diff" in argv and "--no-textconv" in argv
    assert "diff.noprefix=false" in " ".join(argv)


def test_ci_gates_honor_caller_anchor_veto():
    fake = _finding("ev_fake")
    assert ci_gates([fake], anchors_valid=False)["pass"] is False
    assert ci_gates([fake], evidence_ids_known=set())["pass"] is False
