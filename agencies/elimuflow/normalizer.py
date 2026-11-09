"""ElimuFlow normalization entry point using shared agency rules."""

from agencies.adl.extraction.normalizers import AgencyNormalizer


class ElimuFlowNormalizer(AgencyNormalizer):
    """ElimuFlow normalizer; agency-specific rules can be added here."""


__all__ = ["ElimuFlowNormalizer"]
