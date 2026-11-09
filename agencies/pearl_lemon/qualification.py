"""Pearl Lemon scoring weights, rules, and qualification thresholds.

These settings use shared baselines until agency-specific criteria are supplied.
"""

from dataclasses import dataclass, field

from agencies.adl.qualification_engine.orchestrator import QualificationOrchestrator
from agencies.adl.qualification_engine.rules import (
    DeterministicRuleEngine,
    QualificationConfig,
)
from agencies.adl.qualification_engine.scorer import QualificationScorer, ScorerConfig
from agencies.adl.qualification_engine.tierer import LeadTierer, TiererConfig
from agencies.pearl_lemon.icp import PearlLemonICP


@dataclass(frozen=True)
class PearlLemonQualificationSettings:
    eligibility: PearlLemonICP = field(default_factory=PearlLemonICP)
    scoring: ScorerConfig = field(default_factory=ScorerConfig)
    tiering: TiererConfig = field(default_factory=TiererConfig)


DEFAULT_PEARL_LEMON_QUALIFICATION = PearlLemonQualificationSettings()

__all__ = [
    "DEFAULT_PEARL_LEMON_QUALIFICATION",
    "DeterministicRuleEngine",
    "LeadTierer",
    "PearlLemonQualificationSettings",
    "QualificationConfig",
    "QualificationOrchestrator",
    "QualificationScorer",
    "ScorerConfig",
    "TiererConfig",
]
