"""authorization detector (Phase E; stub in Phase A)."""

from __future__ import annotations

from typing import Any

from codatiel_i2.detectors.base import (
    ApplicabilityResult, Candidate, Detector, EvidenceBundle, Verification,
)

STATUS = "experimental_not_implemented_until_detector_milestone_E"


class AuthorizationDetector(Detector):
    detector_id = "authorization"

    def applicable(self, change_set: dict, index: Any = None) -> ApplicabilityResult:
        return ApplicabilityResult(applicable=False, reason=STATUS, units=0)

    def generate_candidates(self, change_set: dict, index: Any = None, tools: Any = None) -> list[Candidate]:
        return []

    def gather_evidence(self, candidate: Candidate, tools: Any = None) -> EvidenceBundle:
        return EvidenceBundle(candidate_id=candidate.candidate_id, evidence=[])

    def verify(self, candidate: Candidate, evidence: EvidenceBundle, models: Any = None) -> Verification:
        return Verification(candidate_id=candidate.candidate_id, status="uncertain",
                            proof_level="candidate", detail=STATUS)

    def findings(self, candidate: Candidate, evidence: EvidenceBundle,
                 verification: Verification) -> list[dict]:
        return []
