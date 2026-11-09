"""CIENCE scoring weights, rules, and qualification thresholds.

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
from agencies.cience.icp import CIENCEICP


@dataclass(frozen=True)
class CIENCEQualificationSettings:
    eligibility: CIENCEICP = field(default_factory=CIENCEICP)
    scoring: ScorerConfig = field(default_factory=ScorerConfig)
    tiering: TiererConfig = field(default_factory=TiererConfig)


DEFAULT_CIENCE_QUALIFICATION = CIENCEQualificationSettings()

__all__ = [
    "CIENCEQualificationSettings",
    "DEFAULT_CIENCE_QUALIFICATION",
    "DeterministicRuleEngine",
    "LeadTierer",
    "QualificationConfig",
    "QualificationOrchestrator",
    "QualificationScorer",
    "ScorerConfig",
    "TiererConfig",
]
