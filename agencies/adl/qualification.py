"""ADL scoring weights, qualification rules, and tier thresholds."""

from dataclasses import dataclass, field
import importlib
from pathlib import Path
import sys

# Expose the implementation modules as submodules while keeping this requested
# agency-level facade importable as ``agencies.adl.qualification``.
__path__ = [str(Path(__file__).with_name("qualification_engine"))]

from agencies.adl.qualification_engine.orchestrator import QualificationOrchestrator
from agencies.adl.qualification_engine.rules import (
    DeterministicRuleEngine,
    QualificationConfig,
)
from agencies.adl.qualification_engine.scorer import QualificationScorer, ScorerConfig
from agencies.adl.qualification_engine.tierer import LeadTierer, TiererConfig

for _module_name in ("orchestrator", "rules", "schemas", "scorer", "tierer"):
    sys.modules[f"{__name__}.{_module_name}"] = importlib.import_module(
        f"agencies.adl.qualification_engine.{_module_name}"
    )


@dataclass(frozen=True)
class ADLQualificationSettings:
    """Combined qualification settings using the shared, reviewed baselines."""

    eligibility: QualificationConfig = field(default_factory=QualificationConfig)
    scoring: ScorerConfig = field(default_factory=ScorerConfig)
    tiering: TiererConfig = field(default_factory=TiererConfig)


DEFAULT_ADL_QUALIFICATION = ADLQualificationSettings()

__all__ = [
    "ADLQualificationSettings",
    "DEFAULT_ADL_QUALIFICATION",
    "DeterministicRuleEngine",
    "LeadTierer",
    "QualificationConfig",
    "QualificationOrchestrator",
    "QualificationScorer",
    "ScorerConfig",
    "TiererConfig",
]
