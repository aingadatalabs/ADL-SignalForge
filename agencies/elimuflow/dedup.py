"""ElimuFlow deduplication entry point using shared entity resolution."""

from agencies.adl.identity.entity_resolution import EntityResolver


class ElimuFlowDeduplicator(EntityResolver):
    """ElimuFlow deduplicator using the shared entity-resolution strategy."""


__all__ = ["ElimuFlowDeduplicator"]
