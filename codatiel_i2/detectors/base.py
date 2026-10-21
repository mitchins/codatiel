"""Typed detector contract (§7). All five detectors implement this interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApplicabilityResult:
    applicable: bool
    reason: str = ""
    units: int = 0


@dataclass
class Candidate:
    candidate_id: str
    detector: str
    changed_anchor: dict
    counterpart_anchors: list[dict] = field(default_factory=list)
    invariant: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class EvidenceBundle:
    candidate_id: str
    evidence: list[dict] = field(default_factory=list)


@dataclass
class Verification:
    candidate_id: str
    status: str  # survives | refuted | uncertain
    proof_level: str = "candidate"  # candidate | corroborated | verified
    proof_kind: str = ""
    detail: str = ""


class Detector(ABC):
    detector_id: str = ""

    @abstractmethod
    def applicable(self, change_set: dict, index: Any = None) -> ApplicabilityResult:
        ...

    @abstractmethod
    def generate_candidates(self, change_set: dict, index: Any = None, tools: Any = None) -> list[Candidate]:
        ...

    @abstractmethod
    def gather_evidence(self, candidate: Candidate, tools: Any = None) -> EvidenceBundle:
        ...

    @abstractmethod
    def verify(self, candidate: Candidate, evidence: EvidenceBundle, models: Any = None) -> Verification:
        ...

    @abstractmethod
    def findings(self, candidate: Candidate, evidence: EvidenceBundle,
                 verification: Verification) -> list[dict]:
        ...
