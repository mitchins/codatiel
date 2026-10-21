"""No-op detector: proves the pipeline runs end-to-end with zero detectors."""

from __future__ import annotations

from typing import Any

from codatiel_i2.detectors.base import (
    ApplicabilityResult, Candidate, Detector, EvidenceBundle, Verification,
)


class NoOpDetector(Detector):
    detector_id = "noop"

    def applicable(self, change_set: dict, index: Any = None) -> ApplicabilityResult:
        return ApplicabilityResult(applicable=False, reason="no-op detector never applies", units=0)

    def generate_candidates(self, change_set: dict, index: Any = None, tools: Any = None) -> list[Candidate]:
        return []

    def gather_evidence(self, candidate: Candidate, tools: Any = None) -> EvidenceBundle:
        return EvidenceBundle(candidate_id=candidate.candidate_id, evidence=[])

    def verify(self, candidate: Candidate, evidence: EvidenceBundle, models: Any = None) -> Verification:
        return Verification(candidate_id=candidate.candidate_id, status="refuted",
                            proof_level="candidate", proof_kind="", detail="no-op")

    def findings(self, candidate: Candidate, evidence: EvidenceBundle,
                 verification: Verification) -> list[dict]:
        return []


DETECTOR_REGISTRY: dict[str, type[Detector]] = {
    "noop": NoOpDetector,
}

# Phase B+ detectors register here once measured. Names reserved:
# docs_code, contracts, authorization, operating_constraints, semantic_twins
