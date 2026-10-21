"""docs_code detector: deterministic documentation contradiction proof.

Pipeline per changed file:
  classify -> retrieve counterpart (normalized subject overlap, top-5)
  -> read base+head bytes of both sides (revision-bound evidence)
  -> extract typed facts per revision
  -> form contradiction candidates (bounded, with rejection reasons)
  -> run the registered class validator
  -> emit verified findings only on validator survival.

No models, no embeddings, no breadth agents. Behaviour class: not
implemented (records `uncertain`, never verified).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codatiel_i2.common.hashing import short_id
from codatiel_i2.core import files as files_mod
from codatiel_i2.detectors.base import (
    ApplicabilityResult, Candidate, Detector, EvidenceBundle, Verification,
)
from codatiel_i2.detectors.docs_code import candidates as cand_mod
from codatiel_i2.detectors.docs_code import facts as facts_mod
from codatiel_i2.detectors.docs_code import validators as validators_mod
from codatiel_i2.tools import read_file as rf_mod

from codatiel_i2.detectors.docs_code import normalize as _NORM

DOC_EXTS = (".md", ".mdx", ".rst")
CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".go", ".swift", ".rs")


class DocsCodeDetector(Detector):
    detector_id = "docs_code"

    def applicable(self, change_set: dict, index: Any = None) -> ApplicabilityResult:
        changes = change_set.get("changes") or []
        units = sum(1 for row in changes if _is_doc_or_code(row.get("path", "")))
        return ApplicabilityResult(
            applicable=units > 0,
            reason="docs_code applies to doc/code changes" if units else "no doc/code changes",
            units=units)

    def generate_candidates(self, change_set: dict, index: Any = None, tools: Any = None) -> list[Candidate]:
        # Candidates are formed in scan_pair with full evidence context.
        return []

    def gather_evidence(self, candidate: Candidate, tools: Any = None) -> EvidenceBundle:
        return EvidenceBundle(candidate_id=candidate.candidate_id,
                              evidence=list(candidate.metadata.get("evidence", [])))

    def verify(self, candidate: Candidate, evidence: EvidenceBundle, models: Any = None) -> Verification:
        from codatiel_i2.core.findings import is_verified_proof_kind

        result = candidate.metadata.get("proof_result")
        if result and result.get("survives") and is_verified_proof_kind(result.get("validator", "")):
            return Verification(candidate_id=candidate.candidate_id, status="survives",
                                proof_level="verified",
                                proof_kind=result.get("validator", ""),
                                detail=result.get("detail", ""))
        return Verification(candidate_id=candidate.candidate_id, status="uncertain",
                            proof_level="candidate",
                            detail=candidate.metadata.get("reject_reason", "no surviving proof"))

    def findings(self, candidate: Candidate, evidence: EvidenceBundle,
                 verification: Verification) -> list[dict]:
        if verification.status != "survives":
            return []
        meta = candidate.metadata
        result = meta.get("proof_result", {})
        anchor = candidate.changed_anchor
        evidence_ids = [e.get("evidence_id") for e in evidence.evidence if e.get("evidence_id")]
        return [{
            "finding_id": "finding_" + short_id(candidate.candidate_id)[:12],
            "candidate_id": candidate.candidate_id,
            "detector": "docs_code",
            "subclass": meta.get("proof_class", ""),
            "changed_anchor": anchor,
            "counterpart_anchors": candidate.counterpart_anchors,
            "invariant": meta.get("invariant", ""),
            "base_state": result.get("base_state", ""),
            "head_state": result.get("head_state", ""),
            "trigger": result.get("trigger", ""),
            "bad_outcome": result.get("bad_outcome", ""),
            "evidence_ids": evidence_ids,
            "fact_ids": meta.get("fact_ids", []),
            "proof": {"level": "verified", "kind": result.get("validator", ""),
                      "detail": result.get("detail", ""),
                      "artifact_evidence_ids": evidence_ids},
            "proof_validator_id": result.get("validator", ""),
            "proof_validator_version": result.get("version", ""),
            "proof_result": {k: v for k, v in result.items()},
            "verification": {"status": "survives"},
            "confidence": 0.97,
            "publish_eligible": True,
        }]


def _is_doc_or_code(path: str) -> bool:
    lower = path.lower()
    return lower.endswith(DOC_EXTS) or lower.endswith(CODE_EXTS)


def scan_pair(*, base_dir: Path, head_dir: Path, base_sha: str, head_sha: str,
              repo: str, changes: list[dict]) -> dict:
    """Run the full docs_code pipeline over one base/head pair.

    Returns {"findings", "facts", "candidates", "proofs", "evidence",
    "verifications", "coverage"} with the B6.16 inspectable chain.
    """
    base_dir, head_dir = Path(base_dir), Path(head_dir)
    evidence: list[dict] = []
    facts: list[dict] = []
    candidates: list[dict] = []
    proofs: list[dict] = []
    findings: list[dict] = []
    verifications: list[dict] = []
    examined = 0

    detector = DocsCodeDetector()
    retrieved_pairs: list[list[str]] = []
    for row in changes:
        path = row.get("path", "")
        if not _is_doc_or_code(path):
            continue
        info = files_mod.classify_file(path)
        changed_is_docs = info["category"] == "docs"
        pair = _examine_changed_file(
            base_dir, head_dir, base_sha, head_sha, path, changed_is_docs,
            detector, evidence, facts, candidates, proofs, findings, verifications)
        examined += 1 if pair else 0
        if pair:
            retrieved_pairs.append(pair)

    coverage = {"docs_code": {"applicable_relationships": examined, "checked": examined,
                             "unsupported": 0}}
    return {"findings": findings, "facts": facts, "candidates": candidates,
            "proofs": proofs, "evidence": evidence,
            "verifications": verifications, "coverage": coverage,
            "retrieved_pairs": retrieved_pairs}


def _read_all(base_dir: Path, head_dir: Path, base_sha: str, head_sha: str,
              path: str, evidence: list[dict]) -> dict[str, list[str] | None]:
    """Read base+head bytes of one path. Missing side -> None (added/removed)."""
    out: dict[str, list[str] | None] = {}
    for revision, root in (("base", base_dir), ("head", head_dir)):
        try:
            result = rf_mod.read_file(root, path, base_sha=base_sha, head_sha=head_sha,
                                      revision=revision)  # type: ignore[arg-type]
            env = result["evidence"]
            if env["kind"] == "source_excerpt":
                evidence.append(env)
                out[revision] = env["excerpt"].splitlines()
            else:
                out[revision] = []
        except (FileNotFoundError, ValueError):
            out[revision] = None
    return out


def _retrieve_counterparts(query_text: str, pool: dict[str, str],
                           exclude: set[str], top_k: int = 5) -> list[tuple[str, float]]:
    query = _NORM.normalize_text(query_text)
    scored = []
    for path, content in pool.items():
        if path in exclude:
            continue
        score = _NORM.normalized_overlap(query, _NORM.normalize_text(content))
        if score > 0:
            scored.append((path, score))
    scored.sort(key=lambda r: (-r[1], r[0]))
    return scored[:top_k]


def _head_tree(head_dir: Path) -> dict[str, str]:
    head_dir = Path(head_dir)
    tree: dict[str, str] = {}
    for path in sorted(head_dir.rglob("*")):
        if path.is_symlink() or not path.is_file() or ".git" in path.parts:
            continue
        if not _is_doc_or_code(str(path.relative_to(head_dir))):
            continue
        try:
            tree[str(path.relative_to(head_dir))] = path.read_text(encoding="utf-8", errors="strict")
        except Exception:
            continue
    return tree


def _examine_changed_file(base_dir, head_dir, base_sha, head_sha, path, changed_is_docs,
                          detector, evidence, facts, candidates, proofs, findings,
                          verifications) -> list[str] | None:
    """Examine one changed file against its best counterpart.

    Returns [changed, counterpart] when a pair was examined, else None.
    """
    sides = _read_all(base_dir, head_dir, base_sha, head_sha, path, evidence)
    if sides["head"] is None:
        return None
    head_pool = _head_tree(head_dir)
    if changed_is_docs:
        query_text = "\n".join(sides["head"] or [])
        pool = {p: c for p, c in head_pool.items() if not p.lower().endswith(DOC_EXTS)}
    else:
        # Query both revisions: removals leave their only trace in base.
        query_text = "\n".join((sides["base"] or []) + (sides["head"] or []))
        pool = {p: c for p, c in head_pool.items() if p.lower().endswith(DOC_EXTS)}
    ranked = _retrieve_counterparts(query_text, pool, {path})
    if not ranked and changed_is_docs and pool:
        # Reverse direction: the claimed name is absent by design, so lexical
        # overlap may be zero. Fall back to the deterministic first code file
        # and let the validator (repo-wide surface enumeration) decide. The
        # weak retrieval is recorded, not hidden.
        first = sorted(pool)[0]
        ranked = [(first, 0.0)]
        weak_retrieval = True
    else:
        weak_retrieval = False
    if not ranked:
        return None
    counterpart, _score = ranked[0]
    cp_sides = _read_all(base_dir, head_dir, base_sha, head_sha, counterpart, evidence)
    anchor = {"path": path, "line_start": 1,
              "line_end": max(1, len(sides["head"] or []))}
    cp_anchor = {"path": counterpart, "line_start": 1,
                 "line_end": max(1, len(cp_sides["head"] or [])),
                 "revision": "head", "revision_sha": head_sha}
    before = len(candidates)
    _run_class_pipelines(base_dir, head_dir, base_sha, head_sha, path, counterpart,
                         sides, cp_sides, anchor, cp_anchor, changed_is_docs,
                         detector, evidence, facts, candidates, proofs, findings,
                         verifications)
    for row in candidates[before:]:
        row["retrieval_weak"] = weak_retrieval
    return [path, counterpart]


def _run_class_pipelines(base_dir, head_dir, base_sha, head_sha, path, counterpart,
                         sides, cp_sides, anchor, cp_anchor, changed_is_docs,
                         detector, evidence, facts, candidates, proofs, findings,
                         verifications) -> None:
    code_path, doc_path = (counterpart, path) if changed_is_docs else (path, counterpart)
    code_sides = cp_sides if changed_is_docs else sides
    doc_sides = sides if changed_is_docs else cp_sides
    if code_sides["head"] is None or doc_sides["head"] is None:
        return
    base_code = code_sides["base"] or []
    head_code = code_sides["head"] or []
    base_doc = doc_sides["base"] if doc_sides["base"] is not None else []
    head_doc = doc_sides["head"] or []

    # ---- literal ----
    _run_literal(code_path, doc_path, base_code, head_code, base_doc, head_doc,
                 anchor, cp_anchor, evidence, facts, candidates, proofs,
                 findings, verifications, detector,
                 base_sha=base_sha, head_sha=head_sha)
    # ---- named ----
    direction = "docs_changed" if changed_is_docs else "code_changed"
    _run_named(code_path, doc_path, base_code, head_code, base_doc, head_doc,
               head_dir, base_dir, anchor, cp_anchor, evidence, facts, candidates, proofs,
               findings, verifications, detector, direction=direction,
               base_sha=base_sha, head_sha=head_sha)
    # ---- allowed ----
    _run_allowed(code_path, doc_path, base_code, head_code, base_doc, head_doc,
                 anchor, cp_anchor, evidence, facts, candidates, proofs,
                 findings, verifications, detector,
                 base_sha=base_sha, head_sha=head_sha)
    # ---- interface ----
    _run_interface(code_path, doc_path, base_code, head_code, base_doc, head_doc,
                   anchor, cp_anchor, evidence, facts, candidates, proofs,
                   findings, verifications, detector,
                   base_sha=base_sha, head_sha=head_sha)


def _ev_for(path: str, revision: str, evidence: list[dict]) -> list[str]:
    return [e["evidence_id"] for e in evidence
            if e.get("path") == path and e.get("revision") == revision
            and e.get("kind") == "source_excerpt"]


def _emit(detector, proof_class, invariant, anchor, cp_anchor, fact_list,
          proof_result, evidence, facts, candidates, proofs, findings, verifications,
          reject_reason: str = "", proof_record: dict | None = None) -> None:
    candidate_id = "cand_" + short_id(
        proof_class, anchor.get("path", ""), str(anchor.get("line_start", "")),
        ",".join(a.get("path", "") for a in [cp_anchor]),
        ",".join(sorted(f.get("fact_id", "") for f in fact_list)))[:16]
    for fact in fact_list:
        facts.append(fact)
    # Link only evidence for the files this candidate reasons about.
    involved = {anchor.get("path", "")}
    involved.update(a.get("path", "") for a in [cp_anchor])
    involved.update(f.get("path", "") for f in fact_list)
    linked = [e for e in evidence if e.get("path") in involved]
    candidates.append({"candidate_id": candidate_id, "proof_class": proof_class,
                       "changed_anchor": anchor,
                       "counterpart_anchors": [cp_anchor],
                       "invariant": invariant,
                       "candidate": proof_result.get("survives"),
                       "reason": proof_result.get("reason", reject_reason),
                       "validator": proof_result.get("validator", "")})
    proofs.append({"candidate_id": candidate_id, "record": proof_record, **proof_result})
    candidate = Candidate(candidate_id=candidate_id, detector="docs_code",
                          changed_anchor=anchor, counterpart_anchors=[cp_anchor],
                          invariant=invariant,
                          metadata={"proof_class": proof_class,
                                    "proof_result": proof_result,
                                    "fact_ids": [f["fact_id"] for f in fact_list],
                                    "reject_reason": reject_reason,
                                    "invariant": invariant,
                                    "evidence": linked})
    bundle = detector.gather_evidence(candidate)
    verification = detector.verify(candidate, bundle)
    verifications.append({"candidate_id": candidate_id,
                          "status": verification.status,
                          "proof_level": verification.proof_level})
    for finding in detector.findings(candidate, bundle, verification):
        findings.append(finding)


def _run_literal(code_path, doc_path, base_code, head_code, base_doc, head_doc,
                 anchor, cp_anchor, evidence, facts, candidates, proofs,
                 findings, verifications, detector,
                 base_sha: str = "", head_sha: str = "") -> None:
    doc_facts = {rev: facts_mod.bind_facts(facts_mod.extract_doc_literals(p, lines), p, rev,
                                          base_sha if rev == "base" else head_sha, evidence)
                 for rev, (p, lines) in (("base", (doc_path, base_doc)), ("head", (doc_path, head_doc)))}
    code_facts = {rev: facts_mod.bind_facts(facts_mod.extract_code_literals(p, lines), p, rev,
                                            base_sha if rev == "base" else head_sha, evidence)
                  for rev, (p, lines) in (("base", (code_path, base_code)), ("head", (code_path, head_code)))}
    for doc_fact in doc_facts["head"]:
        for code_fact in code_facts["head"]:
            formed = cand_mod.form_literal_candidate(doc_fact, code_fact)
            if not formed["candidate"]:
                _emit(detector, "literal_default",
                      f"documented {doc_fact.get('subject_display')} == runtime default",
                      anchor, cp_anchor, [doc_fact, code_fact],
                      {"survives": False, "reason": formed["reason"],
                       "validator": "docs_code.literal_value_mismatch.v1", "version": "v1"},
                      evidence, facts, candidates, proofs, findings, verifications,
                      reject_reason=formed["reason"])
                continue
            base_doc_match = _match_fact(doc_facts["base"], doc_fact)
            base_code_match = _match_fact(code_facts["base"], code_fact)
            if base_doc_match is None or base_code_match is None:
                continue
            result = validators_mod.validate_literal_value_mismatch(
                base_doc_match, base_code_match, doc_fact, code_fact)
            record = _proof_record(
                "docs_code.literal_value_mismatch.v1", "v1",
                {"doc_base": base_doc_match, "code_base": base_code_match,
                 "doc_head": doc_fact, "code_head": code_fact}, {})
            _emit(detector, "literal_default",
                  f"documented {doc_fact.get('subject_display')} == runtime default",
                  anchor, cp_anchor, [doc_fact, code_fact, base_doc_match, base_code_match],
                  result, evidence, facts, candidates, proofs, findings, verifications,
                  proof_record=record)


def _proof_record(validator_id: str, version: str, role_facts: dict[str, dict],
                  extra: dict) -> dict:
    """Authoritative proof record: validator identity + version, role-mapped
    input fact IDs, fact payload snapshots, and structured validator inputs."""
    return {
        "validator_id": validator_id,
        "validator_version": version,
        "input_fact_ids": {role: fact["fact_id"] for role, fact in role_facts.items()},
        "facts_snapshot": {role: dict(fact) for role, fact in role_facts.items()},
        "proof_inputs": dict(extra),
    }


def _match_fact(facts_list: list[dict], target: dict) -> dict | None:
    for fact in facts_list:
        if fact.get("subject") == target.get("subject"):
            return fact
    return None


def _run_named(code_path, doc_path, base_code, head_code, base_doc, head_doc,
               head_dir, base_dir, anchor, cp_anchor, evidence, facts, candidates, proofs,
               findings, verifications, detector, direction="code_changed",
               base_sha: str = "", head_sha: str = "") -> None:
    head_doc_facts = facts_mod.bind_facts(
        facts_mod.extract_doc_names(doc_path, head_doc), doc_path, "head", head_sha, evidence)
    if not head_doc_facts:
        return
    head_names, head_aliases, scanned_files, _rel = facts_mod.enumerate_code_names(head_dir)
    # Baseline provision is repo-wide, like the head surface: "X existed
    # in the consistent baseline" is a repository property, not a
    # single-file one. (Replay recomputes the same way.)
    base_names, _base_aliases, _base_scanned, _base_rel = facts_mod.enumerate_code_names(base_dir)
    for doc_fact in head_doc_facts:
        formed = cand_mod.form_named_candidate(
            doc_fact, [{"name": n} for n in head_names if n == doc_fact["name"]])
        if not formed["candidate"]:
            _emit(detector, "named_contract",
                  f"documented contract {doc_fact.get('name')} is provided",
                  anchor, cp_anchor, [doc_fact],
                  {"survives": False, "reason": formed["reason"],
                   "validator": "docs_code.named_contract_missing.v1", "version": "v1"},
                  evidence, facts, candidates, proofs, findings, verifications,
                  reject_reason=formed["reason"])
            continue
        result = validators_mod.validate_named_contract_missing(
            doc_fact, head_names, base_names, head_aliases, direction=direction,
            surface_enumerated=scanned_files > 0)
        record = _proof_record(
            "docs_code.named_contract_missing.v1", "v1", {"doc_fact": doc_fact},
            {"direction": direction, "head_names": sorted(head_names),
             "base_names": sorted(base_names), "head_aliases": sorted(head_aliases),
             "surface_enumerated": scanned_files > 0})
        _emit(detector, "named_contract",
              f"documented contract {doc_fact.get('name')} is provided",
              anchor, cp_anchor, [doc_fact],
              result, evidence, facts, candidates, proofs, findings, verifications,
              proof_record=record)


def _run_allowed(code_path, doc_path, base_code, head_code, base_doc, head_doc,
                 anchor, cp_anchor, evidence, facts, candidates, proofs,
                 findings, verifications, detector,
                 base_sha: str = "", head_sha: str = "") -> None:
    doc_facts = facts_mod.bind_facts(
        facts_mod.extract_doc_sets(doc_path, head_doc), doc_path, "head", head_sha, evidence)
    code_facts = facts_mod.bind_facts(
        facts_mod.extract_code_sets(code_path, head_code), code_path, "head", head_sha, evidence)
    base_set = set(base_doc)
    added = [(i + 1, line) for i, line in enumerate(head_doc) if line not in base_set]
    claim_facts = facts_mod.bind_facts(
        facts_mod.extract_doc_value_claims(doc_path, added), doc_path, "head", head_sha, evidence)
    doc_facts = doc_facts + claim_facts
    for doc_fact in doc_facts:
        for code_fact in code_facts:
            formed = cand_mod.form_allowed_candidate(doc_fact, code_fact)
            if not formed["candidate"]:
                _emit(detector, "allowed_values",
                      f"documented {doc_fact.get('subject_display')} values match implementation",
                      anchor, cp_anchor, [doc_fact, code_fact],
                      {"survives": False, "reason": formed["reason"],
                       "validator": "docs_code.allowed_values_mismatch.v1", "version": "v1"},
                      evidence, facts, candidates, proofs, findings, verifications,
                      reject_reason=formed["reason"])
                continue
            result = validators_mod.validate_allowed_values_mismatch(doc_fact, code_fact)
            record = _proof_record(
                "docs_code.allowed_values_mismatch.v1", "v1",
                {"doc_fact": doc_fact, "code_fact": code_fact}, {})
            _emit(detector, "allowed_values",
                  f"documented {doc_fact.get('subject_display')} values match implementation",
                  anchor, cp_anchor, [doc_fact, code_fact],
                  result, evidence, facts, candidates, proofs, findings, verifications,
                  proof_record=record)


def _closest_route(doc_fact: dict, code_routes: list[dict]) -> dict | None:
    """Representative code route for the detail string: same method wins."""
    if not code_routes:
        return None
    method = doc_fact.get("interface", {}).get("method")
    for fact in code_routes:
        if fact.get("interface", {}).get("method") == method:
            return fact
    return code_routes[0]


def _run_interface(code_path, doc_path, base_code, head_code, base_doc, head_doc,
                   anchor, cp_anchor, evidence, facts, candidates, proofs,
                   findings, verifications, detector,
                   base_sha: str = "", head_sha: str = "") -> None:
    doc_facts = facts_mod.bind_facts(
        facts_mod.extract_doc_interfaces(doc_path, head_doc), doc_path, "head", head_sha, evidence)
    code_facts = facts_mod.bind_facts(
        facts_mod.extract_code_interfaces(code_path, head_code), code_path, "head", head_sha, evidence)
    code_routes = [f for f in code_facts
                   if f.get("interface", {}).get("kind") == "http_route"]
    for doc_fact in doc_facts:
        doc_if = doc_fact.get("interface", {})
        if doc_if.get("kind") == "http_route":
            # Set semantics: the documented path must be served by SOME
            # implemented route. One candidate per documented route.
            implemented = {(c["interface"].get("method"), c["interface"].get("path"))
                           for c in code_routes}
            paths = {c["interface"].get("path") for c in code_routes}
            if (doc_if.get("method"), doc_if.get("path")) in implemented or (
                    doc_if.get("method") in (None, "*") and doc_if.get("path") in paths):
                _emit(detector, "interface",
                      f"documented interface {doc_fact.get('subject')} matches implementation",
                      anchor, cp_anchor, [doc_fact],
                      {"survives": False, "reason": "matched_interface",
                       "validator": "docs_code.interface_contract_mismatch.v1", "version": "v1"},
                      evidence, facts, candidates, proofs, findings, verifications,
                      reject_reason="matched_interface")
                continue
            closest = _closest_route(doc_fact, code_routes)
            result = validators_mod.validate_interface_contract_mismatch(
                doc_fact, closest,
                implemented_paths=paths)
            record = _proof_record(
                "docs_code.interface_contract_mismatch.v1", "v1",
                {"doc_fact": doc_fact, **({"code_fact": closest} if closest else {})},
                {"implemented_paths": sorted(paths)})
            _emit(detector, "interface",
                  f"documented interface {doc_fact.get('subject')} matches implementation",
                  anchor, cp_anchor, [doc_fact] + ([closest] if closest else []),
                  result, evidence, facts, candidates, proofs, findings, verifications,
                  proof_record=record)
            continue
        for code_fact in code_facts:
            if code_fact.get("interface", {}).get("kind") != "call_signature":
                continue
            if facts_mod.subject_key(doc_fact.get("subject", "")) != facts_mod.subject_key(code_fact.get("subject", "")):
                continue
            formed = cand_mod.form_interface_candidate(doc_fact, code_fact)
            if not formed["candidate"]:
                _emit(detector, "interface",
                      f"documented interface {doc_fact.get('subject')} matches implementation",
                      anchor, cp_anchor, [doc_fact, code_fact],
                      {"survives": False, "reason": formed["reason"],
                       "validator": "docs_code.interface_contract_mismatch.v1", "version": "v1"},
                      evidence, facts, candidates, proofs, findings, verifications,
                      reject_reason=formed["reason"])
                continue
            result = validators_mod.validate_interface_contract_mismatch(doc_fact, code_fact)
            record = _proof_record(
                "docs_code.interface_contract_mismatch.v1", "v1",
                {"doc_fact": doc_fact, "code_fact": code_fact}, {})
            _emit(detector, "interface",
                  f"documented interface {doc_fact.get('subject')} matches implementation",
                  anchor, cp_anchor, [doc_fact, code_fact],
                  result, evidence, facts, candidates, proofs, findings, verifications,
                  proof_record=record)
