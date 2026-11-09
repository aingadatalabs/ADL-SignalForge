"""ADL enrichment schema and normalized output-field mappings."""

import importlib
from types import MappingProxyType
from typing import Final, Mapping
from pathlib import Path
import sys

# Keep the agency facade importable while exposing its implementation modules
# as nested imports for existing callers.
__path__ = [str(Path(__file__).with_name("enrichment_engine"))]

from agencies.adl.enrichment_engine.extractor import extract_agency_intelligence
from agencies.adl.enrichment_engine.schemas import AgencyEnrichmentProfile

for _module_name in ("crawler", "extractor", "orchestrator", "schemas"):
    sys.modules[f"{__name__}.{_module_name}"] = importlib.import_module(
        f"agencies.adl.enrichment_engine.{_module_name}"
    )

ADL_ENRICHMENT_FIELD_MAPPINGS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "decision_makers": "decision_makers",
        "contacts": "contacts",
        "social_profiles": "social_profiles",
        "technology_stack": "tech_stack",
        "estimated_team_size": "estimated_team_size",
        "extraction_metadata": "extraction_metadata",
    }
)

__all__ = [
    "ADL_ENRICHMENT_FIELD_MAPPINGS",
    "AgencyEnrichmentProfile",
    "extract_agency_intelligence",
]
