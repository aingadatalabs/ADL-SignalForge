"""Pearl Lemon normalization entry point using shared agency rules."""

from agencies.adl.extraction.normalizers import AgencyNormalizer


class PearlLemonNormalizer(AgencyNormalizer):
    """Pearl Lemon normalizer; agency-specific rules can be added here."""


__all__ = ["PearlLemonNormalizer"]
