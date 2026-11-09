"""CIENCE normalization entry point using shared agency rules."""

from agencies.adl.extraction.normalizers import AgencyNormalizer


class CIENCENormalizer(AgencyNormalizer):
    """CIENCE normalizer; agency-specific rules can be added here."""


__all__ = ["CIENCENormalizer"]
