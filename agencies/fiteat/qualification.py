"""FitEat scoring weights, rules, and qualification thresholds.

These settings use shared baselines until FitEat-specific criteria are supplied.
"""

from dataclasses import dataclass, field

from agencies.adl.qualification_engine.orchestrator import QualificationOrchestrator
from agencies.adl.qualification_engine.rules import (
    DeterministicRuleEngine,
    QualificationConfig,
)
from agencies.adl.qualification_engine.scorer import QualificationScorer, ScorerConfig
from agencies.adl.qualification_engine.tierer import LeadTierer, TiererConfig
from agencies.fiteat.icp import FiteatICP


@dataclass(frozen=True)
class FiteatQualificationSettings:
    eligibility: FiteatICP = field(default_factory=FiteatICP)
    scoring: ScorerConfig = field(default_factory=ScorerConfig)
    tiering: TiererConfig = field(default_factory=TiererConfig)


DEFAULT_FITEAT_QUALIFICATION = FiteatQualificationSettings()

__all__ = [
    "DEFAULT_FITEAT_QUALIFICATION",
    "DeterministicRuleEngine",
    "FiteatQualificationSettings",
    "LeadTierer",
    "QualificationConfig",
    "QualificationOrchestrator",
    "QualificationScorer",
    "ScorerConfig",
    "TiererConfig",
]
