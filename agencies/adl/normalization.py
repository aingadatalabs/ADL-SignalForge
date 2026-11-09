"""Compatibility spelling for the ADL normalization interface."""

from agencies.adl.normalizer import (
    AgencyNormalizer,
    is_blocked_domain,
    is_valid_agency_url,
    normalize_domain,
)

__all__ = [
    "AgencyNormalizer",
    "is_blocked_domain",
    "is_valid_agency_url",
    "normalize_domain",
]
