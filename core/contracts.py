"""Shared abstract contracts for agency processing stages."""

from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

CandidateT = TypeVar("CandidateT")
ResultT = TypeVar("ResultT")


class BaseEnricher(ABC, Generic[CandidateT, ResultT]):
    @abstractmethod
    async def run(self, candidate: CandidateT) -> ResultT:
        """Enrich a single candidate."""


class BaseQualifier(ABC, Generic[CandidateT, ResultT]):
    @abstractmethod
    async def evaluate_candidate(
        self,
        normalized_candidate: CandidateT,
        enrichment_profile: Any | None = None,
        verification_report: Any | None = None,
    ) -> ResultT:
        """Evaluate, score, and route a normalized candidate."""
