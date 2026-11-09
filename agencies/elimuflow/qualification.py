"""ElimuFlow scoring weights, rules, and qualification thresholds.

These settings use shared baselines until ElimuFlow-specific criteria are supplied.
"""

from dataclasses import dataclass, field

from agencies.adl.qualification_engine.orchestrator import QualificationOrchestrator
from agencies.adl.qualification_engine.rules import (
    DeterministicRuleEngine,
    QualificationConfig,
)
from agencies.adl.qualification_engine.scorer import QualificationScorer, ScorerConfig
from agencies.adl.qualification_engine.tierer import LeadTierer, TiererConfig
from agencies.elimuflow.icp import ElimuFlowICP


@dataclass(frozen=True)
class ElimuFlowQualificationSettings:
    eligibility: ElimuFlowICP = field(default_factory=ElimuFlowICP)
    scoring: ScorerConfig = field(default_factory=ScorerConfig)
    tiering: TiererConfig = field(default_factory=TiererConfig)


DEFAULT_ELIMUFLOW_QUALIFICATION = ElimuFlowQualificationSettings()

__all__ = [
    "DEFAULT_ELIMUFLOW_QUALIFICATION",
    "DeterministicRuleEngine",
    "ElimuFlowQualificationSettings",
    "LeadTierer",
    "QualificationConfig",
    "QualificationOrchestrator",
    "QualificationScorer",
    "ScorerConfig",
    "TiererConfig",
]
