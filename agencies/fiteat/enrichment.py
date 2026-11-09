"""FitEat enrichment schema and normalized output-field mappings."""

from types import MappingProxyType
from typing import Final, Mapping

from agencies.adl.enrichment_engine.extractor import extract_agency_intelligence
from agencies.adl.enrichment_engine.schemas import AgencyEnrichmentProfile

FITEAT_ENRICHMENT_FIELD_MAPPINGS: Final[Mapping[str, str]] = MappingProxyType(
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
    "AgencyEnrichmentProfile",
    "FITEAT_ENRICHMENT_FIELD_MAPPINGS",
    "extract_agency_intelligence",
]
